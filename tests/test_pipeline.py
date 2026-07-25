"""Tests for bmnews.pipeline: syncing, storing, and cached digests."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from bmlib.db import connect_sqlite
from bmlib.publications import register_source
from bmlib.publications.models import (
    FetchedRecord,
    FetchResult,
    SourceDescriptor,
    SyncProgress,
)
from click.testing import CliRunner

from bmnews.cli import main
from bmnews.config import load_config
from bmnews.db.operations import (
    get_paper_by_doi,
    get_paper_with_score,
    publication_id,
    record_digest,
    save_score,
    store_paper,
)
from bmnews.db.schema import init_db
from bmnews.pipeline import run_sync, show_cached_digests

STUB_SOURCE = "teststub"


def _test_config():
    """Build a minimal config pointing at an in-memory DB."""
    config = load_config(None)
    config.database.backend = "sqlite"
    config.database.sqlite_path = ":memory:"
    return config


def _days_ago(days: int) -> str:
    """Return an ISO date *days* before today.

    Tests must not hard-code calendar dates: a fixed "recent" date stops being
    recent and silently turns a passing suite red months later.
    """
    return (date.today() - timedelta(days=days)).isoformat()


def _seeded_db():
    """Return a conn with papers, scores, and a digest recorded."""
    conn = connect_sqlite(":memory:")
    init_db(conn)
    pid = store_paper(conn, doi="10.1101/cached1", title="Cached Paper",
                      abstract="Abs", published_date=_days_ago(2),
                      source="medrxiv")
    save_score(conn, paper_id=pid, combined_score=0.8, relevance_score=0.9,
               quality_score=0.7, summary="Great paper.")
    record_digest(conn, [pid], delivery_method="stdout")
    return conn


def _register_stub_source(records: list[FetchedRecord], status: str = "completed") -> None:
    """Register a fetcher that emits *records* for whatever day it is asked for.

    Going through the real registry (rather than sync's ``_fetcher_override``
    test hook) keeps the source-resolution path under test.
    """

    def fetcher(client, target_date, *, on_record, on_progress=None, **config):
        if on_progress is not None:
            on_progress(
                SyncProgress(
                    source=STUB_SOURCE,
                    date=target_date.isoformat(),
                    records_processed=len(records),
                    records_total=len(records),
                    status=status,
                )
            )
        for record in records:
            on_record(record)
        return FetchResult(
            source=STUB_SOURCE,
            date=target_date.isoformat(),
            record_count=len(records),
            status=status,
        )

    register_source(
        SourceDescriptor(
            name=STUB_SOURCE, display_name="Test stub", description="test fixture",
        ),
        fetcher,
    )


class TestShowCachedDigests:
    @patch("bmnews.pipeline.open_db")
    def test_renders_cached_papers(self, mock_open_db):
        mock_open_db.return_value = _seeded_db()
        config = _test_config()
        text = show_cached_digests(config)
        assert "Cached Paper" in text

    @patch("bmnews.pipeline.open_db")
    def test_returns_empty_when_no_cached(self, mock_open_db):
        conn = connect_sqlite(":memory:")
        init_db(conn)
        mock_open_db.return_value = conn
        config = _test_config()
        text = show_cached_digests(config)
        assert text == ""


class TestRunCLI:
    @patch("bmnews.pipeline.open_db")
    def test_run_show_cached(self, mock_open_db):
        mock_open_db.return_value = _seeded_db()
        runner = CliRunner()
        result = runner.invoke(main, ["run", "--show_cached"])
        assert result.exit_code == 0
        assert "Cached Paper" in result.output

    @patch("bmnews.pipeline.open_db")
    def test_run_show_cached_with_days(self, mock_open_db):
        mock_open_db.return_value = _seeded_db()
        runner = CliRunner()
        result = runner.invoke(main, ["run", "--show_cached", "--days", "30"])
        assert result.exit_code == 0
        assert "Cached Paper" in result.output

    @patch("bmnews.pipeline.run_pipeline")
    def test_run_days_without_show_cached(self, mock_pipeline):
        """--days without --show_cached passes through to pipeline."""
        runner = CliRunner()
        result = runner.invoke(main, ["run", "--days", "14"])
        assert result.exit_code == 0
        mock_pipeline.assert_called_once()
        _, kwargs = mock_pipeline.call_args
        assert kwargs.get("days") == 14
        assert kwargs.get("show_cached") is False


class TestRunSync:
    """run_sync delegates fetch *and* store to bmlib.publications.sync."""

    def _run(self, records, tmp_path, config=None, **run_kwargs):
        """Sync *records* into a fresh file-backed database and return a conn."""
        db_path = str(tmp_path / "sync.db")
        conn = connect_sqlite(db_path)
        init_db(conn)
        conn.close()

        _register_stub_source(records)
        config = config or _test_config()
        config.sources.enabled = [STUB_SOURCE]
        config.sources.lookback_days = 0

        with patch("bmnews.pipeline.open_db", return_value=connect_sqlite(db_path)):
            report = self.report = run_sync(config, **run_kwargs)

        assert not report.errors
        return connect_sqlite(db_path)

    def test_stores_a_fetched_record(self, tmp_path):
        conn = self._run(
            [
                FetchedRecord(
                    title="Test Paper",
                    source=STUB_SOURCE,
                    doi="10.1234/test",
                    pmid="12345",
                    pmc_id="PMC678",
                    authors=["Smith J"],
                    publication_date="2026-02-10",
                )
            ],
            tmp_path,
        )

        paper = get_paper_by_doi(conn, "10.1234/test")
        assert paper["title"] == "Test Paper"
        assert paper["pmid"] == "12345"
        assert paper["pmcid"] == "PMC678"
        assert paper["authors"] == ["Smith J"]
        assert self.report.records_added == 1

    def test_a_record_without_a_doi_is_stored_not_dropped(self, tmp_path):
        """The old papers.doi was NOT NULL, so run_store discarded these."""
        conn = self._run(
            [FetchedRecord(title="PMID only", source=STUB_SOURCE, pmid="999")],
            tmp_path,
        )

        paper_id = publication_id(conn, pmid="999")
        assert paper_id is not None
        assert get_paper_with_score(conn, paper_id)["title"] == "PMID only"

    def test_publication_types_reach_the_quality_classifier(self, tmp_path):
        """pub_types are the only input to bmlib's free Tier-1 classification."""
        from bmnews.scoring.scorer import _extract_pub_types

        conn = self._run(
            [
                FetchedRecord(
                    title="A Trial",
                    source=STUB_SOURCE,
                    doi="10.1234/trial",
                    publication_types=["Randomized Controlled Trial"],
                    keywords=["Oncology"],
                )
            ],
            tmp_path,
        )

        paper = get_paper_by_doi(conn, "10.1234/trial")
        assert paper["publication_types"] == ["Randomized Controlled Trial"]
        assert "Randomized Controlled Trial" in _extract_pub_types(paper)
        assert "Oncology" in _extract_pub_types(paper)

    def test_journal_license_and_access_are_stored(self, tmp_path):
        conn = self._run(
            [
                FetchedRecord(
                    title="OA Paper",
                    source=STUB_SOURCE,
                    doi="10.1234/oa",
                    journal="The Journal",
                    license="cc-by",
                    is_open_access=True,
                )
            ],
            tmp_path,
        )

        paper = get_paper_by_doi(conn, "10.1234/oa")
        assert paper["journal"] == "The Journal"
        assert paper["license"] == "cc-by"
        assert paper["is_open_access"] is True

    def test_fulltext_sources_land_in_bmlibs_table(self, tmp_path):
        from bmlib.fulltext.models import FullTextSourceEntry

        from bmnews.db.operations import get_fulltext_sources

        conn = self._run(
            [
                FetchedRecord(
                    title="With fulltext",
                    source=STUB_SOURCE,
                    doi="10.1234/ft",
                    fulltext_sources=[
                        FullTextSourceEntry(
                            url="http://x/p.pdf", format="pdf", source=STUB_SOURCE,
                        )
                    ],
                )
            ],
            tmp_path,
        )

        paper_id = get_paper_by_doi(conn, "10.1234/ft")["id"]
        assert [s.url for s in get_fulltext_sources(conn, paper_id)] == ["http://x/p.pdf"]

    def test_source_extras_without_a_column_are_kept(self, tmp_path):
        conn = self._run(
            [
                FetchedRecord(
                    title="Cited",
                    source=STUB_SOURCE,
                    doi="10.1234/cited",
                    extras={"cited_by": 7, "url": "https://stale.example/x"},
                )
            ],
            tmp_path,
        )

        metadata = get_paper_by_doi(conn, "10.1234/cited")["metadata"]
        assert metadata["cited_by"] == 7
        # The URL is derived from the identifiers, so a stored copy would only
        # go stale.
        assert "url" not in metadata
        assert get_paper_by_doi(conn, "10.1234/cited")["url"] == "https://doi.org/10.1234/cited"

    def test_the_same_work_twice_is_stored_once(self, tmp_path):
        record = FetchedRecord(title="Dup", source=STUB_SOURCE, doi="10.1234/dup")
        conn = self._run([record, record], tmp_path)

        from bmlib.db import fetch_scalar
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM publications") == 1

    def test_progress_is_reported_as_strings(self, tmp_path):
        messages: list[str] = []
        self._run(
            [FetchedRecord(title="P", source=STUB_SOURCE, doi="10.1234/p")],
            tmp_path,
            on_progress=messages.append,
        )

        assert messages
        assert all(isinstance(m, str) for m in messages)
        assert any(STUB_SOURCE in m for m in messages)


