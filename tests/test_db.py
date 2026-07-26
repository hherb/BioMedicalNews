"""Tests for bmnews.db schema and operations."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import pytest
from bmlib.db import (
    execute,
    fetch_all,
    fetch_one,
    fetch_scalar,
    placeholder,
    run_migrations,
    table_exists,
)

from bmnews.db import migrations
from bmnews.db.migrations import MIGRATIONS
from bmnews.db.operations import (
    get_all_tags,
    get_cached_digest_papers,
    get_fulltext_sources,
    get_paper_by_doi,
    get_paper_metadata,
    get_paper_tags,
    get_paper_with_score,
    get_papers_by_tag,
    get_papers_filtered,
    get_papers_for_digest,
    get_scored_papers,
    get_unscored_papers,
    paper_exists,
    record_digest,
    save_fulltext,
    save_paper_metadata,
    save_paper_tags,
    save_score,
    store_paper,
)
from bmnews.db.schema import init_db
from tests.backends import new_db

# Every test in this module runs once per supported backend. bmnews's SQL is
# backend-specific in several places (JSON array unnesting, LIKE vs ILIKE, the
# per-migration DDL pairs), so a suite that only ever saw SQLite could not
# catch a PostgreSQL-only regression.
pytestmark = pytest.mark.usefixtures("db_backend")


def _db():
    conn = new_db()
    init_db(conn)
    return conn


def _days_ago(days: int) -> str:
    """Return an ISO date *days* before today.

    Tests must not hard-code calendar dates: a fixed "recent" date stops being
    recent and silently turns a passing suite red months later.
    """
    return (date.today() - timedelta(days=days)).isoformat()


class TestSchema:
    def test_init_creates_tables(self):
        conn = _db()
        # bmlib owns the publication records...
        assert table_exists(conn, "publications")
        assert table_exists(conn, "fulltext_sources")
        assert table_exists(conn, "download_days")
        # ...bmnews owns scoring, tagging, digests and its own extras.
        assert table_exists(conn, "scores")
        assert table_exists(conn, "digests")
        assert table_exists(conn, "digest_papers")
        assert table_exists(conn, "paper_tags")
        assert table_exists(conn, "paper_extras")
        assert table_exists(conn, "schema_version")

    def test_papers_table_is_gone(self):
        assert not table_exists(_db(), "papers")

    def test_init_is_idempotent(self):
        conn = _db()
        init_db(conn)  # second call
        assert table_exists(conn, "publications")
        assert table_exists(conn, "paper_tags")

    def test_migrations_recorded(self):
        conn = _db()
        from bmlib.db.migrations import get_applied_versions

        versions = get_applied_versions(conn)
        assert {1, 2, 3, 4} <= versions

    def test_removing_a_publication_takes_its_bmnews_rows_with_it(self):
        """bmlib owns the publications row; nothing of ours may outlive it."""
        conn = _db()
        pid = store_paper(conn, doi="10.1/cascade", title="Doomed", metadata={"cited_by": 1})
        save_score(conn, paper_id=pid, combined_score=0.5)
        save_paper_tags(conn, paper_id=pid, tags=["onc"])
        record_digest(conn, [pid], delivery_method="stdout")

        ph = placeholder(conn)
        execute(conn, f"DELETE FROM publications WHERE id = {ph}", (pid,))
        conn.commit()

        for table in ("scores", "paper_tags", "digest_papers", "paper_extras"):
            column = "publication_id" if table == "paper_extras" else "paper_id"
            assert (
                fetch_scalar(conn, f"SELECT COUNT(*) FROM {table} WHERE {column} = {ph}", (pid,))
                == 0
            ), f"{table} kept a row pointing at a deleted publication"


class TestPapers:
    def test_store_and_retrieve(self):
        conn = _db()
        pid = store_paper(
            conn,
            doi="10.1101/test1",
            title="Test Paper",
            authors=["Smith J", "Doe A"],
            abstract="An abstract.",
            source="medrxiv",
            published_date="2024-01-01",
        )
        assert pid > 0
        assert paper_exists(conn, "10.1101/test1")

        paper = get_paper_by_doi(conn, "10.1101/test1")
        assert paper["title"] == "Test Paper"
        assert paper["authors"] == ["Smith J", "Doe A"]
        assert paper["sources"] == ["medrxiv"]

    def test_store_merges_rather_than_duplicating(self):
        """bmlib fills gaps on re-store but never overwrites what it has."""
        conn = _db()
        first = store_paper(conn, doi="10.1101/upd", title="Original Title")

        again = store_paper(
            conn,
            doi="10.1101/upd",
            title="Updated Title",
            abstract="Now with an abstract",
        )

        assert again == first
        paper = get_paper_by_doi(conn, "10.1101/upd")
        assert paper["title"] == "Original Title"
        assert paper["abstract"] == "Now with an abstract"

    def test_the_same_work_from_two_sources_is_one_paper(self):
        conn = _db()
        first = store_paper(conn, doi="10.1101/xover", title="Shared", source="medrxiv")

        again = store_paper(conn, doi="10.1101/xover", title="Shared", source="europepmc")

        assert again == first
        assert get_paper_by_doi(conn, "10.1101/xover")["sources"] == ["medrxiv", "europepmc"]

    def test_doi_case_and_prefix_do_not_split_a_paper(self):
        conn = _db()
        first = store_paper(conn, doi="10.1101/AbC", title="Cased")

        again = store_paper(conn, doi="https://doi.org/10.1101/abc", title="Cased")

        assert again == first
        assert get_paper_by_doi(conn, "10.1101/abc") is not None

    def test_a_paper_without_a_doi_is_stored(self):
        """The old papers.doi was NOT NULL, so PMID-only records were dropped."""
        conn = _db()
        pid = store_paper(conn, pmid="12345678", title="PMID only", source="pubmed")

        paper = get_paper_with_score(conn, pid)
        assert paper["pmid"] == "12345678"
        assert paper["doi"] is None

    def test_a_paper_with_no_identifier_is_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="doi or pmid"):
            store_paper(_db(), title="Anonymous")

    def test_paper_not_found(self):
        conn = _db()
        assert get_paper_by_doi(conn, "nonexistent") is None
        assert not paper_exists(conn, "nonexistent")

    def test_url_is_derived_from_the_identifiers(self):
        conn = _db()
        by_doi = store_paper(conn, doi="10.1/url", title="By DOI")
        by_pmid = store_paper(conn, pmid="999", title="By PMID")

        assert get_paper_with_score(conn, by_doi)["url"] == "https://doi.org/10.1/url"
        assert get_paper_with_score(conn, by_pmid)["url"] == "https://pubmed.ncbi.nlm.nih.gov/999/"


class TestScores:
    def test_save_and_retrieve(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1101/scored", title="Scored Paper", abstract="Abstract")
        save_score(
            conn,
            paper_id=pid,
            relevance_score=0.8,
            quality_score=0.7,
            combined_score=0.76,
            summary="A good paper.",
            study_design="RCT",
        )
        scored = get_scored_papers(conn, min_combined=0.5)
        assert len(scored) == 1
        assert scored[0]["title"] == "Scored Paper"
        assert scored[0]["combined_score"] == 0.76

    def test_unscored_papers(self):
        conn = _db()
        store_paper(conn, doi="10.1101/a", title="Paper A", abstract="A")
        store_paper(conn, doi="10.1101/b", title="Paper B", abstract="B")
        pid = store_paper(conn, doi="10.1101/c", title="Paper C", abstract="C")
        save_score(conn, paper_id=pid, combined_score=0.5)

        unscored = get_unscored_papers(conn)
        dois = [p["doi"] for p in unscored]
        assert "10.1101/a" in dois
        assert "10.1101/b" in dois
        assert "10.1101/c" not in dois


class TestDigests:
    def test_papers_for_digest_excludes_sent(self):
        conn = _db()
        pid1 = store_paper(conn, doi="10.1101/d1", title="P1", abstract="A1")
        pid2 = store_paper(conn, doi="10.1101/d2", title="P2", abstract="A2")
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
        pid = store_paper(
            conn,
            doi="10.1101/pws1",
            title="PaperWithScore Test",
            authors=["Doe J"],
            abstract="Some abstract.",
            source="medrxiv",
            published_date="2025-06-01",
        )
        save_score(
            conn,
            paper_id=pid,
            relevance_score=0.85,
            quality_score=0.70,
            combined_score=0.78,
            summary="An excellent study.",
            study_design="RCT",
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
        pid = store_paper(
            conn,
            doi="10.1101/pws_unscored",
            title="Unscored Paper",
            abstract="No score here.",
        )
        paper = get_paper_with_score(conn, pid)
        assert paper is not None
        assert paper["title"] == "Unscored Paper"
        assert paper["relevance_score"] is None


class TestPapersFiltered:
    def _seed(self, conn):
        p1 = store_paper(
            conn,
            doi="10.1101/f1",
            title="Alpha Paper",
            authors=["Smith"],
            abstract="Cancer immunotherapy trial",
            source="medrxiv",
            published_date="2026-02-10",
        )
        save_score(
            conn,
            paper_id=p1,
            relevance_score=0.9,
            quality_score=0.8,
            combined_score=0.86,
            study_design="rct",
            quality_tier="TIER_4_EXPERIMENTAL",
            summary="Sum1",
        )

        p2 = store_paper(
            conn,
            doi="10.1101/f2",
            title="Beta Paper",
            authors=["Jones"],
            abstract="Genomics cohort study",
            source="biorxiv",
            published_date="2026-02-12",
        )
        save_score(
            conn,
            paper_id=p2,
            relevance_score=0.7,
            quality_score=0.6,
            combined_score=0.66,
            study_design="cohort",
            quality_tier="TIER_3_CONTROLLED",
            summary="Sum2",
        )

        p3 = store_paper(
            conn,
            doi="10.1101/f3",
            title="Gamma Paper",
            authors=["Lee"],
            abstract="Case report on rare disease",
            source="europepmc",
            published_date="2026-02-14",
        )
        save_score(
            conn,
            paper_id=p3,
            relevance_score=0.5,
            quality_score=0.3,
            combined_score=0.42,
            study_design="case_report",
            quality_tier="TIER_1_ANECDOTAL",
            summary="Sum3",
        )
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

    def test_filter_by_source_matches_inside_the_sources_list(self):
        """A paper seen on two sources is found under either of them."""
        conn = _db()
        store_paper(conn, doi="10.1101/multi", title="Multi", source="medrxiv")
        store_paper(conn, doi="10.1101/multi", title="Multi", source="pubmed")

        assert len(get_papers_filtered(conn, source="medrxiv")) == 1
        assert len(get_papers_filtered(conn, source="pubmed")) == 1
        assert get_papers_filtered(conn, source="biorxiv") == []

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

    def test_search_ignores_case(self):
        """SQLite's LIKE does this for free; PostgreSQL needs ILIKE."""
        conn = _db()
        self._seed(conn)
        assert len(get_papers_filtered(conn, search="IMMUNOTHERAPY")) == 1
        assert len(get_papers_filtered(conn, search="alpha paper")) == 1

    def test_search_matches_the_title_as_well_as_the_abstract(self):
        conn = _db()
        self._seed(conn)
        assert len(get_papers_filtered(conn, search="Gamma")) == 1

    def test_an_unscored_paper_sorts_last_not_first(self):
        """A NULL score must not outrank a real one in a descending sort."""
        conn = _db()
        self._seed(conn)
        store_paper(conn, doi="10.1101/f4", title="Delta Paper", source="medrxiv")

        results = get_papers_filtered(conn, sort="combined")
        assert len(results) == 4
        assert results[-1]["doi"] == "10.1101/f4"

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
        pid1 = store_paper(
            conn, doi="10.1101/c1", title="Cached 1", abstract="A1", published_date="2026-02-10"
        )
        pid2 = store_paper(
            conn,
            doi="10.1101/c2",
            title="Not in digest",
            abstract="A2",
            published_date="2026-02-10",
        )
        save_score(conn, paper_id=pid1, combined_score=0.8, summary="Sum1")
        save_score(conn, paper_id=pid2, combined_score=0.7, summary="Sum2")
        record_digest(conn, [pid1], delivery_method="stdout")

        cached = get_cached_digest_papers(conn)
        assert len(cached) == 1
        assert cached[0]["doi"] == "10.1101/c1"
        assert cached[0]["combined_score"] == 0.8

    def test_a_paper_in_two_digests_is_returned_once(self):
        conn = _db()
        pid = store_paper(
            conn, doi="10.1101/twice", title="Twice", abstract="A", published_date="2026-02-10"
        )
        save_score(conn, paper_id=pid, combined_score=0.8, summary="Sum")
        record_digest(conn, [pid], delivery_method="stdout")
        record_digest(conn, [pid], delivery_method="email")

        assert len(get_cached_digest_papers(conn)) == 1

    def test_filters_by_publication_date(self):
        conn = _db()
        pid_old = store_paper(
            conn, doi="10.1101/old", title="Old Paper", abstract="A", published_date="2020-01-01"
        )
        pid_new = store_paper(
            conn, doi="10.1101/new", title="New Paper", abstract="B", published_date=_days_ago(2)
        )
        save_score(conn, paper_id=pid_old, combined_score=0.8)
        save_score(conn, paper_id=pid_new, combined_score=0.9)
        record_digest(conn, [pid_old, pid_new], delivery_method="stdout")

        cached = get_cached_digest_papers(conn, days=7)
        assert len(cached) == 1
        assert cached[0]["doi"] == "10.1101/new"

    def test_no_days_returns_all_cached(self):
        conn = _db()
        pid_old = store_paper(
            conn, doi="10.1101/old2", title="Old", abstract="A", published_date="2020-01-01"
        )
        pid_new = store_paper(
            conn, doi="10.1101/new2", title="New", abstract="B", published_date=_days_ago(2)
        )
        save_score(conn, paper_id=pid_old, combined_score=0.8)
        save_score(conn, paper_id=pid_new, combined_score=0.9)
        record_digest(conn, [pid_old, pid_new], delivery_method="stdout")

        cached = get_cached_digest_papers(conn)
        assert len(cached) == 2

    def test_empty_when_no_digests(self):
        conn = _db()
        store_paper(conn, doi="10.1101/x", title="X", abstract="A", published_date="2026-02-10")
        cached = get_cached_digest_papers(conn)
        assert cached == []


