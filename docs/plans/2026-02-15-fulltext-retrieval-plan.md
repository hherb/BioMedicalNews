# Full Text Retrieval & Display Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add full text retrieval and display to the newsreader, porting the proven Swift BioMedLit architecture to Python bmlib, with better abstract formatting.

**Architecture:** New `bmlib.fulltext` module with JATS XML parser (SAX-based), 3-tier retrieval service (Europe PMC → Unpaywall → DOI), and PDF caching. BioMedicalNews gets a DB migration for pmid/pmcid/fulltext columns, a new HTMX-driven route, and updated reading pane template with styled full text display.

**Tech Stack:** Python 3.11+, xml.sax (stdlib), httpx, Flask/HTMX, SQLite

---

### Task 1: bmlib Data Models (`bmlib/bmlib/fulltext/models.py`)

**Files:**
- Create: `bmlib/bmlib/fulltext/__init__.py`
- Create: `bmlib/bmlib/fulltext/models.py`
- Test: `bmlib/tests/test_fulltext_models.py`

**Step 1: Write the failing test**

Create `bmlib/tests/test_fulltext_models.py`:

```python
"""Tests for bmlib.fulltext.models."""

from bmlib.fulltext.models import (
    FullTextResult,
    JATSAbstractSection,
    JATSArticle,
    JATSAuthorInfo,
    JATSBodySection,
    JATSFigureInfo,
    JATSReferenceInfo,
    JATSTableInfo,
)


class TestJATSAuthorInfo:
    def test_full_name(self):
        author = JATSAuthorInfo(surname="Smith", given_names="John A")
        assert author.full_name == "John A Smith"

    def test_full_name_no_given(self):
        author = JATSAuthorInfo(surname="Consortium")
        assert author.full_name == "Consortium"


class TestJATSReferenceInfo:
    def test_formatted_citation_structured(self):
        ref = JATSReferenceInfo(
            id="r1", label="1", citation="",
            authors=["Smith J", "Doe A"],
            article_title="A study",
            source="Nature", year="2024",
            volume="580", issue="3",
            first_page="123", last_page="130",
            doi="10.1038/example", pmid="12345678",
        )
        result = ref.formatted_citation
        assert "Smith J, Doe A" in result
        assert "A study" in result
        assert "Nature" in result
        assert "(2024)" in result
        assert "580(3):123-130" in result
        assert "doi:10.1038/example" in result

    def test_formatted_citation_fallback(self):
        ref = JATSReferenceInfo(
            id="r1", label="1", citation="Raw citation text.",
            authors=[], article_title="", source="", year="",
            volume="", issue="", first_page="", last_page="",
            doi="", pmid="",
        )
        assert ref.formatted_citation == "Raw citation text."

    def test_formatted_citation_et_al(self):
        ref = JATSReferenceInfo(
            id="r1", label="1", citation="",
            authors=["A", "B", "C", "D"],
            article_title="Title", source="J", year="2024",
            volume="", issue="", first_page="", last_page="",
            doi="", pmid="",
        )
        result = ref.formatted_citation
        assert "et al." in result


class TestFullTextResult:
    def test_europepmc(self):
        r = FullTextResult(source="europepmc", html="<p>content</p>")
        assert r.source == "europepmc"
        assert r.html == "<p>content</p>"
        assert r.pdf_url is None

    def test_unpaywall(self):
        r = FullTextResult(source="unpaywall", pdf_url="https://example.com/paper.pdf")
        assert r.pdf_url == "https://example.com/paper.pdf"

    def test_doi(self):
        r = FullTextResult(source="doi", web_url="https://doi.org/10.1234/test")
        assert r.web_url == "https://doi.org/10.1234/test"


class TestJATSBodySection:
    def test_nested(self):
        child = JATSBodySection(title="Methods", paragraphs=["We did X."])
        parent = JATSBodySection(title="Main", paragraphs=[], subsections=[child])
        assert parent.subsections[0].title == "Methods"


class TestJATSArticle:
    def test_construction(self):
        article = JATSArticle(
            title="Test", authors=[], journal="Nature",
            volume="1", issue="2", pages="3-4", year="2024",
            doi="10.1/t", pmc_id="PMC123", pmid="456",
            abstract_sections=[], body_sections=[],
            figures=[], tables=[], references=[],
        )
        assert article.title == "Test"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_fulltext_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bmlib.fulltext'`

**Step 3: Write the module**

Create `bmlib/bmlib/fulltext/__init__.py`:

```python
"""Full-text retrieval and JATS XML parsing for biomedical literature."""
```

Create `bmlib/bmlib/fulltext/models.py`:

```python
"""Data models for full-text retrieval and JATS XML parsing.

Mirrors the Swift BioMedLit library's JATSModels and FullTextResult types.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JATSAuthorInfo:
    """Parsed author information from a JATS article."""

    surname: str
    given_names: str = ""
    affiliations: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        if not self.given_names:
            return self.surname
        return f"{self.given_names} {self.surname}"


@dataclass
class JATSAbstractSection:
    """Parsed abstract section (e.g. Background, Methods)."""

    title: str
    content: str


@dataclass
class JATSBodySection:
    """Parsed body section with nested subsections."""

    title: str
    paragraphs: list[str] = field(default_factory=list)
    subsections: list[JATSBodySection] = field(default_factory=list)


@dataclass
class JATSFigureInfo:
    """Parsed figure metadata."""

    id: str
    label: str
    caption: str
    graphic_url: str | None = None


@dataclass
class JATSTableInfo:
    """Parsed table metadata with pre-rendered HTML content."""

    id: str
    label: str
    caption: str
    html_content: str = ""


@dataclass
class JATSReferenceInfo:
    """Parsed reference/citation information."""

    id: str
    label: str
    citation: str
    authors: list[str] = field(default_factory=list)
    article_title: str = ""
    source: str = ""
    year: str = ""
    volume: str = ""
    issue: str = ""
    first_page: str = ""
    last_page: str = ""
    doi: str = ""
    pmid: str = ""

    @property
    def formatted_citation(self) -> str:
        parts: list[str] = []
        if self.authors:
            if len(self.authors) <= 3:
                parts.append(", ".join(self.authors))
            else:
                parts.append(f"{self.authors[0]}, {self.authors[1]}, et al.")
        if self.article_title:
            parts.append(self.article_title)
        if self.source:
            parts.append(self.source)
        if self.year:
            parts.append(f"({self.year})")
        volume_info = ""
        if self.volume:
            volume_info = self.volume
            if self.issue:
                volume_info += f"({self.issue})"
        if self.first_page:
            if volume_info:
                volume_info += ":"
            volume_info += self.first_page
            if self.last_page:
                volume_info += f"-{self.last_page}"
        if volume_info:
            parts.append(volume_info)
        if self.doi:
            parts.append(f"doi:{self.doi}")
        if not parts:
            return self.citation
        return ". ".join(parts)


@dataclass
class JATSArticle:
    """Complete parsed JATS article data."""

    title: str
    authors: list[JATSAuthorInfo]
    journal: str
    volume: str
    issue: str
    pages: str
    year: str
    doi: str
    pmc_id: str
    pmid: str
    abstract_sections: list[JATSAbstractSection]
    body_sections: list[JATSBodySection]
    figures: list[JATSFigureInfo]
    tables: list[JATSTableInfo]
    references: list[JATSReferenceInfo]


@dataclass
class FullTextResult:
    """Result of a full-text retrieval attempt."""

    source: str  # "europepmc", "unpaywall", "doi", "cached"
    html: str | None = None
    pdf_url: str | None = None
    web_url: str | None = None
    file_path: str | None = None
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_fulltext_models.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/fulltext/__init__.py bmlib/fulltext/models.py tests/test_fulltext_models.py
git commit -m "feat(fulltext): add JATS and full-text result data models"
```