class TestRunSyncSourceDispatch:
    """Every enabled source resolves through bmlib's registry."""

    def test_unknown_source_is_skipped(self):
        config = _test_config()
        config.sources.enabled = ["not-a-real-source"]

        report = run_sync(config)

        assert report.sources_synced == []
        assert report.records_added == 0

    @patch("bmnews.pipeline.open_db")
    @patch("bmnews.pipeline.sync")
    def test_source_options_reach_the_fetcher(self, mock_sync, mock_open_db):
        conn = connect_sqlite(":memory:")
        init_db(conn)
        mock_open_db.return_value = conn
        config = _test_config()
        config.sources.enabled = ["europepmc"]
        config.sources.europepmc_query = "cancer"

        run_sync(config)

        assert mock_sync.call_args.kwargs["source_configs"]["europepmc"] == {"query": "cancer"}

    @patch("bmnews.pipeline.open_db")
    @patch("bmnews.pipeline.sync")
    def test_openalex_gets_the_user_email(self, mock_sync, mock_open_db):
        conn = connect_sqlite(":memory:")
        init_db(conn)
        mock_open_db.return_value = conn
        config = _test_config()
        config.sources.enabled = ["openalex"]
        config.user.email = "me@example.com"

        run_sync(config)

        assert mock_sync.call_args.kwargs["source_configs"]["openalex"] == {
            "email": "me@example.com",
        }

    @patch("bmnews.pipeline.open_db")
    @patch("bmnews.pipeline.sync")
    def test_lookback_days_sets_the_date_range(self, mock_sync, mock_open_db):
        conn = connect_sqlite(":memory:")
        init_db(conn)
        mock_open_db.return_value = conn
        config = _test_config()
        config.sources.enabled = ["medrxiv"]
        config.sources.lookback_days = 3

        run_sync(config)

        kwargs = mock_sync.call_args.kwargs
        assert kwargs["date_to"] == date.today()
        assert kwargs["date_from"] == date.today() - timedelta(days=3)
