"""Fetcher for medRxiv and bioRxiv preprint servers.

Uses the public API: https://api.medrxiv.org/
Endpoint pattern: /details/{server}/{start_date}/{end_date}/{cursor}
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from bmnews.fetchers.base import FetchedPaper

logger = logging.getLogger(__name__)

BASE_URL = "https://api.medrxiv.org/details"
PAGE_SIZE = 100


def fetch_medrxiv(
    lookback_days: int = 7,
    timeout: float = 30.0,
) -> list[FetchedPaper]:
    """Fetch recent preprints from medRxiv."""
    return _fetch_rxiv("medrxiv", lookback_days, timeout)


def fetch_biorxiv(
    lookback_days: int = 7,
    timeout: float = 30.0,
) -> list[FetchedPaper]:
    """Fetch recent preprints from bioRxiv."""
    return _fetch_rxiv("biorxiv", lookback_days, timeout)


def _fetch_rxiv(
    server: str,
    lookback_days: int,
    timeout: float,
) -> list[FetchedPaper]:
    """Fetch from a medRxiv/bioRxiv API endpoint with pagination."""
    end = date.today()
    start = end - timedelta(days=lookback_days)
    start_str = start.isoformat()
    end_str = end.isoformat()

    papers: list[FetchedPaper] = []
    cursor = 0

    with httpx.Client(timeout=timeout) as client:
        while True:
            url = f"{BASE_URL}/{server}/{start_str}/{end_str}/{cursor}"
            logger.debug("Fetching %s", url)

            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("HTTP error fetching %s: %s", url, e)
                break

            data = resp.json()
            collection = data.get("collection", [])
            if not collection:
                break

            for item in collection:
                doi = item.get("rel_doi", "")
                if not doi:
                    continue

                paper = FetchedPaper(
                    doi=doi,
                    title=item.get("rel_title", ""),
                    authors=_format_authors(item.get("rel_authors", "")),
                    abstract=item.get("rel_abs", ""),
                    url=f"https://doi.org/{doi}",
                    source=server,
                    published_date=item.get("rel_date", ""),
                    categories=item.get("rel_site", ""),
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
                    total = int(msg["total"])
                    break

            cursor += PAGE_SIZE
            if cursor >= total:
                break

    logger.info("Fetched %d papers from %s", len(papers), server)
    return papers


def _format_authors(authors_str: str) -> str:
    """Clean up the authors string from the API."""
    if not authors_str:
        return ""
    # The API returns semicolon-separated authors
    authors = [a.strip() for a in authors_str.split(";") if a.strip()]
    return "; ".join(authors)
