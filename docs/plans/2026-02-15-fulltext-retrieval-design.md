# Full Text Retrieval & Display Design

**Date:** 2026-02-15
**Template:** Swift BioMedLit library at `/Users/hherb/src/bmlibrarian_lite/Packages/BioMedLit`

## Problem

The newsreader currently displays only paper abstracts as plain text with a single "Open DOI" link. Users need to:
1. Read full text articles inline in the reading pane
2. Download and view PDFs for papers without open-access XML
3. See better-formatted abstracts with structured sections (Background, Methods, Results, etc.)

## Architecture

### Component 1: bmlib.fulltext Module (Shared Library)

New module at `bmlib/bmlib/fulltext/` porting the proven Swift BioMedLit architecture to Python.

#### `models.py` — Data Models

```python
@dataclass
class JATSAbstractSection:
    title: str
    content: str

@dataclass
class JATSBodySection:
    title: str
    paragraphs: list[str]
    subsections: list[JATSBodySection]

@dataclass
class JATSFigureInfo:
    id: str
    label: str
    caption: str
    graphic_url: str | None

@dataclass
class JATSTableInfo:
    id: str
    label: str
    caption: str
    html_content: str  # Pre-rendered HTML table

@dataclass
class JATSReferenceInfo:
    id: str
    label: str
    authors: list[str]
    article_title: str
    source: str  # journal name
    year: str
    volume: str
    issue: str
    first_page: str
    last_page: str
    doi: str
    pmid: str

@dataclass
class JATSArticle:
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
    source: str  # "europepmc", "unpaywall", "doi", "cached"
    html: str | None = None       # For europepmc
    pdf_url: str | None = None    # For unpaywall
    web_url: str | None = None    # For doi
    file_path: str | None = None  # For cached
```

#### `jats_parser.py` — JATS XML → HTML Parser

