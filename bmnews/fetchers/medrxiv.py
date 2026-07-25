"""Fetcher for medRxiv and bioRxiv preprint servers.

Uses the public API: https://api.medrxiv.org/
Endpoint pattern: /details/{server}/{start_date}/{end_date}/{cursor}
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from bmnews.constants import HTTP_TIMEOUT_SECONDS, MAX_FETCH_PAGES, RXIV_PAGE_SIZE
from bmnews.fetchers.base import FetchedPaper

logger = logging.getLogger(__name__)

BASE_URL = "https://api.medrxiv.org/details"
PAGE_SIZE = RXIV_PAGE_SIZE


def fetch_medrxiv(
    lookback_days: int = 7,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> list[FetchedPaper]:
    """Fetch recent preprints from medRxiv.

    Args:
        lookback_days: How many days back from today to fetch.
        timeout: Per-request HTTP timeout in seconds.

    Returns:
        Normalized papers from medRxiv.
    """
    return _fetch_rxiv("medrxiv", lookback_days, timeout)


def fetch_biorxiv(
    lookback_days: int = 7,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> list[FetchedPaper]:
    """Fetch recent preprints from bioRxiv.

    Args:
        lookback_days: How many days back from today to fetch.
        timeout: Per-request HTTP timeout in seconds.

    Returns:
        Normalized papers from bioRxiv.
    """
    return _fetch_rxiv("biorxiv", lookback_days, timeout)


def _fetch_rxiv(
    server: str,
    lookback_days: int,
    timeout: float,
) -> list[FetchedPaper]:
    """Fetch from a medRxiv/bioRxiv details endpoint, walking all pages.

    Args:
        server: Either ``"medrxiv"`` or ``"biorxiv"``.
        lookback_days: How many days back from today to fetch.
        timeout: Per-request HTTP timeout in seconds.

    Returns:
        Normalized papers. Returns whatever was collected so far if the API
        errors partway through pagination.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    start_str = start.isoformat()
    end_str = end.isoformat()

    papers: list[FetchedPaper] = []
    cursor = 0

    with httpx.Client(timeout=timeout) as client:
        for _page in range(MAX_FETCH_PAGES):
            url = f"{BASE_URL}/{server}/{start_str}/{end_str}/{cursor}"
            logger.debug("Fetching %s", url)

            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("HTTP error fetching %s: %s", url, e)
                break

            try:
                data = resp.json()
            except ValueError:
                logger.error("Malformed JSON from %s — stopping pagination", url)
                break

            collection = data.get("collection", [])
            if not collection:
                break

            for item in collection:
                doi = item.get("doi", "")
                if not doi:
                    continue

                paper = FetchedPaper(
                    doi=doi,
                    title=item.get("title", ""),
                    authors=_format_authors(item.get("authors", "")),
                    abstract=item.get("abstract", ""),
                    url=f"https://doi.org/{doi}",
                    source=server,
                    published_date=item.get("date", ""),
                    categories=item.get("category", ""),
                    metadata={
                        "version": item.get("version", ""),
                        "type": item.get("type", ""),
                        "category": item.get("category", ""),
                        "jats_xml_path": item.get("jatsxml", ""),
                    },
                )
                papers.append(paper)

            # Check if there are more pages
            messages = data.get("messages", [])
            total = 0
            for msg in messages:
                if isinstance(msg, dict) and "total" in msg:
                    try:
                        total = int(msg["total"])
                    except (TypeError, ValueError):
                        total = 0
                    break

            cursor += PAGE_SIZE
            if cursor >= total:
                break
        else:
            logger.warning(
                "Stopped after %d %s pages — results may be truncated",
                MAX_FETCH_PAGES, server,
            )

    logger.info("Fetched %d papers from %s", len(papers), server)
    return papers


def _format_authors(authors_str: str) -> str:
    """Clean up the authors string from the API."""
    if not authors_str:
        return ""
    # The API returns semicolon-separated authors
    authors = [a.strip() for a in authors_str.split(";") if a.strip()]
    return "; ".join(authors)
