"""Tests for bmnews.fetchers."""

from __future__ import annotations

from datetime import date

import pytest
from bmlib.publications import get_fetcher, source_names
from bmlib.publications.models import FetchedRecord

from bmnews.fetchers import EUROPEPMC, register_local_sources
from bmnews.fetchers.europepmc import fetch_europepmc


class _FakeResponse:
    """Minimal stand-in for an httpx response."""

    def __init__(self, payload: dict, error: Exception | None = None):
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Returns queued responses and records the params it was called with."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None):
        self.calls.append(params or {})
        return self._responses.pop(0)


def _hit(**overrides) -> dict:
    """Build a Europe PMC search hit with sensible defaults."""
    hit = {
        "doi": "10.1234/abc",
        "pmid": "111",
        "pmcid": "PMC222",
        "title": "A Trial",
        "authorString": "Smith J, Jones A.",
        "abstractText": "Findings.",
        "journalTitle": "The Journal",
        "firstPublicationDate": "2026-02-10",
        "pubTypeList": {"pubType": ["Randomized Controlled Trial"]},
        "keywordList": {"keyword": ["Oncology", " Immunotherapy "]},
        "isOpenAccess": "Y",
        "license": "cc-by",
        "citedByCount": 4,
        "source": "MED",
    }
    hit.update(overrides)
    return hit


def _page(results, next_cursor="") -> dict:
    """Build a Europe PMC search response page."""
    return {
        "hitCount": len(results),
        "resultList": {"result": results},
        "nextCursorMark": next_cursor,
    }


class TestEuropePMCRegistration:
    """Europe PMC must be reachable through bmlib's registry, not a side door."""

    def test_source_is_registered(self):
        register_local_sources()
        assert EUROPEPMC in source_names()

    def test_registry_returns_the_local_fetcher(self):
        register_local_sources()
        assert get_fetcher(EUROPEPMC) is fetch_europepmc

    def test_registration_is_idempotent(self):
        register_local_sources()
        register_local_sources()
        assert source_names().count(EUROPEPMC) == 1

    def test_builtin_sources_survive_registration(self):
        register_local_sources()
        for builtin in ("pubmed", "medrxiv", "biorxiv", "openalex"):
            assert builtin in source_names()


class TestFetchEuropePMC:
    def _fetch(self, responses, **kwargs):
        """Run the fetcher against canned responses, returning records + result."""
        client = _FakeClient(responses)
        records: list[FetchedRecord] = []
        result = fetch_europepmc(
            client,
            date(2026, 2, 10),
            on_record=records.append,
            **kwargs,
        )
        return records, result, client

    def test_emits_normalised_records(self):
        records, result, _ = self._fetch([_FakeResponse(_page([_hit()]))])

        assert result.status == "completed"
        assert result.record_count == 1
        record = records[0]
        assert record.source == "europepmc"
        assert record.doi == "10.1234/abc"
        assert record.pmid == "111"
        assert record.pmc_id == "PMC222"
        assert record.authors == ["Smith J", "Jones A"]
        assert record.journal == "The Journal"
        assert record.publication_types == ["Randomized Controlled Trial"]
        assert record.keywords == ["Oncology", "Immunotherapy"]
        assert record.is_open_access is True
        assert record.license == "cc-by"

    def test_absent_fields_are_none_not_empty_string(self):
        """bmlib's storage merge uses COALESCE — "" would block a later fill-in."""
        records, _, _ = self._fetch(
            [
                _FakeResponse(
                    _page(
                        [
                            _hit(
                                abstractText="",
                                journalTitle="",
                                pmcid="",
                                license="",
                            )
                        ]
                    )
                )
            ]
        )

        record = records[0]
        assert record.abstract is None
        assert record.journal is None
        assert record.pmc_id is None
        assert record.license is None

    def test_query_is_scoped_to_the_target_date(self):
        _, _, client = self._fetch([_FakeResponse(_page([]))], query="cancer")

        query = client.calls[0]["query"]
        assert "(cancer)" in query
        assert "FIRST_PDATE:[2026-02-10 TO 2026-02-10]" in query

    def test_default_query_is_preprints(self):
        _, _, client = self._fetch([_FakeResponse(_page([]))])
        assert "(SRC:PPR)" in client.calls[0]["query"]

    def test_hit_without_identifiers_is_skipped(self):
        records, result, _ = self._fetch(
            [_FakeResponse(_page([_hit(doi="", pmid="")]))],
        )
        assert records == []
        assert result.record_count == 0

    def test_pmid_only_hit_is_kept(self):
        records, _, _ = self._fetch([_FakeResponse(_page([_hit(doi="")]))])
        assert records[0].doi is None
        assert records[0].pmid == "111"
        assert records[0].extras["url"] == "https://europepmc.org/article/med/111"

    def test_follows_the_cursor_across_pages(self):
        records, result, client = self._fetch(
            [
                _FakeResponse(_page([_hit(doi="10.1/a")], next_cursor="NEXT")),
                _FakeResponse(_page([_hit(doi="10.1/b")], next_cursor="NEXT")),
            ]
        )

        # Second page repeats the cursor, which ends pagination.
        assert result.record_count == 2
        assert [r.doi for r in records] == ["10.1/a", "10.1/b"]
        assert client.calls[1]["cursorMark"] == "NEXT"

    def test_http_failure_keeps_records_already_emitted(self):
        records, result, _ = self._fetch(
            [
                _FakeResponse(_page([_hit()], next_cursor="NEXT")),
                _FakeResponse({}, error=RuntimeError("boom")),
            ]
        )

        assert result.status == "failed"
        assert result.error == "boom"
        assert result.record_count == 1
        assert len(records) == 1

    def test_free_fulltext_sources_are_extracted(self):
        hit = _hit(
            fullTextUrlList={
                "fullTextUrl": [
                    {"availability": "Free", "documentStyle": "pdf", "url": "http://x/p.pdf"},
                    {"availability": "Subscription", "documentStyle": "pdf", "url": "http://y"},
                    {"availability": "Free", "documentStyle": "unknown", "url": "http://z"},
                ]
            }
        )
        records, _, _ = self._fetch([_FakeResponse(_page([hit]))])

        sources = records[0].fulltext_sources
        assert len(sources) == 1
        assert sources[0].url == "http://x/p.pdf"
        assert sources[0].format == "pdf"

    def test_progress_callback_is_invoked_per_page(self):
        client = _FakeClient([_FakeResponse(_page([_hit()]))])
        progress = []
        fetch_europepmc(
            client,
            date(2026, 2, 10),
            on_record=lambda _r: None,
            on_progress=progress.append,
        )

        assert len(progress) == 1
        assert progress[0].source == "europepmc"
        assert progress[0].records_processed == 1


class TestFetcherSignature:
    """Every source must be callable the same way, bmnews-supplied or not."""

    @pytest.mark.parametrize(
        "name",
        ["europepmc", "pubmed", "medrxiv", "biorxiv", "openalex"],
    )
    def test_accepts_the_registry_calling_convention(self, name):
        import inspect

        register_local_sources()
        signature = inspect.signature(get_fetcher(name))
        params = signature.parameters

        assert "on_record" in params
        assert params["on_record"].kind is inspect.Parameter.KEYWORD_ONLY
        assert "on_progress" in params
