"""Fetcher for Europe PMC.

Uses the Europe PMC REST API:
https://europepmc.org/RestfulWebService
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from bmnews.fetchers.base import FetchedPaper

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PAGE_SIZE = 100


def fetch_europepmc(
    query: str = "",
    lookback_days: int = 7,
    timeout: float = 30.0,
) -> list[FetchedPaper]:
    """Fetch recent publications from Europe PMC.

    If *query* is empty, fetches recent preprints (PPR source).
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)

    if query:
        date_query = (
            f"({query}) AND (FIRST_PDATE:[{start.isoformat()} TO {end.isoformat()}])"
        )
    else:
        date_query = (
            f"SRC:PPR AND (FIRST_PDATE:[{start.isoformat()} TO {end.isoformat()}])"
        )

    papers: list[FetchedPaper] = []
    cursor_mark = "*"

    with httpx.Client(timeout=timeout) as client:
        while True:
            params = {
                "query": date_query,
                "format": "json",
                "pageSize": PAGE_SIZE,
                "resultType": "core",
                "cursorMark": cursor_mark,
            }
            logger.debug("Fetching EuropePMC: %s", date_query)

            try:
                resp = client.get(SEARCH_URL, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("HTTP error fetching EuropePMC: %s", e)
                break

            data = resp.json()
            results = data.get("resultList", {}).get("result", [])
            if not results:
                break

            for item in results:
                doi = item.get("doi", "")
                pmid = item.get("pmid", "")
                identifier = doi or pmid
                if not identifier:
                    continue

                paper = FetchedPaper(
                    doi=doi or f"pmid:{pmid}",
                    title=item.get("title", ""),
                    authors=_format_authors(item.get("authorString", "")),
                    abstract=item.get("abstractText", ""),
                    url=_build_url(doi, pmid),
                    source="europepmc",
                    published_date=item.get("firstPublicationDate", ""),
                    categories=_format_categories(item),
                    metadata={
                        "pmid": pmid,
                        "pmcid": item.get("pmcid", ""),
                        "source": item.get("source", ""),
                        "pub_type": item.get("pubTypeList", {}).get("pubType", []),
                        "journal": item.get("journalTitle", ""),
                        "cited_by": item.get("citedByCount", 0),
                        "is_open_access": item.get("isOpenAccess", "N"),
                    },
                )
                papers.append(paper)

            # Pagination
            next_cursor = data.get("nextCursorMark", "")
            if not next_cursor or next_cursor == cursor_mark:
                break
            cursor_mark = next_cursor

    logger.info("Fetched %d papers from EuropePMC", len(papers))
    return papers


def _format_authors(authors_str: str) -> str:
    """Clean up the authors string."""
    if not authors_str:
        return ""
    return authors_str.rstrip(".")


def _build_url(doi: str, pmid: str) -> str:
    """Build the best available URL for the paper."""
    if doi:
        return f"https://doi.org/{doi}"
    if pmid:
        return f"https://europepmc.org/article/med/{pmid}"
    return ""


def _format_categories(item: dict) -> str:
    """Extract category/subject info."""
    parts = []
    if item.get("journalTitle"):
        parts.append(item["journalTitle"])
    pub_types = item.get("pubTypeList", {}).get("pubType", [])
    if pub_types:
        parts.extend(pub_types)
    return "; ".join(parts)
