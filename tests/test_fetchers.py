"""Tests for fetcher parsing logic (no network calls)."""

from __future__ import annotations

from datetime import date

from bmnews.fetchers.medrxiv import MedRxivFetcher
from bmnews.fetchers.europepmc import EuropePMCFetcher


class TestMedRxivParser:
    def setup_method(self):
        self.fetcher = MedRxivFetcher("medrxiv")

    def test_parse_valid_item(self):
        item = {
            "rel_doi": "10.1101/2024.01.01.12345678",
            "rel_title": "A Great Paper",
            "rel_date": "2024-01-15",
            "rel_link": "https://medrxiv.org/content/short/10.1101/2024.01.01.12345678",
            "rel_abs": "This is the abstract text.",
            "rel_authors": "Smith, J.; Jones, K.; Lee, M.",
            "rel_num_authors": 3,
            "category": "epidemiology",
            "version": "1",
            "author_inst": "MIT; Stanford",
            "type": "new",
        }
        paper = self.fetcher._parse(item)
        assert paper is not None
        assert paper.doi == "10.1101/2024.01.01.12345678"
        assert paper.title == "A Great Paper"
        assert len(paper.authors) == 3
        assert paper.published_date == date(2024, 1, 15)
        assert paper.source == "medrxiv"
        assert "epidemiology" in paper.categories

    def test_parse_missing_title(self):
        item = {"rel_doi": "10.1101/test", "rel_title": "", "rel_abs": "text"}
        assert self.fetcher._parse(item) is None

    def test_parse_minimal_item(self):
        item = {"rel_title": "Minimal Paper"}
        paper = self.fetcher._parse(item)
        assert paper is not None
        assert paper.title == "Minimal Paper"
        assert paper.doi is None
        assert paper.authors == []


class TestEuropePMCParser:
    def setup_method(self):
        self.fetcher = EuropePMCFetcher()

    def test_parse_valid_item(self):
        item = {
            "id": "PPR12345",
            "doi": "10.1101/2024.02.01.99999",
            "title": "Europe PMC Preprint",
            "authorString": "Author A, Author B, Author C",
            "abstractText": "A detailed abstract about genomics.",
            "firstPublicationDate": "2024-02-01",
            "source": "PPR",
            "citedByCount": 5,
            "hasPDF": "Y",
            "keywordList": {"keyword": ["genomics", "bioinformatics"]},
        }
        paper = self.fetcher._parse(item)
        assert paper is not None
        assert paper.doi == "10.1101/2024.02.01.99999"
        assert paper.title == "Europe PMC Preprint"
        assert len(paper.authors) == 3
        assert paper.published_date == date(2024, 2, 1)
        assert "genomics" in paper.categories

    def test_parse_missing_title(self):
        item = {"doi": "10.1101/x", "title": ""}
        assert self.fetcher._parse(item) is None

    def test_parse_no_doi_with_url(self):
        item = {
            "title": "No DOI Paper",
            "fullTextUrlList": {
                "fullTextUrl": [
                    {"documentStyle": "html", "url": "https://example.com/paper"},
                ]
            },
        }
        paper = self.fetcher._parse(item)
        assert paper is not None
        assert paper.url == "https://example.com/paper"
        assert paper.doi is None