class TestPaperTags:
    def test_save_and_retrieve_tags(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1101/t1", title="Tagged Paper", abstract="A")
        save_paper_tags(conn, paper_id=pid, tags=["AI", "oncology", "clinical trials"])
        tags = get_paper_tags(conn, pid)
        assert set(tags) == {"AI", "oncology", "clinical trials"}

    def test_replace_tags(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1101/t2", title="Re-tagged", abstract="B")
        save_paper_tags(conn, paper_id=pid, tags=["old_tag"])
        save_paper_tags(conn, paper_id=pid, tags=["new_tag1", "new_tag2"])
        tags = get_paper_tags(conn, pid)
        assert "old_tag" not in tags
        assert set(tags) == {"new_tag1", "new_tag2"}

    def test_get_all_tags(self):
        conn = _db()
        p1 = store_paper(conn, doi="10.1101/ta1", title="P1", abstract="A")
        p2 = store_paper(conn, doi="10.1101/ta2", title="P2", abstract="B")
        save_paper_tags(conn, paper_id=p1, tags=["AI", "genomics"])
        save_paper_tags(conn, paper_id=p2, tags=["AI", "oncology"])
        all_tags = get_all_tags(conn)
        assert set(all_tags) == {"AI", "genomics", "oncology"}

    def test_get_papers_by_tag(self):
        conn = _db()
        p1 = store_paper(conn, doi="10.1101/tb1", title="P1", abstract="A")
        p2 = store_paper(conn, doi="10.1101/tb2", title="P2", abstract="B")
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
        pid = store_paper(conn, doi="10.1101/t_empty", title="No Tags", abstract="C")
        tags = get_paper_tags(conn, pid)
        assert tags == []

    def test_save_empty_tags_clears(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1101/t_clear", title="Clear Tags", abstract="D")
        save_paper_tags(conn, paper_id=pid, tags=["tag1", "tag2"])
        save_paper_tags(conn, paper_id=pid, tags=[])
        tags = get_paper_tags(conn, pid)
        assert tags == []


class TestPaperExtras:
    def test_save_and_get_fulltext(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1/ft", title="FT Paper")
        save_fulltext(conn, paper_id=pid, html="<p>Full text</p>", source="europepmc")
        save_score(conn, paper_id=pid, combined_score=0.5)
        paper = get_paper_with_score(conn, pid)
        assert paper["fulltext_html"] == "<p>Full text</p>"
        assert paper["fulltext_source"] == "europepmc"

    def test_fulltext_can_be_replaced(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1/ft2", title="FT Paper")
        save_fulltext(conn, paper_id=pid, html="<p>First</p>", source="europepmc")
        save_fulltext(conn, paper_id=pid, html="http://x/p.pdf", source="unpaywall_pdf")
        paper = get_paper_with_score(conn, pid)
        assert paper["fulltext_html"] == "http://x/p.pdf"
        assert paper["fulltext_source"] == "unpaywall_pdf"

    def test_metadata_and_fulltext_do_not_clobber_each_other(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1/both", title="Both", metadata={"cited_by": 4})
        save_fulltext(conn, paper_id=pid, html="<p>Text</p>", source="europepmc")

        paper = get_paper_with_score(conn, pid)
        assert paper["metadata"] == {"cited_by": 4}
        assert paper["fulltext_html"] == "<p>Text</p>"

    def test_a_rewritten_key_takes_the_newer_value(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1/meta", title="Meta")
        save_paper_metadata(conn, paper_id=pid, metadata={"cited_by": 1})
        save_paper_metadata(conn, paper_id=pid, metadata={"cited_by": 2})

        assert get_paper_with_score(conn, pid)["metadata"] == {"cited_by": 2}

    def test_a_second_source_adds_keys_without_dropping_the_first(self):
        """One publication can be fed by several sources — neither wins outright."""
        conn = _db()
        pid = store_paper(conn, doi="10.1/two-sources", title="Shared")
        save_paper_metadata(conn, paper_id=pid, metadata={"cited_by": 4})
        save_paper_metadata(conn, paper_id=pid, metadata={"altmetric": 9})

        assert get_paper_with_score(conn, pid)["metadata"] == {
            "cited_by": 4,
            "altmetric": 9,
        }

    def test_an_empty_metadata_write_changes_nothing(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1/empty-meta", title="Meta")
        save_paper_metadata(conn, paper_id=pid, metadata={"cited_by": 4})
        save_paper_metadata(conn, paper_id=pid, metadata={})

        assert get_paper_with_score(conn, pid)["metadata"] == {"cited_by": 4}

    def test_get_paper_metadata_is_empty_for_a_paper_with_no_extras(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1/no-extras", title="Bare")
        assert get_paper_metadata(conn, pid) == {}

    def test_identifiers_are_stored_with_the_paper(self):
        conn = _db()
        pid = store_paper(
            conn,
            doi="10.1/id",
            title="ID Paper",
            pmid="12345",
            pmcid="PMC678",
        )
        paper = get_paper_with_score(conn, pid)
        assert paper["pmid"] == "12345"
        assert paper["pmcid"] == "PMC678"


class TestStoreReturnsCorrectId:
    """Regression tests for the store helper returning a stale row id.

    On the ON CONFLICT DO UPDATE path SQLite leaves ``cursor.lastrowid``
    pointing at the last row actually *inserted*, so re-fetching an existing
    paper used to return another paper's id — and the caller then wrote that
    paper's PMID/PMCID onto the wrong row.
    """

    def test_restore_returns_original_id(self):
        conn = _db()
        first = store_paper(conn, doi="10.1101/a", title="A")
        store_paper(conn, doi="10.1101/b", title="B")
        store_paper(conn, doi="10.1101/c", title="C")

        again = store_paper(conn, doi="10.1101/a", title="A", abstract="Revised")
        assert again == first

        paper = get_paper_by_doi(conn, "10.1101/a")
        assert paper["id"] == first
        assert paper["abstract"] == "Revised"

    def test_restore_does_not_create_duplicate(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1101/dup", title="Dup")
        assert store_paper(conn, doi="10.1101/dup", title="Dup again") == pid

        rows = get_papers_filtered(conn, search="Dup", limit=10)
        assert len(rows) == 1

    def test_identifiers_land_on_the_right_paper(self):
        conn = _db()
        pid_a = store_paper(conn, doi="10.1101/ident-a", title="A")
        pid_b = store_paper(conn, doi="10.1101/ident-b", title="B")

        returned = store_paper(
            conn,
            doi="10.1101/ident-a",
            title="A",
            pmid="111",
            pmcid="PMC111",
        )

        assert get_paper_by_doi(conn, "10.1101/ident-a")["pmid"] == "111"
        assert get_paper_by_doi(conn, "10.1101/ident-b")["pmid"] is None
        assert returned == pid_a != pid_b


class TestRecordDigestId:
    def test_returns_increasing_ids(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1101/d1", title="D1")
        save_score(conn, paper_id=pid, combined_score=0.9)

        first = record_digest(conn, [pid], delivery_method="stdout")
        second = record_digest(conn, [], delivery_method="stdout")
        assert first > 0
        assert second > first


class TestDigestSelectionFilters:
    """min_relevance and min_quality_tier must actually reach the SQL."""

    def _seed(self, conn, doi, *, relevance, combined, tier):
        pid = store_paper(conn, doi=doi, title=f"Paper {doi}")
        save_score(
            conn,
            paper_id=pid,
            relevance_score=relevance,
            combined_score=combined,
            quality_tier=tier,
        )
        return pid

    def _conn(self):
        conn = _db()
        self._seed(conn, "10.1/weak", relevance=0.2, combined=0.7, tier="TIER_1_ANECDOTAL")
        self._seed(conn, "10.1/strong", relevance=0.9, combined=0.9, tier="TIER_4_EXPERIMENTAL")
        self._seed(conn, "10.1/unknown", relevance=0.8, combined=0.8, tier="UNCLASSIFIED")
        return conn

    def _dois(self, papers):
        return {p["doi"] for p in papers}

    def test_no_filters_returns_everything_above_threshold(self):
        papers = get_papers_for_digest(self._conn(), min_combined=0.5)
        assert len(papers) == 3

    def test_min_relevance_excludes_low_relevance_papers(self):
        papers = get_papers_for_digest(
            self._conn(),
            min_combined=0.5,
            min_relevance=0.5,
        )
        assert "10.1/weak" not in self._dois(papers)
        assert "10.1/strong" in self._dois(papers)

    def test_excluded_tiers_are_dropped(self):
        papers = get_papers_for_digest(
            self._conn(),
            min_combined=0.5,
            exclude_tiers=["TIER_1_ANECDOTAL", "TIER_2_OBSERVATIONAL"],
        )
        assert "10.1/weak" not in self._dois(papers)

    def test_unclassified_papers_survive_a_tier_floor(self):
        papers = get_papers_for_digest(
            self._conn(),
            min_combined=0.5,
            exclude_tiers=["TIER_1_ANECDOTAL"],
        )
        assert "10.1/unknown" in self._dois(papers)

    def test_max_papers_still_applies_with_filters(self):
        papers = get_papers_for_digest(
            self._conn(),
            min_combined=0.5,
            min_relevance=0.1,
            exclude_tiers=["TIER_1_ANECDOTAL"],
            max_papers=1,
        )
        assert len(papers) == 1


class TestCountUnscoredPapers:
    def test_counts_only_unscored(self):
        from bmnews.db.operations import count_unscored_papers

        conn = _db()
        assert count_unscored_papers(conn) == 0

        scored = store_paper(conn, doi="10.1/scored", title="Scored")
        store_paper(conn, doi="10.1/pending", title="Pending")
        assert count_unscored_papers(conn) == 2

        save_score(conn, paper_id=scored, combined_score=0.5)
        assert count_unscored_papers(conn) == 1


# ---------------------------------------------------------------------------
# Migration 4: papers → bmlib.publications
# ---------------------------------------------------------------------------


def _v3_db():
    """A database at schema version 3 — the last one with a ``papers`` table."""
    conn = new_db()
    run_migrations(conn, MIGRATIONS[:3])
    return conn


def _insert_v3_paper(conn, doi, title, **kwargs):
    """Insert a row into the pre-migration ``papers`` table.

    ``RETURNING`` rather than ``cursor.lastrowid``: psycopg2 has no such
    attribute, and both backends have supported the clause for years.
    """
    ph = placeholder(conn)
    row = fetch_one(
        conn,
        "INSERT INTO papers (doi, title, authors, abstract, url, source,"
        " published_date, categories, metadata_json, pmid, pmcid,"
        " fulltext_html, fulltext_source)"
        f" VALUES ({', '.join([ph] * 13)}) RETURNING id",
        (
            doi,
            title,
            kwargs.get("authors", ""),
            kwargs.get("abstract", ""),
            kwargs.get("url", ""),
            kwargs.get("source", "medrxiv"),
            kwargs.get("published_date", "2026-01-01"),
            kwargs.get("categories", ""),
            json.dumps(kwargs.get("metadata", {})),
            kwargs.get("pmid"),
            kwargs.get("pmcid"),
            kwargs.get("fulltext_html"),
            kwargs.get("fulltext_source", ""),
        ),
    )
    conn.commit()
    return row["id"]


def _insert_v3_score(conn, paper_id, **kwargs):
    """Insert a row into the pre-migration ``scores`` table."""
    ph = placeholder(conn)
    execute(
        conn,
        "INSERT INTO scores (paper_id, relevance_score, quality_score,"
        " combined_score, summary, study_design, quality_tier)"
        f" VALUES ({', '.join([ph] * 7)})",
        (
            paper_id,
            kwargs.get("relevance_score", 0.0),
            kwargs.get("quality_score", 0.0),
            kwargs.get("combined_score", 0.0),
            kwargs.get("summary", ""),
            kwargs.get("study_design", ""),
            kwargs.get("quality_tier", ""),
        ),
    )
    conn.commit()


class TestMigrationToPublications:
    """Migration 4 must carry a populated v3 database across intact."""

    def _migrated(self):
        """A v3 database with representative content, migrated to v4."""
        conn = _v3_db()
        self.p1 = _insert_v3_paper(
            conn,
            "10.1/a",
            "Paper A",
            authors="Ann Lee; Bo Ng",
            categories="Oncology; Genomics",
            source="medrxiv",
            pmid="111",
            pmcid="PMC1",
            fulltext_html="<p>cached</p>",
            fulltext_source="europepmc",
            metadata={
                "pub_type": ["Journal Article"],
                "journal": "Nature",
                "license": "cc-by",
                "is_open_access": True,
                "cited_by": 7,
                "fulltext_sources": [
                    {"url": "http://x/a.pdf", "format": "pdf", "source": "epmc"},
                ],
            },
        )
        # The same work, differing only in DOI case and source: bmlib's dedupe
        # must collapse these two rows into one publication.
        self.p2 = _insert_v3_paper(conn, "10.1/A", "Paper A again", source="europepmc")
        self.p3 = _insert_v3_paper(conn, "10.1/b", "Paper B", authors="Cy Do")

        _insert_v3_score(conn, self.p1, combined_score=0.5, summary="lower")
        _insert_v3_score(conn, self.p2, combined_score=0.9, summary="higher")
        _insert_v3_score(conn, self.p3, combined_score=0.3, summary="b")

        ph = placeholder(conn)
        for pid in (self.p1, self.p2):
            execute(
                conn,
                f"INSERT INTO paper_tags (paper_id, tag) VALUES ({ph}, {ph})",
                (pid, "onc"),
            )
        execute(conn, "INSERT INTO digests (paper_count, delivery_method) VALUES (2, 'stdout')")
        digest_id = fetch_scalar(conn, "SELECT id FROM digests")
        for pid in (self.p1, self.p2):
            execute(
                conn,
                f"INSERT INTO digest_papers (digest_id, paper_id) VALUES ({ph}, {ph})",
                (digest_id, pid),
            )
        conn.commit()

        run_migrations(conn, MIGRATIONS)
        return conn

    def test_duplicate_papers_collapse_into_one_publication(self):
        conn = self._migrated()
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM publications") == 2

    def test_fields_are_converted_to_their_new_shapes(self):
        conn = self._migrated()
        paper = get_paper_by_doi(conn, "10.1/a")

        assert paper["authors"] == ["Ann Lee", "Bo Ng"]
        assert paper["keywords"] == ["Oncology", "Genomics"]
        assert paper["publication_types"] == ["Journal Article"]
        assert paper["journal"] == "Nature"
        assert paper["license"] == "cc-by"
        assert paper["is_open_access"] is True
        assert paper["pmid"] == "111"
        assert paper["pmcid"] == "PMC1"

    def test_sources_are_unioned_across_the_collapsed_rows(self):
        conn = self._migrated()
        assert get_paper_by_doi(conn, "10.1/a")["sources"] == ["medrxiv", "europepmc"]

    def test_the_surviving_score_is_the_highest_combined(self):
        """scores is UNIQUE(paper_id), so a collapse has to pick one."""
        conn = self._migrated()
        scores = fetch_all(conn, "SELECT paper_id, combined_score, summary FROM scores")

        by_paper = {r["paper_id"]: r for r in scores}
        merged_id = get_paper_by_doi(conn, "10.1/a")["id"]
        assert by_paper[merged_id]["summary"] == "higher"
        assert by_paper[merged_id]["combined_score"] == 0.9

    def test_no_score_tag_or_digest_link_is_orphaned(self):
        conn = self._migrated()
        publication_ids = {r["id"] for r in fetch_all(conn, "SELECT id FROM publications")}

        for table in ("scores", "paper_tags", "digest_papers"):
            referenced = {r["paper_id"] for r in fetch_all(conn, f"SELECT paper_id FROM {table}")}
            assert referenced, f"{table} lost every row"
            assert referenced <= publication_ids, f"{table} references a missing publication"

    def test_tags_and_digest_links_are_unioned_not_duplicated(self):
        conn = self._migrated()
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM paper_tags") == 1
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM digest_papers") == 1

    def test_source_metadata_without_a_column_is_kept(self):
        conn = self._migrated()
        assert get_paper_by_doi(conn, "10.1/a")["metadata"]["cited_by"] == 7

    def test_cached_fulltext_survives(self):
        conn = self._migrated()
        paper = get_paper_by_doi(conn, "10.1/a")
        assert paper["fulltext_html"] == "<p>cached</p>"
        assert paper["fulltext_source"] == "europepmc"

    def test_fulltext_source_urls_move_into_bmlibs_table(self):
        conn = self._migrated()
        sources = get_fulltext_sources(conn, get_paper_by_doi(conn, "10.1/a")["id"])
        assert [s.url for s in sources] == ["http://x/a.pdf"]

    def test_the_papers_table_is_dropped(self):
        assert not table_exists(self._migrated(), "papers")

    def test_migrating_an_empty_database_is_fine(self):
        conn = _v3_db()
        run_migrations(conn, MIGRATIONS)
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM publications") == 0
        assert not table_exists(conn, "papers")

    def test_later_rows_refresh_metadata_rather_than_being_ignored(self):
        """Two rows for one work: the newer reading of a key is the one to keep."""
        conn = _v3_db()
        _insert_v3_paper(conn, "10.1/m", "M", metadata={"cited_by": 1, "journal": "J"})
        _insert_v3_paper(conn, "10.1/M", "M again", metadata={"cited_by": 9})
        run_migrations(conn, MIGRATIONS)

        metadata = get_paper_by_doi(conn, "10.1/m")["metadata"]
        assert metadata["cited_by"] == 9
        # A key the later row said nothing about is not dropped with it.
        assert metadata["journal"] == "J"

    def test_a_later_row_does_not_blank_out_cached_fulltext(self):
        conn = _v3_db()
        _insert_v3_paper(
            conn, "10.1/ft", "FT", fulltext_html="<p>body</p>", fulltext_source="europepmc"
        )
        _insert_v3_paper(conn, "10.1/FT", "FT again")
        run_migrations(conn, MIGRATIONS)

        assert get_paper_by_doi(conn, "10.1/ft")["fulltext_html"] == "<p>body</p>"


class TestMigrationStrandedPapers:
    """``papers`` is dropped, so a row that cannot be carried across is gone."""

    def _migrate_with_a_stranded_row(self, tmp_path, monkeypatch):
        rescue = tmp_path / "stranded.json"
        monkeypatch.setattr(migrations, "STRANDED_PAPERS_PATH", str(rescue))

        conn = _v3_db()
        # papers.doi was NOT NULL, so a blank DOI is the only way to get here.
        _insert_v3_paper(conn, "", "Unidentifiable", abstract="Worth keeping")
        _insert_v3_paper(conn, "10.1/ok", "Fine")
        run_migrations(conn, MIGRATIONS)
        return conn, rescue

    def test_the_rest_of_the_migration_still_completes(self, tmp_path, monkeypatch):
        conn, _ = self._migrate_with_a_stranded_row(tmp_path, monkeypatch)
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM publications") == 1
        assert get_paper_by_doi(conn, "10.1/ok") is not None

    def test_the_row_is_written_out_before_papers_is_dropped(self, tmp_path, monkeypatch):
        _, rescue = self._migrate_with_a_stranded_row(tmp_path, monkeypatch)

        stranded = json.loads(rescue.read_text())
        assert len(stranded) == 1
        assert stranded[0]["title"] == "Unidentifiable"
        assert stranded[0]["abstract"] == "Worth keeping"
        assert stranded[0]["reason"] == "no DOI or PMID"

    def test_it_is_reported_as_an_error_not_a_warning(self, tmp_path, monkeypatch, caplog):
        """A GUI session never shows a warning; losing data has to be an error."""
        with caplog.at_level(logging.ERROR, logger="bmnews.db.migrations"):
            self._migrate_with_a_stranded_row(tmp_path, monkeypatch)

        assert any("could not be migrated" in r.message for r in caplog.records)

    def test_an_unwritable_rescue_path_does_not_abort_the_migration(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(
            migrations,
            "STRANDED_PAPERS_PATH",
            str(tmp_path / "nope.txt" / "x.json"),
        )
        (tmp_path / "nope.txt").write_text("not a directory")

        conn = _v3_db()
        _insert_v3_paper(conn, "", "Unidentifiable")
        _insert_v3_paper(conn, "10.1/ok", "Fine")
        run_migrations(conn, MIGRATIONS)

        assert fetch_scalar(conn, "SELECT COUNT(*) FROM publications") == 1
