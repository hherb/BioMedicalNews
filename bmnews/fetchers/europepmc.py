"""Fetcher for Europe PMC preprints.

Uses the Europe PMC REST API:
  https://www.ebi.ac.uk/europepmc/webservices/rest/search

We query for preprints (SRC:PPR) published within a date range and paginate
using the cursorMark parameter.
"""

from __future__ import annotations

import logging
import time
from datetime import date

import httpx

from bmnews.fetchers.base import FetchedPaper

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_PAGE_SIZE = 100
_REQUEST_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 2.0


class EuropePMCFetcher:
    """Fetch preprints from Europe PMC."""

    source_name = "europepmc"

    def fetch(self, since: date, until: date | None = None) -> list[FetchedPaper]:
        until = until or date.today()
        # EuropePMC date format: YYYY-MM-DD
        date_query = f'(SRC:PPR) AND (FIRST_PDATE:[{since.isoformat()} TO {until.isoformat()}])'

        papers: list[FetchedPaper] = []
        cursor_mark = "*"

        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            while True:
                params = {
                    "query": date_query,
                    "format": "json",
                    "pageSize": _PAGE_SIZE,
                    "cursorMark": cursor_mark,
                    "sort": "FIRST_PDATE desc",
                    "resultType": "core",
                }
                data = self._request(client, params)
                if data is None:
                    break

                result_list = data.get("resultList", {}).get("result", [])
                if not result_list:
                    break

                for item in result_list:
                    paper = self._parse(item)
                    if paper:
                        papers.append(paper)

                logger.info("europepmc: fetched %d papers (cursor=%s)", len(result_list), cursor_mark)

                next_cursor = data.get("nextCursorMark")
                if not next_cursor or next_cursor == cursor_mark:
                    break
                cursor_mark = next_cursor

        logger.info("europepmc: total %d papers from %s to %s", len(papers), since, until)
        return papers

    def _request(self, client: httpx.Client, params: dict) -> dict | None:
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                resp = client.get(_BASE_URL, params=params)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.warning(
                    "europepmc: request failed (attempt %d/%d): %s",
                    attempt, _RETRY_ATTEMPTS, exc,
                )
                if attempt < _RETRY_ATTEMPTS:
                    time.sleep(_RETRY_BACKOFF * attempt)
        logger.error("europepmc: giving up after %d attempts", _RETRY_ATTEMPTS)
        return None

    def _parse(self, item: dict) -> FetchedPaper | None:
        title = (item.get("title") or "").strip()
        if not title:
            return None

        doi = item.get("doi")
        authors_str = item.get("authorString", "")
        authors = [a.strip() for a in authors_str.split(",") if a.strip()] if authors_str else []

        abstract = (item.get("abstractText") or "").strip()

        pub_date = None
        date_str = item.get("firstPublicationDate")
        if date_str:
            try:
                pub_date = date.fromisoformat(date_str)
            except ValueError:
                pass

        url = ""
        if doi:
            url = f"https://doi.org/{doi}"
        elif item.get("fullTextUrlList"):
            urls = item["fullTextUrlList"].get("fullTextUrl", [])
            for u in urls:
                if u.get("documentStyle") == "html":
                    url = u.get("url", "")
                    break
            if not url and urls:
                url = urls[0].get("url", "")

        # EuropePMC preprints often have source info
        source_name = "europepmc"
        ppr_source = (item.get("bookOrReportDetails") or {}).get("publisher", "")
        if "medrxiv" in ppr_source.lower():
            source_name = "medrxiv"
        elif "biorxiv" in ppr_source.lower():
            source_name = "biorxiv"

        keywords = item.get("keywordList", {}).get("keyword", [])

        return FetchedPaper(
            doi=doi,
            title=title,
            authors=authors,
            abstract=abstract,
            url=url,
            source=source_name,
            published_date=pub_date,
            categories=keywords,
            extra={
                "europepmc_id": item.get("id"),
                "source_db": item.get("source"),
                "cited_by_count": item.get("citedByCount", 0),
                "has_pdf": item.get("hasPDF", "N"),
            },
        )
