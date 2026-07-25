"""Europe PMC source fetcher.

Europe PMC is not one of bmlib's built-in sources, so bmnews supplies it and
registers it into bmlib's publication registry (see
:mod:`bmnews.fetchers`).  It therefore follows exactly the same calling
convention as every bmlib fetcher::

    fetcher(client, target_date, *, on_record, on_progress=None, **config)

and emits :class:`~bmlib.publications.models.FetchedRecord` objects, so the
pipeline needs no special case for it.

API reference: https://europepmc.org/RestfulWebService
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from bmlib.fulltext.models import FullTextSourceEntry
from bmlib.publications.models import FetchedRecord, FetchResult, SyncProgress

from bmnews.constants import EUROPEPMC_PAGE_SIZE, MAX_FETCH_PAGES

logger = logging.getLogger(__name__)

SOURCE_NAME = "europepmc"
SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

#: Query used when the user has not configured one — every preprint indexed
#: by Europe PMC for the target date.
DEFAULT_QUERY = "SRC:PPR"


def fetch_europepmc(
    client: Any,
    target_date: date,
    *,
    on_record: Callable[[FetchedRecord], None],
    on_progress: Callable[[SyncProgress], None] | None = None,
    query: str = "",
) -> FetchResult:
    """Fetch Europe PMC records first published on *target_date*.

    Args:
        client: An httpx-compatible client supporting ``get(url, params=...)``.
        target_date: The publication date to query for.
        on_record: Callback invoked with each normalised record.
        on_progress: Optional callback invoked after each page.
        query: Europe PMC query string. Defaults to :data:`DEFAULT_QUERY`
            (all preprints) when empty.

    Returns:
        A :class:`FetchResult` summarising the day. Records already handed to
        *on_record* are kept even when a later page fails.
    """
    date_str = target_date.isoformat()
    base_query = query or DEFAULT_QUERY
    date_query = f"({base_query}) AND (FIRST_PDATE:[{date_str} TO {date_str}])"

    cursor_mark = "*"
    total_fetched = 0
    records_total: int | None = None

    try:
        for _page in range(MAX_FETCH_PAGES):
            params = {
                "query": date_query,
                "format": "json",
                "pageSize": EUROPEPMC_PAGE_SIZE,
                "resultType": "core",
                "cursorMark": cursor_mark,
            }
            logger.debug("Fetching EuropePMC: %s", date_query)

            response = client.get(SEARCH_URL, params=params)
            response.raise_for_status()
            data = response.json()

            if records_total is None:
                records_total = int(data.get("hitCount", 0) or 0)

            results = data.get("resultList", {}).get("result", [])
            if not results:
                break

            for item in results:
                record = _normalize(item)
                if record is None:
                    continue
                on_record(record)
                total_fetched += 1

            if on_progress is not None:
                on_progress(
                    SyncProgress(
                        source=SOURCE_NAME,
                        date=date_str,
                        records_processed=total_fetched,
                        records_total=records_total or total_fetched,
                        status="in_progress",
                    )
                )

            next_cursor = data.get("nextCursorMark", "")
            if not next_cursor or next_cursor == cursor_mark:
                break
            cursor_mark = next_cursor
        else:
            logger.warning(
                "Stopped after %d EuropePMC pages for %s — results may be truncated",
                MAX_FETCH_PAGES, date_str,
            )
    except Exception as exc:
        logger.error("Error fetching EuropePMC for %s: %s", date_str, exc)
        return FetchResult(
            source=SOURCE_NAME,
            date=date_str,
            record_count=total_fetched,
            status="failed",
            error=str(exc),
        )

    logger.debug("Fetched %d EuropePMC records for %s", total_fetched, date_str)
    return FetchResult(
        source=SOURCE_NAME,
        date=date_str,
        record_count=total_fetched,
        status="completed",
    )


def _normalize(item: dict) -> FetchedRecord | None:
    """Convert one Europe PMC search hit to a :class:`FetchedRecord`.

    Args:
        item: A single entry from the API's ``resultList.result`` array.

    Returns:
        The normalised record, or None when the hit carries neither a DOI nor
        a PMID and so cannot be stored under a stable key.
    """
    doi = item.get("doi", "")
    pmid = item.get("pmid", "")
    if not doi and not pmid:
        return None

    return FetchedRecord(
        title=item.get("title", ""),
        source=SOURCE_NAME,
        # Empty optional fields are sent as None (not ""), matching bmlib's
        # own fetchers, so a later merge from another source can fill them in.
        doi=doi or None,
        pmid=pmid or None,
        pmc_id=item.get("pmcid") or None,
        abstract=item.get("abstractText") or None,
        authors=_split_authors(item.get("authorString", "")),
        journal=item.get("journalTitle") or None,
        publication_date=item.get("firstPublicationDate") or None,
        keywords=_keywords(item),
        publication_types=list(item.get("pubTypeList", {}).get("pubType", [])),
        is_open_access=item.get("isOpenAccess", "N") == "Y",
        license=item.get("license") or None,
        fulltext_sources=_fulltext_sources(item),
        extras={
            "europepmc_source": item.get("source", ""),
            "cited_by": item.get("citedByCount", 0),
            "url": _build_url(doi, pmid),
        },
    )


def _split_authors(authors_str: str) -> list[str]:
    """Split Europe PMC's comma-separated ``authorString`` into names."""
    if not authors_str:
        return []
    return [a.strip() for a in authors_str.rstrip(".").split(",") if a.strip()]


def _keywords(item: dict) -> list[str]:
    """Collect subject keywords for a hit, ignoring malformed entries."""
    raw = item.get("keywordList", {})
    if not isinstance(raw, dict):
        return []
    return [k.strip() for k in raw.get("keyword", []) if isinstance(k, str) and k.strip()]


def _build_url(doi: str, pmid: str) -> str:
    """Build the best available canonical URL for a hit."""
    if doi:
        return f"https://doi.org/{doi}"
    if pmid:
        return f"https://europepmc.org/article/med/{pmid}"
    return ""


def _fulltext_sources(item: dict) -> list[FullTextSourceEntry]:
    """Extract free full-text source URLs from Europe PMC's fullTextUrlList."""
    sources: list[FullTextSourceEntry] = []
    url_list = item.get("fullTextUrlList")
    if not isinstance(url_list, dict):
        return sources
    for entry in url_list.get("fullTextUrl", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("availability") != "Free":
            continue
        url = entry.get("url", "")
        style = entry.get("documentStyle", "")
        if not url or not style:
            continue
        fmt = {"pdf": "pdf", "html": "html", "doi": "html"}.get(style)
        if fmt:
            sources.append(FullTextSourceEntry(
                url=url,
                format=fmt,
                source=SOURCE_NAME,
                open_access=True,
            ))
    return sources
