"""Base protocol for publication fetchers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol


@dataclass
class FetchedPaper:
    """Normalised representation of a fetched publication."""

    doi: str | None
    title: str
    authors: list[str]
    abstract: str
    url: str
    source: str  # "medrxiv", "biorxiv", "europepmc"
    published_date: date | None = None
    categories: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


class Fetcher(Protocol):
    """Interface that all fetchers implement."""

    source_name: str

    def fetch(self, since: date, until: date | None = None) -> list[FetchedPaper]:
        """Fetch papers published between *since* and *until* (inclusive)."""
        ...