Python port of Swift `JATSXMLParser` using `xml.sax` (event-driven, same pattern as Swift's XMLParser delegate).

Handles:
- **Metadata:** title, authors (surname + given names), journal, volume/issue, year, DOI, PMID, PMC ID
- **Structured abstracts:** Multi-section with labeled sections (Background, Methods, Results, Conclusions)
- **Body sections:** Hierarchical with nested subsections and paragraphs
- **Tables:** JATS `<table-wrap>` → HTML `<table>` with thead/tbody, colspan/rowspan
- **Figures:** Extract label, caption, graphic URL; construct Europe PMC image URLs:
  `https://europepmc.org/articles/PMC{id}/bin/{graphic}.jpg`
- **References:** Structured bibliographic data from `<ref-list>`
- **Inline formatting:** bold, italic, subscript, superscript, monospace
- **Cross-references:** `<xref>` tags → anchor links

Key internal state (mirroring Swift):
- `text_stack: list[str]` — accumulates nested element text
- `element_stack: list[str]` — tracks XML hierarchy
- `section_stack: list[SectionBuilder]` — builds nested body sections
- Builder classes for tables, figures, references

Output: HTML string suitable for direct rendering in the reading pane.

#### `service.py` — 3-Tier Full Text Retrieval

```
Tier 1: Europe PMC (preferred — machine-readable XML)
  URL: https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcId}/fullTextXML
  Process: Fetch XML → JATSParser → HTML
  Returns: FullTextResult(source="europepmc", html=...)

Tier 2: Unpaywall (fallback — open-access PDF)
  URL: https://api.unpaywall.org/v2/{doi}?email={email}
  Process: Query API → extract best_oa_location.url_for_pdf
  Returns: FullTextResult(source="unpaywall", pdf_url=...)

Tier 3: DOI Resolution (last resort — publisher website)
  URL: https://doi.org/{doi}
  Returns: FullTextResult(source="doi", web_url=...)
```

Requires at least one of: PMC ID, DOI, or PMID.
Uses `httpx` with retry logic for transient failures (429, 5xx).

#### `cache.py` — PDF Caching

- Cache directory: configurable, default `~/.bmnews/pdfs/`
- Download with PDF magic byte validation (`%PDF`)
- Lookup by PMID or DOI (slugified)
- Clear/delete operations

### Component 2: BioMedicalNews Changes

#### Database Migration (M003)

Add columns to `papers` table:
```sql
ALTER TABLE papers ADD COLUMN pmid TEXT;
ALTER TABLE papers ADD COLUMN pmcid TEXT;
ALTER TABLE papers ADD COLUMN fulltext_html TEXT;
ALTER TABLE papers ADD COLUMN fulltext_source TEXT DEFAULT '';
```

Backfill from existing `metadata_json`:
```sql
UPDATE papers SET
  pmid = json_extract(metadata_json, '$.pmid'),
  pmcid = json_extract(metadata_json, '$.pmcid')
WHERE source = 'europepmc'
  AND metadata_json != '{}';
```

#### Fetcher Changes

Update EuropePMC fetcher to store `pmid` and `pmcid` as top-level columns
in addition to metadata_json.

Update bioRxiv/medRxiv fetchers to store DOI-derived identifiers for
potential Unpaywall lookup.

#### Route: `POST /papers/<id>/fulltext`

New endpoint in `papers.py`:
1. Look up paper by ID, extract pmcid/doi/pmid
2. Call `bmlib.fulltext.service.fetch_fulltext(pmcid, doi, pmid, email)`
3. On Europe PMC success: store HTML in `fulltext_html`, return HTML fragment
4. On Unpaywall success: download PDF to cache, open in system viewer
5. On DOI fallback: open URL in system browser
6. On failure: return "not available" message
7. Cache result in DB so subsequent clicks are instant

#### Reading Pane Template Changes

**Abstract formatting:**
```html
<section class="abstract-section">
    <h2>Abstract</h2>
    {{ paper.abstract_html | safe }}
</section>
```

Where `abstract_html` is generated by detecting structured sections in the
abstract text (lines starting with "Background:", "Methods:", etc.) and
wrapping labels in `<strong>` tags, with `<br>` between sections.

**Full text button + display:**
```html
{% if not paper.fulltext_html %}
<div class="paper-actions">
    <button class="btn btn-secondary"
            hx-post="/papers/{{ paper.id }}/fulltext"
            hx-target="#fulltext-container"
            hx-indicator="#fulltext-spinner">
        Get Full Text
    </button>
    <span id="fulltext-spinner" class="htmx-indicator">Loading...</span>
</div>
<div id="fulltext-container"></div>
{% else %}
<section class="fulltext-section">
    <h2>Full Text</h2>
    <div class="fulltext-content">
        {{ paper.fulltext_html | safe }}
    </div>
</section>
{% endif %}
```

**Full text content CSS:**
New styles in `app.css` for `.fulltext-content`:
- Headings (h2-h5) with appropriate sizing and margins
- Tables with borders, padding, striped rows
- Figures with centered images, caption styling
- References as numbered list with DOI links
- Inline formatting (sub/sup, monospace)
- Responsive images (max-width: 100%)

### Flow Diagram

```
User clicks "Get Full Text"
  → HTMX POST /papers/<id>/fulltext
  → Route handler extracts pmcid/doi/pmid from paper
  → bmlib.fulltext.service.fetch_fulltext()
    → Try Europe PMC XML
      → Success: JATSParser → HTML → store in DB → return HTML fragment
    → Try Unpaywall
      → Success: return PDF URL → download + open externally
    → Try DOI
      → Success: return web URL → open in browser
    → Failure: return "not available" message
  → HTMX swaps result into #fulltext-container
```

### Dependencies

- `httpx` (already used by bmnews fetchers)
- `xml.sax` (Python stdlib, no new dependency)
- No new pip packages required

### Testing Strategy

- Unit tests for JATS parser with sample XML fixtures
- Unit tests for service with mocked HTTP responses
- Integration test for the full retrieval chain
