"""Tests for bmnews.pipeline: fetching, storing, and cached digests."""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

from bmlib.db import connect_sqlite, fetch_one
from click.testing import CliRunner

from bmnews.cli import main
from bmnews.config import load_config
from bmnews.db.operations import record_digest, save_score, upsert_paper
from bmnews.db.schema import init_db
from bmnews.fetchers.base import FetchedPaper
from bmnews.pipeline import run_store, show_cached_digests


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
    pid = upsert_paper(conn, doi="10.1101/cached1", title="Cached Paper",
                       abstract="Abs", published_date=_days_ago(2),
                       source="medrxiv")
    save_score(conn, paper_id=pid, combined_score=0.8, relevance_score=0.9,
               quality_score=0.7, summary="Great paper.")
    record_digest(conn, [pid], delivery_method="stdout")
    return conn


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


class TestRunStore:
    @patch("bmnews.pipeline.open_db")
    def test_stores_pmid_pmcid_from_metadata(self, mock_open_db, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = connect_sqlite(db_path)
        init_db(conn)
        conn.close()
        mock_open_db.return_value = connect_sqlite(db_path)
        config = _test_config()

        papers = [
            FetchedPaper(
                doi="10.1234/test",
                title="Test Paper",
                source="europepmc",
                metadata={"pmid": "12345", "pmcid": "PMC678"},
            ),
        ]
        run_store(config, papers)

        conn2 = connect_sqlite(db_path)
        row = fetch_one(conn2, "SELECT pmid, pmcid FROM papers WHERE doi = ?",
                        ("10.1234/test",))
        assert row["pmid"] == "12345"
        assert row["pmcid"] == "PMC678"
        conn2.close()

    @patch("bmnews.pipeline.open_db")
    def test_stores_paper_without_identifiers(self, mock_open_db, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = connect_sqlite(db_path)
        init_db(conn)
        conn.close()
        mock_open_db.return_value = connect_sqlite(db_path)
        config = _test_config()

        papers = [
            FetchedPaper(
                doi="10.1234/noid",
                title="No IDs Paper",
                source="medrxiv",
            ),
        ]
        run_store(config, papers)

        conn2 = connect_sqlite(db_path)
        row = fetch_one(conn2, "SELECT pmid, pmcid FROM papers WHERE doi = ?",
                        ("10.1234/noid",))
        assert row["pmid"] is None
        assert row["pmcid"] is None
        conn2.close()


class TestRunStoreIdentifiers:
    """Regression tests for identifiers landing on the wrong paper row.

    A batch that mixes new inserts with re-fetched papers used to scramble
    PMIDs: SQLite leaves ``lastrowid`` pointing at the last row actually
    inserted, so the id returned for a conflicting upsert belonged to a
    different paper.
    """

    @patch("bmnews.pipeline.open_db")
    def test_mixed_insert_and_conflict_batch(self, mock_open_db, tmp_path):
        db_path = str(tmp_path / "mixed.db")
        conn = connect_sqlite(db_path)
        init_db(conn)
        conn.close()

        def _paper(i, pmid=None):
            return FetchedPaper(
                doi=f"10.1234/p{i}", title=f"Paper {i}", source="europepmc",
                metadata={"pmid": pmid} if pmid else {},
            )

        config = _test_config()

        # Day 1: paper 0 arrives without identifiers.
        mock_open_db.return_value = connect_sqlite(db_path)
        run_store(config, [_paper(0)])

        # Day 2: one brand-new paper (INSERT) followed by the already-stored
        # paper 0, now carrying a PMID (ON CONFLICT → UPDATE).
        mock_open_db.return_value = connect_sqlite(db_path)
        run_store(config, [_paper(9, "9999"), _paper(0, "1000")])

        conn = connect_sqlite(db_path)
        p0 = fetch_one(conn, "SELECT pmid FROM papers WHERE doi = ?", ("10.1234/p0",))
        p9 = fetch_one(conn, "SELECT pmid FROM papers WHERE doi = ?", ("10.1234/p9",))
        conn.close()

        assert p0["pmid"] == "1000"
        assert p9["pmid"] == "9999"

    @patch("bmnews.pipeline.open_db")
    def test_paper_without_doi_is_skipped(self, mock_open_db, tmp_path):
        db_path = str(tmp_path / "nodoi.db")
        conn = connect_sqlite(db_path)
        init_db(conn)
        conn.close()
        mock_open_db.return_value = connect_sqlite(db_path)

        stored = run_store(_test_config(), [
            FetchedPaper(doi="", title="No DOI", source="medrxiv"),
            FetchedPaper(doi="10.1234/has-doi", title="Has DOI", source="medrxiv"),
        ])
        assert stored == 1


class TestRecordToFetchedPaper:
    """Nothing bmlib normalises may be dropped on the way into the database."""

    def _record(self, **overrides):
        from bmlib.fulltext.models import FullTextSourceEntry
        from bmlib.publications.models import FetchedRecord

        defaults = dict(
            title="A Trial",
            source="pubmed",
            doi="10.1234/abc",
            pmid="111",
            pmc_id="PMC222",
            abstract="Findings.",
            authors=["Smith J", "Jones A"],
            journal="The Journal",
            publication_date="2026-02-10",
            keywords=["Oncology"],
            publication_types=["Randomized Controlled Trial"],
            is_open_access=True,
            license="cc-by",
            fulltext_sources=[
                FullTextSourceEntry(url="http://x/p.pdf", format="pdf", source="pubmed"),
            ],
            extras={"category": "Medicine"},
        )
        defaults.update(overrides)
        return FetchedRecord(**defaults)

    def _convert(self, **overrides):
        from bmnews.pipeline import _record_to_fetched_paper
        return _record_to_fetched_paper(self._record(**overrides))

    def test_publication_types_reach_the_quality_classifier(self):
        """pub_type is the only input to bmlib's free Tier-1 classification."""
        from bmnews.scoring.scorer import _extract_pub_types

        paper = self._convert()
        assert paper.metadata["pub_type"] == ["Randomized Controlled Trial"]

        stored = {"metadata_json": json.dumps(paper.metadata), "categories": ""}
        assert "Randomized Controlled Trial" in _extract_pub_types(stored)

    def test_carries_journal_license_and_access(self):
        paper = self._convert()
        assert paper.metadata["journal"] == "The Journal"
        assert paper.metadata["license"] == "cc-by"
        assert paper.metadata["is_open_access"] is True

    def test_carries_identifiers_and_fulltext_sources(self):
        paper = self._convert()
        assert paper.metadata["pmid"] == "111"
        assert paper.metadata["pmcid"] == "PMC222"
        assert paper.metadata["fulltext_sources"][0]["url"] == "http://x/p.pdf"

    def test_core_fields(self):
        paper = self._convert()
        assert paper.doi == "10.1234/abc"
        assert paper.authors == "Smith J; Jones A"
        assert paper.url == "https://doi.org/10.1234/abc"
        assert paper.published_date == "2026-02-10"

    def test_categories_prefer_keywords(self):
        assert self._convert().categories == "Oncology"

    def test_categories_fall_back_to_the_rxiv_subject(self):
        """bioRxiv/medRxiv report their subject as an extra, not a keyword."""
        assert self._convert(keywords=[]).categories == "Medicine"

    def test_url_falls_back_to_a_source_supplied_one(self):
        paper = self._convert(
            doi=None, extras={"url": "https://europepmc.org/article/med/111"},
        )
        assert paper.url == "https://europepmc.org/article/med/111"

    def test_normalised_fields_win_over_same_named_extras(self):
        paper = self._convert(extras={"journal": "Stale", "pmid": "999"})
        assert paper.metadata["journal"] == "The Journal"
        assert paper.metadata["pmid"] == "111"


class TestRunFetchSourceDispatch:
    """Every enabled source resolves through bmlib's registry."""

    def test_unknown_source_is_skipped(self):
        from bmnews.pipeline import run_fetch

        config = _test_config()
        config.sources.enabled = ["not-a-real-source"]
        assert run_fetch(config) == []

    @patch("bmnews.pipeline._fetch_via_registry", return_value=[])
    def test_europepmc_goes_through_the_registry(self, mock_fetch):
        from bmnews.pipeline import run_fetch

        config = _test_config()
        config.sources.enabled = ["europepmc"]
        config.sources.europepmc_query = "cancer"
        run_fetch(config)

        source_name, _lookback, src_config, _progress = mock_fetch.call_args[0]
        assert source_name == "europepmc"
        assert src_config == {"query": "cancer"}

    @patch("bmnews.pipeline._fetch_via_registry", return_value=[])
    def test_openalex_gets_the_user_email(self, mock_fetch):
        from bmnews.pipeline import run_fetch

        config = _test_config()
        config.sources.enabled = ["openalex"]
        config.user.email = "me@example.com"
        run_fetch(config)

        assert mock_fetch.call_args[0][2] == {"email": "me@example.com"}
