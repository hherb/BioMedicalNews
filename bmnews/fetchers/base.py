"""Shared data types for fetched papers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FetchedPaper:
    """Normalized representation of a paper from any source."""
    doi: str
    title: str
    authors: str = ""
    abstract: str = ""
    url: str = ""
    source: str = ""
    published_date: str = ""
    categories: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def has_abstract(self) -> bool:
        return bool(self.abstract and self.abstract.strip())