---

### Task 2: JATS XML Parser — Core Parsing (`bmlib/bmlib/fulltext/jats_parser.py`)

**Files:**
- Create: `bmlib/bmlib/fulltext/jats_parser.py`
- Create: `bmlib/tests/test_jats_parser.py`
- Create: `bmlib/tests/fixtures/` (sample JATS XML)

**Reference:** Port of Swift `JATSXMLParser` at `/Users/hherb/src/bmlibrarian_lite/Packages/BioMedLit/Sources/BioMedLit/JATS/JATSXMLParser.swift` (1407 lines). Uses `xml.sax.ContentHandler` — same SAX event-driven pattern as Swift's `XMLParserDelegate`.

**Step 1: Write failing tests with a sample JATS XML fixture**

Create `bmlib/tests/fixtures/sample_article.xml` — a minimal but representative JATS XML document covering all key elements (metadata, structured abstract, body sections, figures, tables, references). Use a real-world-like structure based on the JATS tag set.

Create `bmlib/tests/test_jats_parser.py`:

```python
"""Tests for bmlib.fulltext.jats_parser."""

from pathlib import Path

from bmlib.fulltext.jats_parser import JATSParser

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class TestJATSParserMetadata:
    def test_parse_title(self):
        data = _load_fixture("sample_article.xml")
        parser = JATSParser(data)
        article = parser.parse()
        assert article.title != ""

    def test_parse_authors(self):
        data = _load_fixture("sample_article.xml")
        article = JATSParser(data).parse()
        assert len(article.authors) > 0
        assert article.authors[0].surname != ""

    def test_parse_journal(self):
        data = _load_fixture("sample_article.xml")
        article = JATSParser(data).parse()
        assert article.journal != ""

    def test_parse_identifiers(self):
        data = _load_fixture("sample_article.xml")
        article = JATSParser(data).parse()
        assert article.doi != ""


class TestJATSParserAbstract:
    def test_structured_abstract(self):
        data = _load_fixture("sample_article.xml")
        article = JATSParser(data).parse()
        assert len(article.abstract_sections) > 0
        # Should have titled sections
        titles = [s.title for s in article.abstract_sections]
        assert any(t != "" for t in titles)

    def test_abstract_content(self):
        data = _load_fixture("sample_article.xml")
        article = JATSParser(data).parse()
        for section in article.abstract_sections:
            assert section.content != ""


class TestJATSParserBody:
    def test_body_sections(self):
        data = _load_fixture("sample_article.xml")
        article = JATSParser(data).parse()
        assert len(article.body_sections) > 0
        assert article.body_sections[0].title != ""

    def test_section_paragraphs(self):
        data = _load_fixture("sample_article.xml")
        article = JATSParser(data).parse()
        # At least one section should have paragraphs
        has_paragraphs = any(len(s.paragraphs) > 0 for s in article.body_sections)
        assert has_paragraphs


class TestJATSParserReferences:
    def test_references(self):
        data = _load_fixture("sample_article.xml")
        article = JATSParser(data).parse()
        assert len(article.references) > 0


class TestJATSParserHTML:
    def test_to_html(self):
        data = _load_fixture("sample_article.xml")
        html = JATSParser(data).to_html()
        assert "<h1>" in html
        assert "<h2>" in html
        assert "Abstract" in html

    def test_html_escaping(self):
        data = _load_fixture("sample_article.xml")
        html = JATSParser(data).to_html()
        # Should not contain unescaped XML artifacts
        assert "<!DOCTYPE" not in html

    def test_to_html_with_known_pmc_id(self):
        data = _load_fixture("sample_article.xml")
        html = JATSParser(data, known_pmc_id="PMC7614751").to_html()
        assert "<h1>" in html
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_jats_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bmlib.fulltext.jats_parser'`

**Step 3: Write the JATS parser**

Create `bmlib/bmlib/fulltext/jats_parser.py`. This is a Python port of the Swift `JATSXMLParser`. Key design decisions:

- Use `xml.sax.ContentHandler` (event-driven SAX, same pattern as Swift's `XMLParserDelegate`)
- Same text stack / element stack / section stack architecture
- Builder classes for tables, figures, references (mirrored from Swift)
- Two output modes: `parse()` returns `JATSArticle`, `to_html()` returns HTML string
- HTML output includes semantic markup: `<figure>`, `<table>`, `<ol class="references">`, etc.
- Figure URLs constructed for Europe PMC: `https://europepmc.org/articles/PMC{id}/bin/{graphic}.jpg`

The parser handles these JATS elements (matching Swift parser):
- `front/article-meta` → metadata (title, authors, journal, IDs)
- `abstract/sec/title/p` → structured abstract sections
- `body/sec/title/p` → body sections with nesting
- `fig/graphic/label/caption` → figures
- `table-wrap/thead/tbody/tr/th/td` → tables (output as HTML `<table>`)
- `ref-list/ref/mixed-citation/element-citation` → references
- `bold/italic/sub/sup/monospace` → inline formatting
- `xref` → cross-reference anchor links

The implementation should closely follow the Swift code at lines 896-1407 of `JATSXMLParser.swift` for the element handling logic, adapted to Python's `xml.sax` API where:
- `startElement(name, attrs)` maps to Swift's `didStartElement`
- `endElement(name)` maps to Swift's `didEndElement`
- `characters(content)` maps to Swift's `foundCharacters`

Also create `bmlib/tests/fixtures/sample_article.xml` — a realistic JATS XML document with all the elements listed above.

**Step 4: Run test to verify it passes**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_jats_parser.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/fulltext/jats_parser.py tests/test_jats_parser.py tests/fixtures/sample_article.xml
git commit -m "feat(fulltext): add JATS XML parser ported from Swift BioMedLit"
```

---

### Task 3: Full Text Retrieval Service (`bmlib/bmlib/fulltext/service.py`)

**Files:**
- Create: `bmlib/bmlib/fulltext/service.py`
- Create: `bmlib/tests/test_fulltext_service.py`

**Reference:** Port of Swift `FullTextService` at `/Users/hherb/src/bmlibrarian_lite/Packages/BioMedLit/Sources/BioMedLit/Services/FullTextService.swift`

**Step 1: Write failing tests**

Create `bmlib/tests/test_fulltext_service.py`:

```python
"""Tests for bmlib.fulltext.service."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bmlib.fulltext.service import FullTextService, FullTextError

FIXTURES = Path(__file__).parent / "fixtures"


class TestFetchEuropePMC:
    def test_success(self):
        xml_data = (FIXTURES / "sample_article.xml").read_bytes()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = xml_data

        service = FullTextService(email="test@example.com")
        with patch.object(service, "_http_get", return_value=mock_response):
            result = service.fetch_fulltext(pmc_id="PMC123", doi=None, pmid="456")

        assert result.source == "europepmc"
        assert result.html is not None
        assert "<h1>" in result.html

    def test_404_falls_through(self):
        mock_404 = MagicMock()
        mock_404.status_code = 404
        mock_unpaywall_404 = MagicMock()
        mock_unpaywall_404.status_code = 404

        service = FullTextService(email="test@example.com")
        with patch.object(service, "_http_get", side_effect=[mock_404, mock_unpaywall_404]):
            result = service.fetch_fulltext(pmc_id="PMC123", doi="10.1/test", pmid="456")

        assert result.source == "doi"
        assert result.web_url == "https://doi.org/10.1/test"


class TestFetchUnpaywall:
    def test_success(self):
        mock_pmc_404 = MagicMock()
        mock_pmc_404.status_code = 404

        unpaywall_json = {
            "best_oa_location": {
                "url_for_pdf": "https://example.com/paper.pdf",
                "url": "https://example.com/paper",
                "host_type": "publisher",
                "license": "cc-by",
            }
        }
        mock_unpaywall = MagicMock()
        mock_unpaywall.status_code = 200
        mock_unpaywall.json.return_value = unpaywall_json

        service = FullTextService(email="test@example.com")
        with patch.object(service, "_http_get", side_effect=[mock_pmc_404, mock_unpaywall]):
            result = service.fetch_fulltext(pmc_id="PMC123", doi="10.1/test", pmid="456")

        assert result.source == "unpaywall"
        assert result.pdf_url == "https://example.com/paper.pdf"


class TestFetchDOIFallback:
    def test_no_pmc_no_unpaywall(self):
        service = FullTextService(email="test@example.com")
        result = service.fetch_fulltext(pmc_id=None, doi="10.1/test", pmid="456")
        assert result.source == "doi"
        assert result.web_url == "https://doi.org/10.1/test"

    def test_no_identifiers(self):
        service = FullTextService(email="test@example.com")
        with pytest.raises(FullTextError):
            service.fetch_fulltext(pmc_id=None, doi=None, pmid="")


class TestFullTextError:
    def test_no_identifiers_message(self):
        err = FullTextError("No identifiers provided")
        assert "No identifiers" in str(err)
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_fulltext_service.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the service**

Create `bmlib/bmlib/fulltext/service.py`:

```python
"""Full-text retrieval service with 3-tier fallback chain.

Tier 1: Europe PMC XML → JATS parser → HTML
Tier 2: Unpaywall → open-access PDF URL
Tier 3: DOI resolution → publisher website URL
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from bmlib.fulltext.jats_parser import JATSParser
from bmlib.fulltext.models import FullTextResult

logger = logging.getLogger(__name__)

EUROPE_PMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
DOI_BASE = "https://doi.org"
PUBMED_BASE = "https://pubmed.ncbi.nlm.nih.gov"
TIMEOUT = 30.0
MAX_RETRIES = 3


class FullTextError(Exception):
    """Error during full-text retrieval."""


class FullTextService:
    """Retrieves full text from multiple sources with fallback."""

    def __init__(self, email: str, timeout: float = TIMEOUT) -> None:
        self.email = email
        self.timeout = timeout

    def _http_get(self, url: str, **kwargs) -> httpx.Response:
        """HTTP GET with timeout. Separated for testability."""
        with httpx.Client(timeout=self.timeout) as client:
            return client.get(url, **kwargs)

    def fetch_fulltext(
        self,
        *,
        pmc_id: str | None = None,
        doi: str | None = None,
        pmid: str = "",
    ) -> FullTextResult:
        """Fetch full text using 3-tier fallback chain.

        Tries: Europe PMC XML → Unpaywall PDF → DOI resolution.
        """
        # Tier 1: Europe PMC
        if pmc_id:
            try:
                html = self._fetch_europepmc(pmc_id)
                logger.info("Full text retrieved from Europe PMC for %s", pmc_id)
                return FullTextResult(source="europepmc", html=html)
            except Exception:
                logger.debug("Europe PMC failed for %s", pmc_id, exc_info=True)

        # Tier 2: Unpaywall
        if doi:
            try:
                pdf_url = self._fetch_unpaywall(doi)
                logger.info("PDF URL found via Unpaywall for DOI %s", doi)
                return FullTextResult(source="unpaywall", pdf_url=pdf_url)
            except Exception:
                logger.debug("Unpaywall failed for DOI %s", doi, exc_info=True)

        # Tier 3: DOI fallback
        if doi:
            logger.info("Falling back to DOI URL for %s", doi)
            return FullTextResult(source="doi", web_url=f"{DOI_BASE}/{doi}")

        # Final fallback: PubMed URL
        if pmid:
            logger.info("Falling back to PubMed URL for PMID %s", pmid)
            return FullTextResult(source="doi", web_url=f"{PUBMED_BASE}/{pmid}/")

        raise FullTextError("No identifiers provided")

    def _fetch_europepmc(self, pmc_id: str) -> str:
        """Fetch JATS XML from Europe PMC and parse to HTML."""
        normalized = pmc_id if pmc_id.startswith("PMC") else f"PMC{pmc_id}"
        url = f"{EUROPE_PMC_BASE}/{normalized}/fullTextXML"

        resp = self._http_get(url, headers={"Accept": "application/xml"})
        if resp.status_code == 404:
            raise FullTextError(f"No full text in Europe PMC for {normalized}")
        if resp.status_code != 200:
            raise FullTextError(f"Europe PMC HTTP {resp.status_code}")

        parser = JATSParser(resp.content, known_pmc_id=normalized)
        return parser.to_html()

    def _fetch_unpaywall(self, doi: str) -> str:
        """Query Unpaywall for open-access PDF URL."""
        encoded_doi = quote(doi, safe="")
        url = f"{UNPAYWALL_BASE}/{encoded_doi}?email={self.email}"

        resp = self._http_get(url, headers={"Accept": "application/json"})
        if resp.status_code == 404:
            raise FullTextError(f"DOI not found in Unpaywall: {doi}")
        if resp.status_code != 200:
            raise FullTextError(f"Unpaywall HTTP {resp.status_code}")

        data = resp.json()
        best = data.get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf") or best.get("url")
        if pdf_url:
            return pdf_url

        for loc in data.get("oa_locations") or []:
            pdf_url = loc.get("url_for_pdf") or loc.get("url")
            if pdf_url:
                return pdf_url

        raise FullTextError(f"No open-access PDF found for DOI {doi}")
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_fulltext_service.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/fulltext/service.py tests/test_fulltext_service.py
git commit -m "feat(fulltext): add 3-tier full text retrieval service"
```

---

### Task 4: PDF Caching (`bmlib/bmlib/fulltext/cache.py`)

**Files:**
- Create: `bmlib/bmlib/fulltext/cache.py`
- Create: `bmlib/tests/test_fulltext_cache.py`

**Step 1: Write failing tests**

Create `bmlib/tests/test_fulltext_cache.py`:

```python
"""Tests for bmlib.fulltext.cache."""

from bmlib.fulltext.cache import PDFCache

PDF_MAGIC = b"%PDF-1.4 fake content"


class TestPDFCache:
    def test_cache_and_retrieve(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path)
        path = cache.save(PDF_MAGIC, "12345")
        assert path is not None
        assert cache.get("12345") == path

    def test_get_missing(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path)
        assert cache.get("99999") is None

    def test_rejects_non_pdf(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path)
        path = cache.save(b"not a pdf", "12345")
        assert path is None

    def test_delete(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path)
        cache.save(PDF_MAGIC, "12345")
        cache.delete("12345")
        assert cache.get("12345") is None

    def test_clear(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path)
        cache.save(PDF_MAGIC, "111")
        cache.save(PDF_MAGIC, "222")
        cache.clear()
        assert cache.get("111") is None
        assert cache.get("222") is None
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_fulltext_cache.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the cache**

Create `bmlib/bmlib/fulltext/cache.py`:

```python
"""Local PDF cache for downloaded full-text articles."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PDF_MAGIC_BYTES = b"%PDF"


class PDFCache:
    """Cache for downloaded PDF files, validated by magic bytes."""

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, identifier: str) -> str | None:
        """Save PDF data if valid. Returns file path or None."""
        if len(data) < len(PDF_MAGIC_BYTES) or data[:4] != PDF_MAGIC_BYTES:
            logger.warning("Rejected non-PDF data for %s", identifier)
            return None
        path = self.cache_dir / f"{identifier}.pdf"
        path.write_bytes(data)
        return str(path)

    def get(self, identifier: str) -> str | None:
        """Return cached file path if it exists."""
        path = self.cache_dir / f"{identifier}.pdf"
        return str(path) if path.exists() else None

    def delete(self, identifier: str) -> None:
        """Delete a cached PDF."""
        path = self.cache_dir / f"{identifier}.pdf"
        path.unlink(missing_ok=True)

    def clear(self) -> None:
        """Remove all cached PDFs."""
        for path in self.cache_dir.glob("*.pdf"):
            path.unlink()
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_fulltext_cache.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/fulltext/cache.py tests/test_fulltext_cache.py
git commit -m "feat(fulltext): add PDF caching with magic-byte validation"
```

---

### Task 5: Export bmlib.fulltext public API

**Files:**
- Modify: `bmlib/bmlib/fulltext/__init__.py`

**Step 1: Update the `__init__.py` to export public API**

```python
"""Full-text retrieval and JATS XML parsing for biomedical literature."""

from bmlib.fulltext.cache import PDFCache
from bmlib.fulltext.jats_parser import JATSParser
from bmlib.fulltext.models import (
    FullTextResult,
    JATSAbstractSection,
    JATSArticle,
    JATSAuthorInfo,
    JATSBodySection,
    JATSFigureInfo,
    JATSReferenceInfo,
    JATSTableInfo,
)
from bmlib.fulltext.service import FullTextError, FullTextService

__all__ = [
    "FullTextError",
    "FullTextResult",
    "FullTextService",
    "JATSAbstractSection",
    "JATSArticle",
    "JATSAuthorInfo",
    "JATSBodySection",
    "JATSFigureInfo",
    "JATSParser",
    "JATSReferenceInfo",
    "JATSTableInfo",
    "PDFCache",
]
```

**Step 2: Run all fulltext tests**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_fulltext_*.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/fulltext/__init__.py
git commit -m "feat(fulltext): export public API"
```

---

### Task 6: BioMedicalNews DB Migration — Add pmid, pmcid, fulltext columns

**Files:**
- Modify: `bmnews/db/migrations.py` (add M003)
- Test: `tests/test_db.py` (add migration test)

**Step 1: Write failing test**

Add to `tests/test_db.py`:

```python
class TestMigration003:
    def test_fulltext_columns_exist(self):
        conn = _db()
        # After init_db, the new columns should exist
        from bmlib.db import fetch_one
        row = fetch_one(conn, "PRAGMA table_info(papers)")
        columns = [r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()]
        assert "pmid" in columns
        assert "pmcid" in columns
        assert "fulltext_html" in columns
        assert "fulltext_source" in columns
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_db.py::TestMigration003 -v`
Expected: FAIL — columns don't exist yet

**Step 3: Add the migration**

Modify `bmnews/db/migrations.py` — add after `_m002_add_paper_tags`:

```python
_M003_SQLITE = """\
ALTER TABLE papers ADD COLUMN pmid TEXT;
ALTER TABLE papers ADD COLUMN pmcid TEXT;
ALTER TABLE papers ADD COLUMN fulltext_html TEXT;
ALTER TABLE papers ADD COLUMN fulltext_source TEXT NOT NULL DEFAULT '';
"""

_M003_POSTGRESQL = """\
ALTER TABLE papers ADD COLUMN IF NOT EXISTS pmid TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS pmcid TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS fulltext_html TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS fulltext_source TEXT NOT NULL DEFAULT '';
"""


def _m003_add_fulltext_columns(conn: Any) -> None:
    """Add pmid, pmcid, fulltext_html, fulltext_source columns to papers."""
    is_sqlite = _is_sqlite(conn)
    sql = _M003_SQLITE if is_sqlite else _M003_POSTGRESQL

    if is_sqlite:
        # SQLite doesn't support ADD COLUMN IF NOT EXISTS,
        # so check each column first
        existing = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        for col_def in [
            "pmid TEXT",
            "pmcid TEXT",
            "fulltext_html TEXT",
            "fulltext_source TEXT NOT NULL DEFAULT ''",
        ]:
            col_name = col_def.split()[0]
            if col_name not in existing:
                conn.execute(f"ALTER TABLE papers ADD COLUMN {col_def}")
        conn.commit()
    else:
        create_tables(conn, sql)

    # Backfill from metadata_json for existing europepmc papers
    if is_sqlite:
        conn.execute("""
            UPDATE papers SET
                pmid = json_extract(metadata_json, '$.pmid'),
                pmcid = json_extract(metadata_json, '$.pmcid')
            WHERE source = 'europepmc'
              AND metadata_json != '{}'
              AND pmid IS NULL
        """)
        conn.commit()
```

Add to `MIGRATIONS` list:

```python
MIGRATIONS: list[Migration] = [
    Migration(1, "initial_schema", _m001_initial_schema),
    Migration(2, "add_paper_tags", _m002_add_paper_tags),
    Migration(3, "add_fulltext_columns", _m003_add_fulltext_columns),
]
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_db.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add bmnews/db/migrations.py tests/test_db.py
git commit -m "feat(db): add migration 3 — pmid, pmcid, fulltext columns with backfill"
```

---

### Task 7: Update DB Operations — Full Text Storage & Retrieval

**Files:**
- Modify: `bmnews/db/operations.py`
- Test: `tests/test_db.py` (extend)

**Step 1: Write failing test**

Add to `tests/test_db.py`:

```python
class TestFulltextOperations:
    def test_save_and_get_fulltext(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1/ft", title="FT Paper")
        from bmnews.db.operations import save_fulltext, get_paper_with_score
        save_fulltext(conn, paper_id=pid, html="<p>Full text</p>", source="europepmc")
        # Need to score paper first to use get_paper_with_score
        save_score(conn, paper_id=pid, combined_score=0.5)
        paper = get_paper_with_score(conn, pid)
        assert paper["fulltext_html"] == "<p>Full text</p>"
        assert paper["fulltext_source"] == "europepmc"

    def test_update_paper_pmid_pmcid(self):
        conn = _db()
        from bmnews.db.operations import update_paper_identifiers
        pid = upsert_paper(conn, doi="10.1/id", title="ID Paper")
        update_paper_identifiers(conn, paper_id=pid, pmid="12345", pmcid="PMC678")
        from bmlib.db import fetch_one
        row = fetch_one(conn, "SELECT pmid, pmcid FROM papers WHERE id = ?", (pid,))
        assert row["pmid"] == "12345"
        assert row["pmcid"] == "PMC678"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_db.py::TestFulltextOperations -v`
Expected: FAIL — functions don't exist

**Step 3: Add operations**

Add to `bmnews/db/operations.py`:

```python
def save_fulltext(
    conn: Any, *, paper_id: int, html: str, source: str,
) -> None:
    """Store full-text HTML and source for a paper."""
    ph = _placeholder(conn)
    with transaction(conn):
        execute(
            conn,
            f"UPDATE papers SET fulltext_html = {ph}, fulltext_source = {ph} WHERE id = {ph}",
            (html, source, paper_id),
        )


def update_paper_identifiers(
    conn: Any, *, paper_id: int, pmid: str | None = None, pmcid: str | None = None,
) -> None:
    """Update pmid and/or pmcid for a paper."""
    ph = _placeholder(conn)
    sets = []
    params: list = []
    if pmid is not None:
        sets.append(f"pmid = {ph}")
        params.append(pmid)
    if pmcid is not None:
        sets.append(f"pmcid = {ph}")
        params.append(pmcid)
    if not sets:
        return
    params.append(paper_id)
    with transaction(conn):
        execute(conn, f"UPDATE papers SET {', '.join(sets)} WHERE id = {ph}", tuple(params))
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_db.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add bmnews/db/operations.py tests/test_db.py
git commit -m "feat(db): add save_fulltext and update_paper_identifiers operations"
```

---

### Task 8: Update EuropePMC Fetcher — Store pmid/pmcid as Columns

**Files:**
- Modify: `bmnews/fetchers/europepmc.py`
- Modify: `bmnews/fetchers/base.py` (check if FetchedPaper needs pmid/pmcid fields)
- Modify: `bmnews/pipeline.py` (to store identifiers during upsert)
- Test: `tests/test_fetchers.py`

**Step 1: Check FetchedPaper model**

Read `bmnews/fetchers/base.py` to see the FetchedPaper dataclass.

**Step 2: Update the pipeline to store pmid/pmcid**

The `metadata` dict already contains `pmid` and `pmcid` from the EuropePMC fetcher (see `europepmc.py:85-87`). The pipeline's `run_store` function calls `upsert_paper`. Update it to also call `update_paper_identifiers` after upserting, extracting pmid/pmcid from the metadata dict.

**Step 3: Run tests**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/ -v`
Expected: All PASS

**Step 4: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add bmnews/fetchers/ bmnews/pipeline.py tests/
git commit -m "feat: store pmid/pmcid as top-level paper columns from fetchers"
```

---

### Task 9: Abstract Formatting Helper

**Files:**
- Create: `bmnews/gui/helpers.py`
- Test: `tests/test_gui_helpers.py`

**Step 1: Write failing test**

Create `tests/test_gui_helpers.py`:

```python
"""Tests for abstract formatting helpers."""

from bmnews.gui.helpers import format_abstract_html


class TestFormatAbstractHTML:
    def test_structured_abstract(self):
        text = "Background: Study context.\nMethods: We did X.\nResults: Found Y."
        html = format_abstract_html(text)
        assert "<strong>Background:</strong>" in html
        assert "<strong>Methods:</strong>" in html
        assert "Study context." in html

    def test_plain_abstract(self):
        text = "This is a plain abstract with no sections."
        html = format_abstract_html(text)
        assert "<p>" in html
        assert "plain abstract" in html

    def test_empty(self):
        assert format_abstract_html("") == ""
        assert format_abstract_html(None) == ""

    def test_html_escaping(self):
        text = "We used <5mg dose & measured >10 outcomes."
        html = format_abstract_html(text)
        assert "&lt;5mg" in html
        assert "&amp;" in html
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_gui_helpers.py -v`
Expected: FAIL

**Step 3: Write the helper**

Create `bmnews/gui/helpers.py`:

```python
"""Template helpers for the GUI."""

from __future__ import annotations

import re
from html import escape

# Common section labels in structured abstracts
_SECTION_PATTERN = re.compile(
    r"^(Background|Objective|Purpose|Introduction|Methods|Study Design|"
    r"Setting|Participants|Interventions|Main Outcome Measures|"
    r"Results|Findings|Conclusions?|Discussion|Significance|"
    r"Context|Design|Measurements|Limitations|Interpretation)\s*:",
    re.IGNORECASE | re.MULTILINE,
)


def format_abstract_html(text: str | None) -> str:
    """Format abstract text as HTML with structured section labels bolded."""
    if not text:
        return ""

    escaped = escape(text)

    # Try structured abstract (has labeled sections)
    parts = _SECTION_PATTERN.split(escaped)
    if len(parts) > 1:
        # parts alternates: [pre-label text, label1, text1, label2, text2, ...]
        html_parts = []
        if parts[0].strip():
            html_parts.append(f"<p>{parts[0].strip()}</p>")
        for i in range(1, len(parts), 2):
            label = parts[i]
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            html_parts.append(f"<p><strong>{label}:</strong> {content}</p>")
        return "\n".join(html_parts)

    # Plain abstract — just wrap in <p>
    paragraphs = [p.strip() for p in escaped.split("\n") if p.strip()]
    return "\n".join(f"<p>{p}</p>" for p in paragraphs)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_gui_helpers.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add bmnews/gui/helpers.py tests/test_gui_helpers.py
git commit -m "feat(gui): add abstract formatting helper with structured section detection"
```

---

### Task 10: Full Text Route + Reading Pane Updates

**Files:**
- Modify: `bmnews/gui/routes/papers.py` — add `POST /papers/<id>/fulltext` endpoint
- Modify: `bmnews/gui/templates/fragments/reading_pane.html` — add full text button + display
- Modify: `bmnews/gui/app.py` — register config for email (needed by FullTextService)
- Test: `tests/test_gui_app.py` (extend)

**Step 1: Write failing test**

Add to `tests/test_gui_app.py`:

```python
class TestFullTextRoute:
    def test_fulltext_endpoint_exists(self, seeded_client):
        # POST to fulltext route — should return 200 (or meaningful response)
        client = seeded_client
        # Get a paper ID first
        resp = client.get("/papers")
        assert resp.status_code == 200

    def test_fulltext_returns_html_fragment(self, seeded_client):
        client = seeded_client
        # Mock the FullTextService to return success
        with patch("bmnews.gui.routes.papers.FullTextService") as MockService:
            instance = MockService.return_value
            instance.fetch_fulltext.return_value = FullTextResult(
                source="europepmc", html="<p>Full text content</p>",
            )
            resp = client.post("/papers/1/fulltext")
            assert resp.status_code == 200
            assert b"Full text content" in resp.data
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_gui_app.py::TestFullTextRoute -v`
Expected: FAIL — route doesn't exist

**Step 3: Add the route**

Add to `bmnews/gui/routes/papers.py`:

```python
@papers_bp.route("/papers/<int:paper_id>/fulltext", methods=["POST"])
def paper_fulltext(paper_id: int):
    """Fetch and display full text for a paper."""
    conn = current_app.config["BMNEWS_DB"]
    paper = get_paper_with_score(conn, paper_id)
    if paper is None:
        abort(404)

    # Check if already cached in DB
    if paper.get("fulltext_html"):
        return render_template("fragments/fulltext_content.html", paper=paper)

    # Extract identifiers
    pmc_id = paper.get("pmcid") or ""
    doi = paper.get("doi") or ""
    pmid = paper.get("pmid") or ""

    # Also try metadata_json
    if not pmc_id or not pmid:
        import json
        meta = json.loads(paper.get("metadata_json") or "{}")
        pmc_id = pmc_id or meta.get("pmcid", "")
        pmid = pmid or meta.get("pmid", "")

    email = current_app.config.get("BMNEWS_EMAIL", "bmnews@example.com")

    from bmlib.fulltext import FullTextService, FullTextError
    service = FullTextService(email=email)

    try:
        result = service.fetch_fulltext(pmc_id=pmc_id or None, doi=doi or None, pmid=pmid)
    except FullTextError:
        return '<div class="fulltext-unavailable"><p>Full text is not available for this paper.</p></div>'

    if result.source == "europepmc" and result.html:
        from bmnews.db.operations import save_fulltext
        save_fulltext(conn, paper_id=paper_id, html=result.html, source="europepmc")
        paper["fulltext_html"] = result.html
        paper["fulltext_source"] = "europepmc"
        return render_template("fragments/fulltext_content.html", paper=paper)

    if result.source == "unpaywall" and result.pdf_url:
        return f'''<div class="fulltext-pdf">
            <p>PDF available from open-access source:</p>
            <a href="{result.pdf_url}" target="_blank" class="btn btn-primary">Open PDF ↗</a>
        </div>'''

    if result.web_url:
        return f'''<div class="fulltext-external">
            <p>Full text available at publisher website:</p>
            <a href="{result.web_url}" target="_blank" class="btn btn-primary">Open Publisher Page ↗</a>
        </div>'''

    return '<div class="fulltext-unavailable"><p>Full text is not available for this paper.</p></div>'
```

**Step 4: Create fulltext content template**

Create `bmnews/gui/templates/fragments/fulltext_content.html`:

```html
<section class="fulltext-section">
    <h2>Full Text <span class="fulltext-source-badge">{{ paper.fulltext_source }}</span></h2>
    <div class="fulltext-content">
        {{ paper.fulltext_html | safe }}
    </div>
</section>
```

**Step 5: Update reading pane template**

Replace `bmnews/gui/templates/fragments/reading_pane.html` with the updated version that includes:
- Formatted abstract (using `format_abstract_html` Jinja2 filter or pre-computed `abstract_html`)
- Full text button with HTMX POST
- Full text display area

Updated template:

```html
<article class="paper-detail">
    <h1 class="paper-title">{{ paper.title }}</h1>

    <div class="paper-meta">
        <span class="authors">{{ paper.authors }}</span>
        <span class="sep">·</span>
        <span class="date">{{ paper.published_date }}</span>
        <span class="sep">·</span>
        <span class="source-badge source-{{ paper.source }}">{{ paper.source }}</span>
    </div>

    <div class="score-section">
        <span class="score-badge {% if paper.relevance_score >= 0.7 %}score-high{% elif paper.relevance_score >= 0.5 %}score-mid{% else %}score-low{% endif %}">
            Relevance: {{ (paper.relevance_score * 100)|int }}%
        </span>
        <span class="tier-badge">{{ paper.quality_tier|replace("_", " ") }}</span>
        {% if paper.study_design %}
        <span class="design-badge">{{ paper.study_design|upper }}</span>
        {% endif %}
    </div>

    {% if paper.summary %}
    <section class="summary-section">
        <h2>Summary</h2>
        <p>{{ paper.summary }}</p>
    </section>
    {% endif %}

    <section class="abstract-section">
        <h2>Abstract</h2>
        {{ paper.abstract | format_abstract | safe }}
    </section>

    <div class="paper-actions">
        {% if paper.url %}
        <a href="{{ paper.url }}" target="_blank" class="btn btn-primary">Open DOI ↗</a>
        {% elif paper.doi %}
        <a href="https://doi.org/{{ paper.doi }}" target="_blank" class="btn btn-primary">Open DOI ↗</a>
        {% endif %}

        {% if paper.fulltext_html %}
        <button class="btn btn-secondary fulltext-toggle" onclick="toggleFulltext()">
            Show Full Text
        </button>
        {% else %}
        <button class="btn btn-secondary"
                hx-post="/papers/{{ paper.id }}/fulltext"
                hx-target="#fulltext-container"
                hx-indicator="#fulltext-spinner"
                hx-swap="innerHTML">
            Get Full Text
        </button>
        <span id="fulltext-spinner" class="htmx-indicator">Loading full text...</span>
        {% endif %}
    </div>

    <div id="fulltext-container">
        {% if paper.fulltext_html %}
        <section class="fulltext-section" id="fulltext-display" style="display:none;">
            <h2>Full Text <span class="fulltext-source-badge">{{ paper.fulltext_source }}</span></h2>
            <div class="fulltext-content">
                {{ paper.fulltext_html | safe }}
            </div>
        </section>
        {% endif %}
    </div>
</article>
```

**Step 6: Register `format_abstract` Jinja filter**

Modify `bmnews/gui/app.py` to register the template filter:

```python
from bmnews.gui.helpers import format_abstract_html

# Inside create_app(), after app creation:
app.jinja_env.filters["format_abstract"] = format_abstract_html
```

**Step 7: Run tests**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_gui_app.py -v`
Expected: All PASS

**Step 8: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add bmnews/gui/routes/papers.py bmnews/gui/templates/fragments/ bmnews/gui/app.py
git commit -m "feat(gui): add full text route, reading pane with formatted abstracts and full text display"
```

---

### Task 11: Full Text CSS Styling

**Files:**
- Modify: `bmnews/gui/static/css/app.css`

**Step 1: Add full text content styles**

Append to `bmnews/gui/static/css/app.css`:

```css
/* Full text content */
.fulltext-section { margin-top: 1.5rem; }
.fulltext-section > h2 {
    font-size: 1rem;
    color: var(--text-muted);
    margin-bottom: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
.fulltext-source-badge {
    font-size: 0.7rem;
    padding: 0.1rem 0.4rem;
    background: #e2d5f1;
    color: #4a1d96;
    border-radius: 3px;
    text-transform: none;
    letter-spacing: 0;
    vertical-align: middle;
}

.fulltext-content h1 { font-size: 1.3rem; margin: 1.5rem 0 0.75rem; }
.fulltext-content h2 { font-size: 1.15rem; margin: 1.25rem 0 0.5rem; color: var(--text); }
.fulltext-content h3 { font-size: 1.05rem; margin: 1rem 0 0.4rem; }
.fulltext-content h4 { font-size: 0.95rem; margin: 0.75rem 0 0.3rem; font-weight: 600; }
.fulltext-content p { line-height: 1.7; margin-bottom: 0.75rem; }
.fulltext-content a { color: var(--primary); text-decoration: none; }
.fulltext-content a:hover { text-decoration: underline; }

/* Tables */
.fulltext-content table {
    width: 100%;
    border-collapse: collapse;
    margin: 1rem 0;
    font-size: 0.85rem;
}
.fulltext-content th, .fulltext-content td {
    border: 1px solid var(--border);
    padding: 0.4rem 0.6rem;
    text-align: left;
}
.fulltext-content th { background: var(--bg-alt); font-weight: 600; }
.fulltext-content tbody tr:nth-child(even) { background: var(--bg-alt); }
.fulltext-content .table-container { overflow-x: auto; margin: 1rem 0; }
.fulltext-content .table-caption {
    font-size: 0.85rem;
    color: var(--text-muted);
    margin-bottom: 0.5rem;
    font-style: italic;
}

/* Figures */
.fulltext-content figure {
    margin: 1.5rem 0;
    text-align: center;
}
.fulltext-content figure img {
    max-width: 100%;
    height: auto;
    border-radius: 4px;
}
.fulltext-content figcaption {
    font-size: 0.85rem;
    color: var(--text-muted);
    margin-top: 0.5rem;
    text-align: left;
}
.fulltext-content figcaption strong { color: var(--text); }

/* References */
.fulltext-content ol.references {
    font-size: 0.85rem;
    line-height: 1.6;
    padding-left: 2rem;
}
.fulltext-content ol.references li { margin-bottom: 0.4rem; }
.fulltext-content ol.references em { font-style: italic; }

/* Inline formatting */
.fulltext-content sub { font-size: 0.75em; vertical-align: sub; }
.fulltext-content sup { font-size: 0.75em; vertical-align: super; }

/* HTMX loading indicator */
.htmx-indicator { display: none; color: var(--text-muted); font-size: 0.85rem; }
.htmx-request .htmx-indicator, .htmx-request.htmx-indicator { display: inline; }

/* Full text status messages */
.fulltext-unavailable, .fulltext-pdf, .fulltext-external {
    margin-top: 1rem;
    padding: 1rem;
    border-radius: 6px;
    background: var(--bg-alt);
    border: 1px solid var(--border);
}
.fulltext-unavailable { color: var(--text-muted); }

/* Full text toggle */
.fulltext-toggle { margin-left: 0.5rem; }
```

**Step 2: Add toggle script**

Add a small inline script or to `app.js` for toggling cached full text:

```javascript
function toggleFulltext() {
    const el = document.getElementById('fulltext-display');
    const btn = document.querySelector('.fulltext-toggle');
    if (el.style.display === 'none') {
        el.style.display = 'block';
        btn.textContent = 'Hide Full Text';
    } else {
        el.style.display = 'none';
        btn.textContent = 'Show Full Text';
    }
}
```

**Step 3: Visually verify**

Run the app and check a paper with a PMC ID to verify styling.

**Step 4: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add bmnews/gui/static/css/app.css bmnews/gui/static/js/app.js
git commit -m "feat(gui): add full text content CSS and toggle behavior"
```

---

### Task 12: Integration Test — End-to-End Full Text Flow

**Files:**
- Create: `tests/test_fulltext_integration.py`

**Step 1: Write integration test**

```python
"""Integration test for full text retrieval and display."""

from unittest.mock import patch, MagicMock
from pathlib import Path

import pytest

from bmlib.db import connect_sqlite
from bmlib.fulltext import FullTextResult
from bmnews.db.schema import init_db
from bmnews.db.operations import upsert_paper, save_score
from bmnews.gui.app import create_app
from bmnews.config import AppConfig


@pytest.fixture
def app_with_paper():
    config = AppConfig()
    conn = connect_sqlite(":memory:")
    init_db(conn)
    app = create_app(config, conn)
    app.config["TESTING"] = True

    pid = upsert_paper(
        conn, doi="10.1/integ", title="Integration Test Paper",
        authors="Smith J, Doe A", abstract="Background: Test. Methods: Test.",
        source="europepmc", metadata_json='{"pmid":"12345","pmcid":"PMC999"}',
    )
    save_score(conn, paper_id=pid, combined_score=0.8, relevance_score=0.9)

    return app, conn, pid


class TestEndToEnd:
    def test_reading_pane_shows_formatted_abstract(self, app_with_paper):
        app, conn, pid = app_with_paper
        with app.test_client() as client:
            resp = client.get(f"/papers/{pid}")
            assert resp.status_code == 200
            assert b"<strong>Background:</strong>" in resp.data

    def test_fulltext_button_present(self, app_with_paper):
        app, conn, pid = app_with_paper
        with app.test_client() as client:
            resp = client.get(f"/papers/{pid}")
            assert b"Get Full Text" in resp.data
            assert b"hx-post" in resp.data

    def test_fulltext_fetches_and_caches(self, app_with_paper):
        app, conn, pid = app_with_paper
        with app.test_client() as client:
            with patch("bmnews.gui.routes.papers.FullTextService") as MockSvc:
                instance = MockSvc.return_value
                instance.fetch_fulltext.return_value = FullTextResult(
                    source="europepmc",
                    html="<h2>Introduction</h2><p>Full text body.</p>",
                )
                resp = client.post(f"/papers/{pid}/fulltext")
                assert resp.status_code == 200
                assert b"Full text body" in resp.data

            # Second request should use cached version (no service call)
            with patch("bmnews.gui.routes.papers.FullTextService") as MockSvc2:
                resp2 = client.get(f"/papers/{pid}")
                assert b"Full text body" in resp2.data or b"Show Full Text" in resp2.data
```

**Step 2: Run test**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_fulltext_integration.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add tests/test_fulltext_integration.py
git commit -m "test: add end-to-end integration tests for full text flow"
```

---

### Task 13: Run All Tests — Both Projects

**Step 1: Run bmlib tests**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/ -v`
Expected: All PASS (including new fulltext tests)

**Step 2: Run BioMedicalNews tests**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/ -v`
Expected: All PASS (including new migration, operations, GUI, integration tests)

**Step 3: Final commit if any fixes needed**

Fix any failing tests, then:

```bash
# In both repos
git add -A && git commit -m "fix: address test failures"
```
