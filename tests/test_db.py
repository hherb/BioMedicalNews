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
from bmlib.fulltext import FullTextCache
from bmlib.fulltext.cache import sanitize_identifier
from bmlib.transparency import TransparencyRisk

from bmnews.db import migrations
from bmnews.db.migrations import MIGRATIONS
from bmnews.db.operations import (
    count_notifications,
    get_all_tags,
    get_cached_digest_papers,
    get_fulltext_sources,
    get_notification_candidates,
    get_paper_by_doi,
    get_paper_metadata,
    get_paper_tags,
    get_paper_with_score,
    get_papers_by_tag,
    get_papers_filtered,
    get_papers_for_digest,
    get_scored_papers,
    get_transparency_candidates,
    get_transparency_results,
    get_unscored_papers,
    paper_exists,
    record_digest,
    record_notification,
    record_notifications,
    save_fulltext,
    save_paper_metadata,
    save_paper_tags,
    save_score,
    save_transparency,
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
        # ...bmnews owns scoring, tagging, digests, notifications and its own extras.
        assert table_exists(conn, "scores")
        assert table_exists(conn, "digests")
        assert table_exists(conn, "digest_papers")
        assert table_exists(conn, "paper_tags")
        assert table_exists(conn, "paper_extras")
        assert table_exists(conn, "notifications")
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
        assert {1, 2, 3, 4, 5} <= versions

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

    def test_pdf_url_is_kept_beside_the_text(self):
        """Text extracted from a PDF keeps a pointer back to the original."""
        conn = _db()
        pid = store_paper(conn, doi="10.1/pdf", title="PDF Paper")
        save_fulltext(
            conn,
            paper_id=pid,
            html="<p>Extracted.</p>",
            source="medrxiv",
            pdf_url="https://medrxiv.org/paper.full.pdf",
        )
        paper = get_paper_with_score(conn, pid)
        assert paper["fulltext_html"] == "<p>Extracted.</p>"
        assert paper["fulltext_pdf_url"] == "https://medrxiv.org/paper.full.pdf"

    def test_pdf_url_defaults_to_empty(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1/nopdf", title="No PDF")
        save_fulltext(conn, paper_id=pid, html="<p>JATS.</p>", source="europepmc")
        assert get_paper_with_score(conn, pid)["fulltext_pdf_url"] == ""

    def test_replacing_the_text_clears_a_stale_pdf_url(self):
        """A later retrieval without a PDF must not leave the old link behind."""
        conn = _db()
        pid = store_paper(conn, doi="10.1/stale", title="Stale")
        save_fulltext(
            conn,
            paper_id=pid,
            html="<p>From PDF.</p>",
            source="medrxiv",
            pdf_url="https://medrxiv.org/old.pdf",
        )
        save_fulltext(conn, paper_id=pid, html="<p>From JATS.</p>", source="europepmc")
        assert get_paper_with_score(conn, pid)["fulltext_pdf_url"] == ""

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


# ---------------------------------------------------------------------------
# Migration 5: notification delivery records
# ---------------------------------------------------------------------------


class TestNotificationsTable:
    """The schema the derived pending queue and per-channel retries rest on."""

    def _seed(self, conn):
        pid = store_paper(conn, doi="10.1/notified", title="Notified")
        save_score(conn, paper_id=pid, combined_score=0.9)
        return pid

    def _record(self, conn, pid, *, watch="w", channel="mail", status="sent"):
        ph = placeholder(conn)
        execute(
            conn,
            "INSERT INTO notifications (watch, paper_id, channel, status)"
            f" VALUES ({ph}, {ph}, {ph}, {ph})",
            (watch, pid, channel, status),
        )
        conn.commit()

    def test_a_delivery_can_be_recorded(self):
        conn = _db()
        self._record(conn, self._seed(conn))
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM notifications") == 1

    def test_defaults_fill_in_attempts_error_and_timestamp(self):
        conn = _db()
        self._record(conn, self._seed(conn))
        row = fetch_one(conn, "SELECT attempts, error, sent_at FROM notifications")
        assert row["attempts"] == 1
        assert row["error"] == ""
        assert row["sent_at"]

    def test_the_same_watch_paper_and_channel_cannot_be_recorded_twice(self):
        """The unique key is what makes a retry an upsert rather than a duplicate."""
        conn = _db()
        pid = self._seed(conn)
        self._record(conn, pid)
        with pytest.raises(Exception):  # noqa: B017 — the driver's own IntegrityError
            self._record(conn, pid)
        conn.rollback()

    def test_one_paper_can_be_delivered_on_two_channels(self):
        """Retry state is per-channel: Matrix can succeed while email fails."""
        conn = _db()
        pid = self._seed(conn)
        self._record(conn, pid, channel="mail", status="failed")
        self._record(conn, pid, channel="matrix", status="sent")
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM notifications") == 2

    def test_two_watches_can_deliver_the_same_paper(self):
        conn = _db()
        pid = self._seed(conn)
        self._record(conn, pid, watch="a")
        self._record(conn, pid, watch="b")
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM notifications") == 2

    def test_removing_the_publication_removes_its_notifications(self):
        conn = _db()
        pid = self._seed(conn)
        self._record(conn, pid)

        ph = placeholder(conn)
        execute(conn, f"DELETE FROM publications WHERE id = {ph}", (pid,))
        conn.commit()

        assert fetch_scalar(conn, "SELECT COUNT(*) FROM notifications") == 0

    def test_notifying_a_paper_leaves_it_eligible_for_the_digest(self):
        """Deliveries must not reuse digest_papers, or an alert would suppress
        the digest entry for that paper. A notification is "now"; the digest is
        the record, and a paper belongs in both."""
        conn = _db()
        pid = self._seed(conn)
        self._record(conn, pid)

        selected = get_papers_for_digest(conn, min_combined=0.5)
        assert [p["id"] for p in selected] == [pid]


# ---------------------------------------------------------------------------
# Migration 6: the PDF behind a retrieved full text
# ---------------------------------------------------------------------------


def _v5_db():
    """A database at schema version 5 — before the PDF column existed."""
    conn = new_db()
    run_migrations(conn, MIGRATIONS[:5])
    return conn


class TestMigrationFulltextPdfUrl:
    """Migration 6 adds the PDF column and drops abstract-only full texts.

    Before it, a body-less JATS document from a preprint server was stored as
    though it were full text. Those rows must be cleared so the next request
    fetches the real article, without disturbing anything else.
    """

    @pytest.fixture(autouse=True)
    def cache(self, tmp_path, monkeypatch):
        """Point the migration's cache purge at a throwaway directory.

        Autouse because the purge runs for every seeded medrxiv row, and the
        default cache directory is the developer's real one.
        """
        cache = FullTextCache(tmp_path / "fulltext_cache")
        monkeypatch.setattr(migrations, "FullTextCache", lambda: cache)
        return cache

    def _seed(self, conn, doi, source, html="<p>cached</p>", metadata="{}"):
        """Store a publication with cached full text, returning its id."""
        publication_id = store_paper(conn, doi=doi, title=f"Paper {doi}")
        ph = placeholder(conn)
        execute(
            conn,
            "INSERT INTO paper_extras (publication_id, metadata_json,"
            f" fulltext_html, fulltext_source) VALUES ({', '.join([ph] * 4)})",
            (publication_id, metadata, html, source),
        )
        conn.commit()
        return publication_id

    def _fulltext(self, conn, publication_id):
        ph = placeholder(conn)
        row = fetch_one(
            conn,
            f"SELECT fulltext_html, fulltext_source FROM paper_extras WHERE publication_id = {ph}",
            (publication_id,),
        )
        return row["fulltext_html"], row["fulltext_source"]

    def _pdf_url_column_works(self, conn):
        """Whether the new column is readable through the normal paper query.

        Asserted through the public path rather than by introspecting the
        catalogue: every paper query selects ``e.fulltext_pdf_url``, so a
        column that is missing breaks all of them — and the check then reads
        the same on SQLite and PostgreSQL.

        The shared paper query also joins the migration-7 ``transparency``
        table now, so this helper — which runs against a connection
        deliberately frozen at schema version 6 — applies migration 7 too.
        That table is not what this test is about; it just has to exist for
        the "normal paper query" to run at all.
        """
        run_migrations(conn, MIGRATIONS[6:7])
        pid = store_paper(conn, doi="10.1/column-probe", title="Probe")
        save_fulltext(
            conn,
            paper_id=pid,
            html="<p>x</p>",
            source="europepmc",
            pdf_url="https://example.org/probe.pdf",
        )
        return get_paper_with_score(conn, pid)["fulltext_pdf_url"]

    def test_adds_the_column(self):
        conn = _v5_db()
        run_migrations(conn, MIGRATIONS[5:6])

        assert self._pdf_url_column_works(conn) == "https://example.org/probe.pdf"

    def test_clears_preprint_server_full_text(self):
        conn = _v5_db()
        med = self._seed(conn, "10.1/med", "medrxiv")
        bio = self._seed(conn, "10.1/bio", "biorxiv")
        run_migrations(conn, MIGRATIONS[5:6])

        assert self._fulltext(conn, med) == (None, "")
        assert self._fulltext(conn, bio) == (None, "")

    def test_leaves_other_sources_alone(self):
        """Europe PMC full text and link markers were never affected."""
        conn = _v5_db()
        epmc = self._seed(conn, "10.1/epmc", "europepmc")
        pdf = self._seed(conn, "10.1/pdf", "unpaywall_pdf", html="http://x/p.pdf")
        pub = self._seed(conn, "10.1/pub", "publisher_url", html="http://x/paper")
        run_migrations(conn, MIGRATIONS[5:6])

        assert self._fulltext(conn, epmc) == ("<p>cached</p>", "europepmc")
        assert self._fulltext(conn, pdf) == ("http://x/p.pdf", "unpaywall_pdf")
        assert self._fulltext(conn, pub) == ("http://x/paper", "publisher_url")

    def test_keeps_the_rest_of_the_row(self):
        """Clearing the text must not disturb the metadata beside it."""
        conn = _v5_db()
        pid = self._seed(conn, "10.1/meta", "medrxiv", metadata='{"cited_by": 9}')
        run_migrations(conn, MIGRATIONS[5:6])

        ph = placeholder(conn)
        row = fetch_one(
            conn,
            f"SELECT metadata_json FROM paper_extras WHERE publication_id = {ph}",
            (pid,),
        )
        assert json.loads(row["metadata_json"]) == {"cited_by": 9}

    def test_is_a_no_op_on_an_empty_database(self):
        conn = _v5_db()
        run_migrations(conn, MIGRATIONS[5:6])

        assert fetch_scalar(conn, "SELECT COUNT(*) FROM paper_extras") == 0

    def test_clearing_a_row_also_purges_the_disk_cache(self, cache):
        """Clearing the row alone would change nothing a reader can see.

        bmlib consults its disk cache *before* the database, so the next
        request would be served the same abstract-only file — and bmnews would
        store it again under the ``cached`` source name, out of reach of this
        migration's filter. The file has to go with the row.
        """
        conn = _v5_db()
        self._seed(conn, "10.1/med", "medrxiv")
        self._seed(conn, "10.1/epmc", "europepmc")
        cache.save_html("<p>abstract only</p>", sanitize_identifier("10.1/med"))
        cache.save_html("<p>real body</p>", sanitize_identifier("10.1/epmc"))

        run_migrations(conn, MIGRATIONS[5:6])

        assert cache.get_html(sanitize_identifier("10.1/med")) is None
        assert cache.get_html(sanitize_identifier("10.1/epmc")) == "<p>real body</p>"

    def test_an_unusable_cache_does_not_abort_the_migration(self, monkeypatch):
        """A cache that cannot be opened must not cost the schema change."""
        conn = _v5_db()
        pid = self._seed(conn, "10.1/med", "medrxiv")

        def unusable():
            raise OSError("cache directory is not writable")

        monkeypatch.setattr(migrations, "FullTextCache", unusable)

        run_migrations(conn, MIGRATIONS[5:6])

        assert self._fulltext(conn, pid) == (None, "")
        assert self._pdf_url_column_works(conn) == "https://example.org/probe.pdf"

    def test_a_paper_without_a_doi_is_skipped(self):
        """Nothing was ever cached for it — the cache is keyed on the DOI."""
        conn = _v5_db()
        pid = store_paper(conn, pmid="12345678", title="PMID only", source="medrxiv")
        ph = placeholder(conn)
        execute(
            conn,
            "INSERT INTO paper_extras (publication_id, metadata_json,"
            f" fulltext_html, fulltext_source) VALUES ({', '.join([ph] * 4)})",
            (pid, "{}", "<p>x</p>", "medrxiv"),
        )
        conn.commit()

        run_migrations(conn, MIGRATIONS[5:6])

        assert self._fulltext(conn, pid) == (None, "")


class TestNullTextColumns:
    """A NULL text column must reach callers as a string, not ``None``.

    ``publications`` leaves most text columns nullable, and sources do omit
    them — an abstract-less record is common enough. Callers ask for these
    with ``paper.get("abstract", "")``, which does *not* protect them: the
    key is present with a ``None`` value, so the default never applies and
    the ``None`` travels on until something subscripts it.
    """

    def _paper_without_abstract(self, conn):
        """Store a publication whose abstract column is SQL NULL."""
        pid = store_paper(conn, doi="10.1/noabs", title="No Abstract Paper")
        ph = placeholder(conn)
        execute(conn, f"UPDATE publications SET abstract = NULL WHERE id = {ph}", (pid,))
        conn.commit()
        return pid

    def test_null_abstract_reads_back_as_empty_string(self):
        conn = _db()
        pid = self._paper_without_abstract(conn)
        save_score(conn, paper_id=pid, combined_score=0.5)

        assert get_paper_with_score(conn, pid)["abstract"] == ""

    def test_unscored_papers_carry_a_string_abstract(self):
        """The scorer reads papers through this query, so it must be safe too."""
        conn = _db()
        self._paper_without_abstract(conn)

        papers = get_unscored_papers(conn)
        assert len(papers) == 1
        assert papers[0]["abstract"] == ""
        # The value must survive the idiom callers actually use.
        assert papers[0].get("abstract", "")[:10] == ""

    def test_other_nullable_text_columns_are_also_strings(self):
        conn = _db()
        pid = self._paper_without_abstract(conn)
        save_score(conn, paper_id=pid, combined_score=0.5)

        paper = get_paper_with_score(conn, pid)
        for column in ("abstract", "journal", "license"):
            assert paper[column] == "", f"{column} came back as {paper[column]!r}"

    def test_absent_identifiers_stay_none(self):
        """For identifiers, "not present" is distinct from "empty"."""
        conn = _db()
        pid = store_paper(conn, pmid="12345678", title="PMID only", source="pubmed")
        save_score(conn, paper_id=pid, combined_score=0.5)

        paper = get_paper_with_score(conn, pid)
        assert paper["doi"] is None
        assert paper["pmcid"] is None

    def test_a_real_abstract_is_untouched(self):
        conn = _db()
        pid = store_paper(conn, doi="10.1/abs", title="Has One", abstract="Real text.")
        save_score(conn, paper_id=pid, combined_score=0.5)

        assert get_paper_with_score(conn, pid)["abstract"] == "Real text."


class TestNotifications:
    """Watch delivery recording, and the derived pending queue behind it.

    The queue is *derived*, not stored: candidates are "papers this watch
    matches now, minus those already sent over this channel". These tests pin
    the SQL half of that — the score floors, the tier exclusion, the
    not-already-sent anti-join and the ordering. The Python half lives in
    ``tests/test_notify.py``.
    """

    def _scored(
        self,
        conn,
        doi: str,
        *,
        combined: float = 0.9,
        relevance: float = 0.9,
        tier: str = "TIER_2_STRONG",
        tags: list[str] | None = None,
    ) -> int:
        paper_id = store_paper(conn, doi=doi, title=f"Paper {doi}", source="medrxiv")
        save_score(
            conn,
            paper_id=paper_id,
            relevance_score=relevance,
            combined_score=combined,
            quality_tier=tier,
        )
        if tags:
            save_paper_tags(conn, paper_id=paper_id, tags=tags)
        return paper_id

    def test_candidates_exclude_already_sent(self):
        conn = _db()
        kept = self._scored(conn, "10.1/kept")
        sent = self._scored(conn, "10.1/sent")
        record_notification(conn, watch="w", paper_id=sent, channel="c", status="sent")

        got = get_notification_candidates(conn, watch="w", channel="c", limit=10)
        assert [p["id"] for p in got] == [kept]

    def test_candidates_include_failed(self):
        """A failed delivery stays in the queue, which is how it gets retried."""
        conn = _db()
        failed = self._scored(conn, "10.1/failed")
        record_notification(
            conn, watch="w", paper_id=failed, channel="c", status="failed", error="smtp down"
        )

        got = get_notification_candidates(conn, watch="w", channel="c", limit=10)
        assert [p["id"] for p in got] == [failed]

    def test_sent_on_another_channel_does_not_exclude(self):
        """Retry state is per-channel or it is wrong."""
        conn = _db()
        paper_id = self._scored(conn, "10.1/split")
        record_notification(conn, watch="w", paper_id=paper_id, channel="mail", status="sent")

        assert [p["id"] for p in get_notification_candidates(conn, watch="w", channel="mail")] == []
        assert [p["id"] for p in get_notification_candidates(conn, watch="w", channel="chat")] == [
            paper_id
        ]

    def test_sent_for_another_watch_does_not_exclude(self):
        conn = _db()
        paper_id = self._scored(conn, "10.1/otherwatch")
        record_notification(conn, watch="other", paper_id=paper_id, channel="c", status="sent")

        got = get_notification_candidates(conn, watch="w", channel="c")
        assert [p["id"] for p in got] == [paper_id]

    def test_candidates_respect_score_floors(self):
        conn = _db()
        self._scored(conn, "10.1/lowcombined", combined=0.3, relevance=0.9)
        self._scored(conn, "10.1/lowrelevance", combined=0.9, relevance=0.3)
        wanted = self._scored(conn, "10.1/high", combined=0.9, relevance=0.9)

        got = get_notification_candidates(
            conn, watch="w", channel="c", min_combined=0.5, min_relevance=0.5
        )
        assert [p["id"] for p in got] == [wanted]

    def test_candidates_exclude_tiers(self):
        conn = _db()
        self._scored(conn, "10.1/weak", tier="TIER_5_WEAK")
        strong = self._scored(conn, "10.1/strong", tier="TIER_2_STRONG")

        got = get_notification_candidates(
            conn, watch="w", channel="c", exclude_tiers=["TIER_5_WEAK"]
        )
        assert [p["id"] for p in got] == [strong]

    def test_candidates_order_by_combined_desc(self):
        conn = _db()
        low = self._scored(conn, "10.1/low", combined=0.5)
        high = self._scored(conn, "10.1/high", combined=0.95)
        mid = self._scored(conn, "10.1/mid", combined=0.7)

        got = get_notification_candidates(conn, watch="w", channel="c")
        assert [p["id"] for p in got] == [high, mid, low]

    def test_candidates_carry_tags(self):
        """The matcher reads paper["tags"], which no publications column holds."""
        conn = _db()
        tagged = self._scored(conn, "10.1/tagged", tags=["melanoma", "immunotherapy"])
        untagged = self._scored(conn, "10.1/untagged", combined=0.4)

        got = {p["id"]: p for p in get_notification_candidates(conn, watch="w", channel="c")}
        assert sorted(got[tagged]["tags"]) == ["immunotherapy", "melanoma"]
        assert got[untagged]["tags"] == []

    def test_candidates_paginate_by_offset(self):
        conn = _db()
        ids = [self._scored(conn, f"10.1/p{n}", combined=0.9 - n / 100.0) for n in range(5)]

        first = get_notification_candidates(conn, watch="w", channel="c", limit=2)
        second = get_notification_candidates(conn, watch="w", channel="c", limit=2, offset=2)
        assert [p["id"] for p in first] == ids[:2]
        assert [p["id"] for p in second] == ids[2:4]

    def test_candidates_skip_unscored_papers(self):
        """Watches are evaluated after scoring; an unscored paper is not a candidate."""
        conn = _db()
        store_paper(conn, doi="10.1/unscored", title="Not scored yet")
        scored = self._scored(conn, "10.1/scored")

        got = get_notification_candidates(conn, watch="w", channel="c")
        assert [p["id"] for p in got] == [scored]

    def test_record_notification_upserts(self):
        conn = _db()
        paper_id = self._scored(conn, "10.1/retry")
        record_notification(
            conn, watch="w", paper_id=paper_id, channel="c", status="failed", error="boom"
        )
        record_notification(conn, watch="w", paper_id=paper_id, channel="c", status="sent")

        ph = placeholder(conn)
        row = fetch_one(
            conn,
            f"SELECT status, attempts, error FROM notifications WHERE paper_id = {ph}",
            (paper_id,),
        )
        assert row["status"] == "sent"
        assert row["attempts"] == 2
        assert row["error"] == ""
        assert fetch_scalar(conn, "SELECT COUNT(*) FROM notifications") == 1

    def test_record_notification_is_per_channel(self):
        conn = _db()
        paper_id = self._scored(conn, "10.1/twochannels")
        record_notification(conn, watch="w", paper_id=paper_id, channel="mail", status="sent")
        record_notification(
            conn, watch="w", paper_id=paper_id, channel="chat", status="failed", error="no room"
        )

        assert fetch_scalar(conn, "SELECT COUNT(*) FROM notifications") == 2

    def test_candidates_leave_the_fulltext_cache_alone(self):
        """The scan walks the whole queue, so it must not select cached articles.

        `_PAPER_COLUMNS` carries `p.*` plus the GUI's cached full text, which
        runs to hundreds of kilobytes per paper. That is right for one page of
        digest results and wrong for a scan that materialises every candidate.
        """
        conn = _db()
        paper_id = self._scored(conn, "10.1/cached")
        save_fulltext(conn, paper_id=paper_id, html="<p>" + "x" * 5000 + "</p>", source="europepmc")

        paper = get_notification_candidates(conn, watch="w", channel="c")[0]

        assert "fulltext_html" not in paper
        # Still everything the matcher tests and the templates render.
        for key in ("id", "doi", "title", "abstract", "journal", "sources", "url"):
            assert key in paper, f"{key} is needed downstream and was dropped"

    def test_record_notifications_writes_the_batch_atomically(self):
        conn = _db()
        ids = [self._scored(conn, f"10.1/batch{n}") for n in range(3)]

        record_notifications(conn, watch="w", paper_ids=ids, channel="c", status="sent")

        assert count_notifications(conn, watch="w", channel="c") == 3

    def test_record_notifications_rolls_back_as_one(self):
        """A half-written batch would re-deliver under a different transaction key."""
        conn = _db()
        ids = [self._scored(conn, f"10.1/rollback{n}") for n in range(3)]

        with pytest.raises(Exception):
            # The last id references no publication, so the FK rejects it.
            record_notifications(
                conn, watch="w", paper_ids=[*ids, 999_999], channel="c", status="sent"
            )

        assert count_notifications(conn, watch="w", channel="c") == 0

    def test_record_notifications_ignores_an_empty_batch(self):
        conn = _db()
        record_notifications(conn, watch="w", paper_ids=[], channel="c", status="sent")

        assert count_notifications(conn, watch="w") == 0

    def test_count_notifications_filters_by_status(self):
        conn = _db()
        first = self._scored(conn, "10.1/one")
        second = self._scored(conn, "10.1/two")
        record_notification(conn, watch="w", paper_id=first, channel="c", status="sent")
        record_notification(conn, watch="w", paper_id=second, channel="c", status="failed")

        assert count_notifications(conn, watch="w") == 1
        assert count_notifications(conn, watch="w", status="failed") == 1
        assert count_notifications(conn, watch="w", channel="c", status="sent") == 1
        assert count_notifications(conn, watch="w", channel="elsewhere") == 0
        assert count_notifications(conn, watch="nobody") == 0


class TestTransparency:
    """The transparency table: storage, the attempt ceiling, and selection."""

    def _scored_paper(self, conn, *, doi, combined, pmid=None):
        """Store a paper with a score, returning its publication id."""
        paper_id = store_paper(
            conn,
            doi=doi,
            pmid=pmid,
            title=f"Paper {doi}",
            abstract="Abstract",
            source="medrxiv",
        )
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=combined)
        return paper_id

    def test_save_and_read_back(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)

        save_transparency(
            conn,
            paper_id=paper_id,
            transparency_score=82,
            risk_level="low",
            result_json='{"transparency_score": 82}',
        )

        rows = get_transparency_results(conn)
        assert len(rows) == 1
        assert rows[0]["paper_id"] == paper_id
        assert rows[0]["transparency_score"] == 82
        assert rows[0]["risk_level"] == "low"
        assert rows[0]["attempts"] == 1
        assert rows[0]["title"] == "Paper 10.1/a"

    def test_repeat_analysis_increments_attempts(self):
        """The ceiling only binds if a repeat actually counts."""
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)

        for _ in range(3):
            save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")

        rows = get_transparency_results(conn)
        assert len(rows) == 1, "one row per paper, not one per attempt"
        assert rows[0]["attempts"] == 3

    def test_reset_attempts_restarts_the_budget(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")
        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")

        save_transparency(
            conn,
            paper_id=paper_id,
            transparency_score=0,
            risk_level="unknown",
            reset_attempts=True,
        )

        assert get_transparency_results(conn)[0]["attempts"] == 1

    def test_candidates_respect_the_score_gate(self):
        conn = _db()
        high = self._scored_paper(conn, doi="10.1/high", combined=0.9)
        self._scored_paper(conn, doi="10.1/low", combined=0.1)

        rows = get_transparency_candidates(conn, min_combined=0.5)

        assert [r["id"] for r in rows] == [high]

    def test_unscored_papers_are_never_candidates(self):
        """The gate reads combined_score, so a paper without one cannot pass."""
        conn = _db()
        store_paper(conn, doi="10.1/unscored", title="No score", source="medrxiv")

        assert get_transparency_candidates(conn, min_combined=0.0) == []

    def test_determinate_result_leaves_the_queue(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        assert get_transparency_candidates(conn, min_combined=0.0) == []

    def test_unknown_result_retries_until_the_ceiling(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)

        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")
        assert [r["id"] for r in get_transparency_candidates(conn, max_attempts=3)] == [paper_id]
        assert get_transparency_candidates(conn, max_attempts=3)[0]["attempts"] == 1

        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")
        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")

        assert get_transparency_candidates(conn, max_attempts=3) == []

    def test_refresh_reselects_a_determinate_result(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        rows = get_transparency_candidates(conn, min_combined=0.0, refresh=True)

        assert [r["id"] for r in rows] == [paper_id]

    def test_successive_refresh_runs_walk_the_corpus(self):
        """A refresh run has no "not done yet" predicate to narrow it, so
        ordering it by score would hand back the identical top-`limit` papers
        every run — re-spending four to eight requests per paper while the
        rest of the corpus is never reached at all."""
        conn = _db()
        ids = [self._scored_paper(conn, doi=f"10.1/{i}", combined=0.9 - i / 100) for i in range(4)]

        first = get_transparency_candidates(conn, min_combined=0.0, limit=2, refresh=True)
        for row in first:
            save_transparency(conn, paper_id=row["id"], transparency_score=80, risk_level="low")

        second = get_transparency_candidates(conn, min_combined=0.0, limit=2, refresh=True)

        assert [r["id"] for r in second] != [r["id"] for r in first]
        assert sorted([r["id"] for r in first] + [r["id"] for r in second]) == sorted(ids)

    def test_refresh_puts_a_never_analysed_paper_first(self):
        """Pins the explicit ``NULLS FIRST``: SQLite sorts NULLs first in ASC
        and PostgreSQL sorts them last, so without it this passes on one
        backend and silently strands unanalysed papers on the other. The
        unanalysed paper scores *lower* here, so score order cannot produce
        this answer by accident."""
        conn = _db()
        analysed = self._scored_paper(conn, doi="10.1/best", combined=0.95)
        save_transparency(conn, paper_id=analysed, transparency_score=82, risk_level="low")
        never = self._scored_paper(conn, doi="10.1/worst", combined=0.5)

        rows = get_transparency_candidates(conn, min_combined=0.0, refresh=True)

        assert [r["id"] for r in rows] == [never, analysed]

    def test_paper_id_bypasses_the_score_gate(self):
        """The user named this paper; a cost gate for papers nobody reads
        does not apply to one that was asked for by id."""
        conn = _db()
        low = self._scored_paper(conn, doi="10.1/low", combined=0.01)

        rows = get_transparency_candidates(conn, min_combined=0.9, paper_id=low)

        assert [r["id"] for r in rows] == [low]

    def test_candidates_are_ordered_best_score_first(self):
        conn = _db()
        mid = self._scored_paper(conn, doi="10.1/mid", combined=0.6)
        best = self._scored_paper(conn, doi="10.1/best", combined=0.95)

        rows = get_transparency_candidates(conn, min_combined=0.0)

        assert [r["id"] for r in rows] == [best, mid]

    def test_candidates_carry_only_identifying_columns(self):
        """No abstract and no cached full text: this query materialises every
        candidate, so a column it does not need is multiplied by all of them."""
        conn = _db()
        self._scored_paper(conn, doi="10.1/a", pmid="123", combined=0.9)

        row = get_transparency_candidates(conn, min_combined=0.0)[0]

        assert set(row) == {"id", "doi", "pmid", "title", "attempts"}

    def test_results_are_ordered_worst_risk_first(self):
        conn = _db()
        for doi, risk in (("10.1/l", "low"), ("10.1/h", "high"), ("10.1/m", "medium")):
            paper_id = self._scored_paper(conn, doi=doi, combined=0.9)
            save_transparency(conn, paper_id=paper_id, transparency_score=50, risk_level=risk)

        assert [r["risk_level"] for r in get_transparency_results(conn)] == [
            "high",
            "medium",
            "low",
        ]

    def test_unknown_risk_value_is_pinned(self):
        """``get_transparency_candidates`` hardcodes the literal ``'unknown'``
        in SQL rather than reading it from the enum (unlike
        ``bmnews/transparency/service.py``'s ``_UNKNOWN``, which deliberately
        does read it from ``TransparencyRisk.UNKNOWN.value`` so a rename
        upstream cannot silently stop matching). This pins the coupling on
        the SQL side too: if bmlib ever renames the value, this test fails
        loudly in CI instead of new rows silently falling out of the retry
        queue."""
        assert TransparencyRisk.UNKNOWN.value == "unknown"


class TestTransparencyReadPath:
    def test_digest_papers_carry_the_risk_badge(self):
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        papers = get_papers_for_digest(conn, min_combined=0.0)

        assert papers[0]["transparency_risk"] == "low"
        assert papers[0]["transparency_score"] == 82

    def test_unanalysed_paper_reads_as_empty_not_none(self):
        """Templates guard on truthiness, exactly as they do for quality_tier."""
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)

        papers = get_papers_for_digest(conn, min_combined=0.0)

        assert papers[0]["transparency_risk"] == ""

    def test_detail_query_decodes_the_result_blob(self):
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_transparency(
            conn,
            paper_id=paper_id,
            transparency_score=30,
            risk_level="high",
            result_json='{"risk_indicators": ["No COI disclosure found in full text"]}',
        )

        paper = get_paper_with_score(conn, paper_id)

        assert paper["transparency"]["risk_indicators"] == ["No COI disclosure found in full text"]

    def test_detail_query_survives_a_malformed_blob(self):
        """A display surface must not fail to render because of stored junk."""
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_transparency(
            conn,
            paper_id=paper_id,
            transparency_score=0,
            risk_level="unknown",
            result_json="not json at all",
        )

        assert get_paper_with_score(conn, paper_id)["transparency"] == {}

    def test_list_queries_do_not_carry_the_blob(self):
        """Absent means 'not asked for', which must not read as 'analysed and
        empty' — so only the detail query populates it."""
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        papers = get_papers_for_digest(conn, min_combined=0.0)

        assert "transparency" not in papers[0]

    def test_notification_candidates_carry_the_badge_but_not_the_blob(self):
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        papers = get_notification_candidates(conn, watch="w", channel="c")

        assert papers[0]["transparency_risk"] == "low"
        assert "transparency" not in papers[0]
        assert "fulltext_html" not in papers[0]


class TestMigration7:
    def test_creates_the_transparency_table(self):
        # A fresh, unmigrated connection: _db() would apply every migration
        # (including this one) up front, defeating the "not yet" assertion —
        # the same reason _v3_db()/_v5_db() start from new_db() rather than _db().
        conn = new_db()
        run_migrations(conn, MIGRATIONS[:6])
        assert not table_exists(conn, "transparency")

        run_migrations(conn, MIGRATIONS)

        assert table_exists(conn, "transparency")

    def test_is_idempotent(self):
        conn = _db()
        init_db(conn)
        run_migrations(conn, MIGRATIONS)
        assert table_exists(conn, "transparency")
