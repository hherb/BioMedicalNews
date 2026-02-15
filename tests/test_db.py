"""Tests for bmnews.db schema and operations."""

from __future__ import annotations

from bmlib.db import connect_sqlite

from bmnews.db.schema import init_db
from bmnews.db.operations import (
    upsert_paper,
    get_paper_by_doi,
    get_unscored_papers,
    get_paper_with_score,
    get_papers_filtered,
    save_score,
    save_fulltext,
    update_paper_identifiers,
    save_paper_tags,
    get_paper_tags,
    get_all_tags,
    get_papers_by_tag,
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
        assert table_exists(conn, "paper_tags")
        assert table_exists(conn, "schema_version")

    def test_init_is_idempotent(self):
        conn = _db()
        init_db(conn)  # second call
        from bmlib.db import table_exists
        assert table_exists(conn, "papers")
        assert table_exists(conn, "paper_tags")

    def test_migrations_recorded(self):
        conn = _db()
        from bmlib.db.migrations import get_applied_versions
        versions = get_applied_versions(conn)
        assert 1 in versions
        assert 2 in versions


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



class TestPaperWithScore:
    def test_returns_paper_and_score_data(self):
        conn = _db()
        pid = upsert_paper(
            conn, doi="10.1101/pws1", title="PaperWithScore Test",
            authors="Doe J", abstract="Some abstract.",
            source="medrxiv", published_date="2025-06-01",
        )
        save_score(
            conn, paper_id=pid, relevance_score=0.85,
            quality_score=0.70, combined_score=0.78,
            summary="An excellent study.", study_design="RCT",
            quality_tier="high",
        )
        result = get_paper_with_score(conn, pid)
        assert result is not None
        assert result["title"] == "PaperWithScore Test"
        assert result["relevance_score"] == 0.85
        assert result["summary"] == "An excellent study."
        assert result["study_design"] == "RCT"

    def test_returns_none_for_missing_id(self):
        conn = _db()
        assert get_paper_with_score(conn, 9999) is None

    def test_returns_paper_without_score(self):
        conn = _db()
        pid = upsert_paper(
            conn, doi="10.1101/pws_unscored", title="Unscored Paper",
            abstract="No score here.",
        )
        paper = get_paper_with_score(conn, pid)
        assert paper is not None
        assert paper["title"] == "Unscored Paper"
        assert paper["relevance_score"] is None


class TestPapersFiltered:
    def _seed(self, conn):
        p1 = upsert_paper(conn, doi="10.1101/f1", title="Alpha Paper",
                          authors="Smith", abstract="Cancer immunotherapy trial",
                          source="medrxiv", published_date="2026-02-10")
        save_score(conn, paper_id=p1, relevance_score=0.9, quality_score=0.8,
                   combined_score=0.86, study_design="rct",
                   quality_tier="TIER_4_EXPERIMENTAL", summary="Sum1")

        p2 = upsert_paper(conn, doi="10.1101/f2", title="Beta Paper",
                          authors="Jones", abstract="Genomics cohort study",
                          source="biorxiv", published_date="2026-02-12")
        save_score(conn, paper_id=p2, relevance_score=0.7, quality_score=0.6,
                   combined_score=0.66, study_design="cohort",
                   quality_tier="TIER_3_CONTROLLED", summary="Sum2")

        p3 = upsert_paper(conn, doi="10.1101/f3", title="Gamma Paper",
                          authors="Lee", abstract="Case report on rare disease",
                          source="europepmc", published_date="2026-02-14")
        save_score(conn, paper_id=p3, relevance_score=0.5, quality_score=0.3,
                   combined_score=0.42, study_design="case_report",
                   quality_tier="TIER_1_ANECDOTAL", summary="Sum3")
        return p1, p2, p3

    def test_default_returns_all_sorted_by_combined(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn)
        assert len(results) == 3
        assert results[0]["doi"] == "10.1101/f1"

    def test_sort_by_date(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn, sort="date")
        assert results[0]["doi"] == "10.1101/f3"

    def test_filter_by_source(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn, source="medrxiv")
        assert len(results) == 1
        assert results[0]["doi"] == "10.1101/f1"

    def test_filter_by_quality_tier(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn, quality_tier="TIER_4_EXPERIMENTAL")
        assert len(results) == 1

    def test_search_query(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn, search="immunotherapy")
        assert len(results) == 1
        assert results[0]["doi"] == "10.1101/f1"

    def test_pagination(self):
        conn = _db()
        self._seed(conn)
        page1 = get_papers_filtered(conn, limit=2, offset=0)
        page2 = get_papers_filtered(conn, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 1

    def test_returns_total_count(self):
        conn = _db()
        self._seed(conn)
        results, total = get_papers_filtered(conn, limit=2, offset=0, with_total=True)
        assert len(results) == 2
        assert total == 3


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


class TestMigration003:
    def test_fulltext_columns_exist(self):
        conn = _db()
        columns = [r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()]
        assert "pmid" in columns
        assert "pmcid" in columns
        assert "fulltext_html" in columns
        assert "fulltext_source" in columns

    def test_migration_recorded(self):
        conn = _db()
        from bmlib.db.migrations import get_applied_versions
        versions = get_applied_versions(conn)
        assert 3 in versions


class TestPaperTags:
    def test_save_and_retrieve_tags(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1101/t1", title="Tagged Paper",
                           abstract="A")
        save_paper_tags(conn, paper_id=pid, tags=["AI", "oncology", "clinical trials"])
        tags = get_paper_tags(conn, pid)
        assert set(tags) == {"AI", "oncology", "clinical trials"}

    def test_replace_tags(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1101/t2", title="Re-tagged",
                           abstract="B")
        save_paper_tags(conn, paper_id=pid, tags=["old_tag"])
        save_paper_tags(conn, paper_id=pid, tags=["new_tag1", "new_tag2"])
        tags = get_paper_tags(conn, pid)
        assert "old_tag" not in tags
        assert set(tags) == {"new_tag1", "new_tag2"}

    def test_get_all_tags(self):
        conn = _db()
        p1 = upsert_paper(conn, doi="10.1101/ta1", title="P1", abstract="A")
        p2 = upsert_paper(conn, doi="10.1101/ta2", title="P2", abstract="B")
        save_paper_tags(conn, paper_id=p1, tags=["AI", "genomics"])
        save_paper_tags(conn, paper_id=p2, tags=["AI", "oncology"])
        all_tags = get_all_tags(conn)
        assert set(all_tags) == {"AI", "genomics", "oncology"}

    def test_get_papers_by_tag(self):
        conn = _db()
        p1 = upsert_paper(conn, doi="10.1101/tb1", title="P1", abstract="A")
        p2 = upsert_paper(conn, doi="10.1101/tb2", title="P2", abstract="B")
        save_score(conn, paper_id=p1, combined_score=0.8)
        save_score(conn, paper_id=p2, combined_score=0.7)
        save_paper_tags(conn, paper_id=p1, tags=["AI", "genomics"])
        save_paper_tags(conn, paper_id=p2, tags=["AI", "oncology"])
        papers = get_papers_by_tag(conn, "AI")
        assert len(papers) == 2
        papers = get_papers_by_tag(conn, "genomics")
        assert len(papers) == 1
        assert papers[0]["doi"] == "10.1101/tb1"

    def test_empty_tags(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1101/t_empty", title="No Tags",
                           abstract="C")
        tags = get_paper_tags(conn, pid)
        assert tags == []

    def test_save_empty_tags_clears(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1101/t_clear", title="Clear Tags",
                           abstract="D")
        save_paper_tags(conn, paper_id=pid, tags=["tag1", "tag2"])
        save_paper_tags(conn, paper_id=pid, tags=[])
        tags = get_paper_tags(conn, pid)
        assert tags == []


class TestFulltextOperations:
    def test_save_and_get_fulltext(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1/ft", title="FT Paper")
        save_fulltext(conn, paper_id=pid, html="<p>Full text</p>", source="europepmc")
        save_score(conn, paper_id=pid, combined_score=0.5)
        paper = get_paper_with_score(conn, pid)
        assert paper["fulltext_html"] == "<p>Full text</p>"
        assert paper["fulltext_source"] == "europepmc"

    def test_update_paper_pmid_pmcid(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1/id", title="ID Paper")
        update_paper_identifiers(conn, paper_id=pid, pmid="12345", pmcid="PMC678")
        from bmlib.db import fetch_one
        row = fetch_one(conn, "SELECT pmid, pmcid FROM papers WHERE id = ?", (pid,))
        assert row["pmid"] == "12345"
        assert row["pmcid"] == "PMC678"

    def test_update_paper_pmid_only(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1/id2", title="PMID Only")
        update_paper_identifiers(conn, paper_id=pid, pmid="99999")
        from bmlib.db import fetch_one
        row = fetch_one(conn, "SELECT pmid, pmcid FROM papers WHERE id = ?", (pid,))
        assert row["pmid"] == "99999"
        assert row["pmcid"] is None

    def test_update_no_args_is_noop(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1/noop", title="Noop")
        update_paper_identifiers(conn, paper_id=pid)
        from bmlib.db import fetch_one
        row = fetch_one(conn, "SELECT pmid, pmcid FROM papers WHERE id = ?", (pid,))
        assert row["pmid"] is None
