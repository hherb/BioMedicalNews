"""Tests for bmnews.pipeline show_cached and days parameter."""

from __future__ import annotations

import tempfile
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


def _seeded_db():
    """Return a conn with papers, scores, and a digest recorded."""
    conn = connect_sqlite(":memory:")
    init_db(conn)
    pid = upsert_paper(conn, doi="10.1101/cached1", title="Cached Paper",
                       abstract="Abs", published_date="2026-02-10",
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
