"""Tests for bmnews.db schema and operations."""

from __future__ import annotations

from bmlib.db import connect_sqlite

from bmnews.db.schema import init_db
from bmnews.db.operations import (
    upsert_paper,
    get_paper_by_doi,
    get_unscored_papers,
    save_score,
    get_scored_papers,
    get_papers_for_digest,
    get_cached_digest_papers,
    record_digest,
    paper_exists,
)


def _db():
    conn = connect_sqlite(":memory:")
    init_db(conn)
    return conn


class TestSchema:
    def test_init_creates_tables(self):
        conn = _db()
        from bmlib.db import table_exists
        assert table_exists(conn, "papers")
        assert table_exists(conn, "scores")
        assert table_exists(conn, "digests")
        assert table_exists(conn, "digest_papers")


class TestPapers:
    def test_upsert_and_retrieve(self):
        conn = _db()
        pid = upsert_paper(
            conn, doi="10.1101/test1", title="Test Paper",
            authors="Smith J", abstract="An abstract.",
            source="medrxiv", published_date="2024-01-01",
        )
        assert pid > 0
        assert paper_exists(conn, "10.1101/test1")

        paper = get_paper_by_doi(conn, "10.1101/test1")
        assert paper["title"] == "Test Paper"
        assert paper["authors"] == "Smith J"

    def test_upsert_updates_existing(self):
        conn = _db()
        upsert_paper(conn, doi="10.1101/upd", title="Original Title")
        upsert_paper(conn, doi="10.1101/upd", title="Updated Title")
        paper = get_paper_by_doi(conn, "10.1101/upd")
        assert paper["title"] == "Updated Title"

    def test_paper_not_found(self):
        conn = _db()
        assert get_paper_by_doi(conn, "nonexistent") is None
        assert not paper_exists(conn, "nonexistent")


class TestScores:
    def test_save_and_retrieve(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1101/scored", title="Scored Paper",
                           abstract="Abstract")
        save_score(
            conn, paper_id=pid, relevance_score=0.8,
            quality_score=0.7, combined_score=0.76,
            summary="A good paper.", study_design="RCT",
        )
        scored = get_scored_papers(conn, min_combined=0.5)
        assert len(scored) == 1
        assert scored[0]["title"] == "Scored Paper"
        assert scored[0]["combined_score"] == 0.76

    def test_unscored_papers(self):
        conn = _db()
        upsert_paper(conn, doi="10.1101/a", title="Paper A", abstract="A")
        upsert_paper(conn, doi="10.1101/b", title="Paper B", abstract="B")
        pid = upsert_paper(conn, doi="10.1101/c", title="Paper C", abstract="C")
        save_score(conn, paper_id=pid, combined_score=0.5)

        unscored = get_unscored_papers(conn)
        dois = [p["doi"] for p in unscored]
        assert "10.1101/a" in dois
        assert "10.1101/b" in dois
        assert "10.1101/c" not in dois


class TestDigests:
    def test_papers_for_digest_excludes_sent(self):
        conn = _db()
        pid1 = upsert_paper(conn, doi="10.1101/d1", title="P1", abstract="A1")
        pid2 = upsert_paper(conn, doi="10.1101/d2", title="P2", abstract="A2")
        save_score(conn, paper_id=pid1, combined_score=0.8)
        save_score(conn, paper_id=pid2, combined_score=0.9)

        # Both should be available
        available = get_papers_for_digest(conn, min_combined=0.5)
        assert len(available) == 2

        # Send digest with first paper
        record_digest(conn, [pid1], delivery_method="email")

        # Only second paper should remain
        available = get_papers_for_digest(conn, min_combined=0.5)
        assert len(available) == 1
        assert available[0]["doi"] == "10.1101/d2"


class TestCachedDigestPapers:
    def test_returns_papers_from_previous_digests(self):
        conn = _db()
        pid1 = upsert_paper(conn, doi="10.1101/c1", title="Cached 1",
                            abstract="A1", published_date="2026-02-10")
        pid2 = upsert_paper(conn, doi="10.1101/c2", title="Not in digest",
                            abstract="A2", published_date="2026-02-10")
        save_score(conn, paper_id=pid1, combined_score=0.8, summary="Sum1")
        save_score(conn, paper_id=pid2, combined_score=0.7, summary="Sum2")
        record_digest(conn, [pid1], delivery_method="stdout")

        cached = get_cached_digest_papers(conn)
        assert len(cached) == 1
        assert cached[0]["doi"] == "10.1101/c1"
        assert cached[0]["combined_score"] == 0.8

    def test_filters_by_published_date(self):
        conn = _db()
        pid_old = upsert_paper(conn, doi="10.1101/old", title="Old Paper",
                               abstract="A", published_date="2020-01-01")
        pid_new = upsert_paper(conn, doi="10.1101/new", title="New Paper",
                               abstract="B", published_date="2026-02-12")
        save_score(conn, paper_id=pid_old, combined_score=0.8)
        save_score(conn, paper_id=pid_new, combined_score=0.9)
        record_digest(conn, [pid_old, pid_new], delivery_method="stdout")

        cached = get_cached_digest_papers(conn, days=7)
        assert len(cached) == 1
        assert cached[0]["doi"] == "10.1101/new"

    def test_no_days_returns_all_cached(self):
        conn = _db()
        pid_old = upsert_paper(conn, doi="10.1101/old2", title="Old",
                               abstract="A", published_date="2020-01-01")
        pid_new = upsert_paper(conn, doi="10.1101/new2", title="New",
                               abstract="B", published_date="2026-02-12")
        save_score(conn, paper_id=pid_old, combined_score=0.8)
        save_score(conn, paper_id=pid_new, combined_score=0.9)
        record_digest(conn, [pid_old, pid_new], delivery_method="stdout")

        cached = get_cached_digest_papers(conn)
        assert len(cached) == 2

    def test_empty_when_no_digests(self):
        conn = _db()
        upsert_paper(conn, doi="10.1101/x", title="X", abstract="A",
                     published_date="2026-02-10")
        cached = get_cached_digest_papers(conn)
        assert cached == []
