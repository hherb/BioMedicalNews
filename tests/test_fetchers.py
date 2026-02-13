"""Tests for bmnews.fetchers."""

from __future__ import annotations

from bmnews.fetchers.base import FetchedPaper


class TestFetchedPaper:
    def test_construction(self):
        paper = FetchedPaper(
            doi="10.1101/2024.01.01.000001",
            title="Test Paper",
            authors="Smith J; Jones A",
            abstract="This is a test abstract.",
            url="https://doi.org/10.1101/2024.01.01.000001",
            source="medrxiv",
        )
        assert paper.doi == "10.1101/2024.01.01.000001"
        assert paper.has_abstract

    def test_empty_abstract(self):
        paper = FetchedPaper(doi="10.1101/x", title="No Abstract")
        assert not paper.has_abstract
        assert paper.metadata == {}
