"""Fetcher for medRxiv and bioRxiv preprint servers.

Uses the official content API:
  https://api.medrxiv.org/details/{server}/{start}/{end}/{cursor}
  https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}

The API returns up to 100 records per page.  We paginate via the cursor
parameter (0, 100, 200, …) until fewer than 100 records are returned.
"""

from __future__ import annotations

import logging
import time
from datetime import date

import httpx

from bmnews.fetchers.base import FetchedPaper

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_BASE_URLS = {
    "medrxiv": "https://api.medrxiv.org/details/medrxiv",
    "biorxiv": "https://api.biorxiv.org/details/biorxiv",
}
_REQUEST_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 2.0


class MedRxivFetcher:
    """Fetch preprints from medRxiv or bioRxiv."""

    def __init__(self, server: str = "medrxiv"):
        if server not in _BASE_URLS:
            raise ValueError(f"Unknown server: {server!r} (expected 'medrxiv' or 'biorxiv')")
        self.server = server
        self.source_name = server
        self._base = _BASE_URLS[server]

    def fetch(self, since: date, until: date | None = None) -> list[FetchedPaper]:
        until = until or date.today()
        start_str = since.strftime("%Y-%m-%d")
        end_str = until.strftime("%Y-%m-%d")

        papers: list[FetchedPaper] = []
        cursor = 0

        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            while True:
                url = f"{self._base}/{start_str}/{end_str}/{cursor}"
                data = self._request(client, url)
                if data is None:
                    break

                collection = data.get("collection", [])
                if not collection:
                    break

                for item in collection:
                    paper = self._parse(item)
                    if paper:
                        papers.append(paper)

                logger.info(
                    "%s: fetched %d papers (cursor=%d)", self.server, len(collection), cursor
                )

                if len(collection) < _PAGE_SIZE:
                    break
                cursor += _PAGE_SIZE

        logger.info("%s: total %d papers from %s to %s", self.server, len(papers), start_str, end_str)
        return papers

    def _request(self, client: httpx.Client, url: str) -> dict | None:
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.warning(
                    "%s: request failed (attempt %d/%d): %s", self.server, attempt, _RETRY_ATTEMPTS, exc
                )
                if attempt < _RETRY_ATTEMPTS:
                    time.sleep(_RETRY_BACKOFF * attempt)
        logger.error("%s: giving up on %s", self.server, url)
        return None

    def _parse(self, item: dict) -> FetchedPaper | None:
        title = item.get("rel_title", "").strip()
        if not title:
            return None

        doi = item.get("rel_doi", "")
        authors_str = item.get("rel_authors", "")
        authors = [a.strip() for a in authors_str.split(";") if a.strip()] if authors_str else []
        abstract = item.get("rel_abs", "").strip()
        pub_date = None
        if item.get("rel_date"):
            try:
                pub_date = date.fromisoformat(item["rel_date"])
            except ValueError:
                pass

        url = item.get("rel_link", "")
        if not url and doi:
            url = f"https://doi.org/{doi}"

        category = item.get("category", "")
        categories = [category] if category else []

        return FetchedPaper(
            doi=doi or None,
            title=title,
            authors=authors,
            abstract=abstract,
            url=url,
            source=self.server,
            published_date=pub_date,
            categories=categories,
            extra={
                "version": item.get("version"),
                "author_institutions": item.get("author_inst", ""),
                "type": item.get("type", ""),
            },
        )
