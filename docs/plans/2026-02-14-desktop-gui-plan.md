# Desktop GUI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a desktop GUI for BioMedicalNews with an email-client-style layout using pywebview + Flask + HTMX.

**Architecture:** Flask runs as a local HTTP server in a daemon thread, serving Jinja2 HTML fragments. HTMX swaps fragments into the DOM on user interaction. pywebview wraps it all in a native desktop window. All data access goes through existing `bmnews.db.operations` functions.

**Tech Stack:** pywebview >=5.0, Flask >=3.0, HTMX 2.x (vendored), Split.js (vendored), Jinja2 (existing)

**Design doc:** `docs/plans/2026-02-14-desktop-gui-design.md`

---

### Task 1: Add GUI Dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml:32-38`

**Step 1: Add the gui optional dependency group**

In `pyproject.toml`, add a `gui` entry to `[project.optional-dependencies]` and include it in `all`:

```toml
gui = ["pywebview>=5.0", "flask>=3.0"]
```

Update the `all` line to include `gui`:

```toml
all = ["bmnews[anthropic,ollama,postgresql,transparency,gui,dev]"]
```

**Step 2: Install the new deps**

Run: `pip install -e ".[gui]"`
Expected: both `pywebview` and `flask` install successfully

**Step 3: Verify imports**

Run: `python -c "import webview; import flask; print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat(gui): add pywebview and flask as optional gui dependencies"
```

---

### Task 2: Vendor HTMX and Split.js Static Assets

**Files:**
- Create: `bmnews/gui/__init__.py` (empty)
- Create: `bmnews/gui/static/vendor/htmx.min.js`
- Create: `bmnews/gui/static/vendor/split-grid.min.js`

**Step 1: Create the gui package directory structure**

```bash
mkdir -p bmnews/gui/static/vendor
mkdir -p bmnews/gui/static/css
mkdir -p bmnews/gui/static/js
mkdir -p bmnews/gui/templates/fragments
mkdir -p bmnews/gui/routes
touch bmnews/gui/__init__.py
touch bmnews/gui/routes/__init__.py
```

**Step 2: Download HTMX**

Run: `curl -o bmnews/gui/static/vendor/htmx.min.js https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`

Verify: `wc -c bmnews/gui/static/vendor/htmx.min.js`
Expected: ~14-16KB file

**Step 3: Download Split.js (grid version)**

Run: `curl -o bmnews/gui/static/vendor/split-grid.min.js https://unpkg.com/split-grid@1.0.11/dist/split-grid.min.js`

Verify: `wc -c bmnews/gui/static/vendor/split-grid.min.js`
Expected: ~4-6KB file

**Step 4: Commit**

```bash
git add bmnews/gui/
git commit -m "feat(gui): scaffold gui package and vendor htmx + split-grid"
```

---

### Task 3: New DB Query — get_paper_with_score

**Files:**
- Modify: `bmnews/db/operations.py`
- Modify: `tests/test_db.py`

**Step 1: Write the failing test**

Add to `tests/test_db.py`:

```python
class TestPaperWithScore:
    def test_returns_paper_and_score_data(self):
        conn = _db()
        pid = upsert_paper(
            conn, doi="10.1101/ws1", title="With Score",
            authors="Doe J", abstract="Abstract text.",
            source="medrxiv", published_date="2026-01-15",
        )
        save_score(
            conn, paper_id=pid, relevance_score=0.85,
            quality_score=0.7, combined_score=0.79,
            summary="A summary.", study_design="cohort",
            quality_tier="TIER_3_CONTROLLED",
        )
        result = get_paper_with_score(conn, pid)
        assert result is not None
        assert result["title"] == "With Score"
        assert result["relevance_score"] == 0.85
        assert result["summary"] == "A summary."
        assert result["study_design"] == "cohort"

    def test_returns_none_for_missing_id(self):
        conn = _db()
        result = get_paper_with_score(conn, 9999)
        assert result is None

    def test_returns_none_for_unscored_paper(self):
        conn = _db()
        pid = upsert_paper(conn, doi="10.1101/ws2", title="No Score")
        result = get_paper_with_score(conn, pid)
        assert result is None
```

Add `get_paper_with_score` to the import at top of `tests/test_db.py`.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::TestPaperWithScore -v`
Expected: ImportError — `get_paper_with_score` not defined yet

**Step 3: Implement get_paper_with_score**

Add to `bmnews/db/operations.py` after `get_paper_by_doi`:

```python
def get_paper_with_score(conn: Any, paper_id: int) -> dict | None:
    """Fetch a single paper with its score data joined. Returns dict or None."""
    ph = _placeholder(conn)
    row = fetch_one(
        conn,
        f"""
        SELECT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier, s.assessment_json
        FROM papers p
        JOIN scores s ON s.paper_id = p.id
        WHERE p.id = {ph}
        """,
        (paper_id,),
    )
    return _row_to_dict(row) if row else None
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py::TestPaperWithScore -v`
Expected: 3 passed

**Step 5: Run full test suite**

Run: `pytest tests/test_db.py -v`
Expected: all pass

**Step 6: Commit**

```bash
git add bmnews/db/operations.py tests/test_db.py
git commit -m "feat(db): add get_paper_with_score query for GUI reading pane"
```

---

### Task 4: New DB Query — get_papers_filtered (sorted, filtered, paginated)

The GUI needs a more flexible query than `get_scored_papers` — it needs sorting, filtering by source/tier/design, pagination with offset, and search.

**Files:**
- Modify: `bmnews/db/operations.py`
- Modify: `tests/test_db.py`

**Step 1: Write the failing tests**

Add to `tests/test_db.py`:

```python
class TestPapersFiltered:
    def _seed(self, conn):
        """Insert 3 papers with different scores, sources, tiers."""
        p1 = upsert_paper(conn, doi="10.1101/f1", title="Alpha Paper",
                          authors="Smith", abstract="Cancer immunotherapy trial",
                          source="medrxiv", published_date="2026-02-10")
        save_score(conn, paper_id=p1, relevance_score=0.9, quality_score=0.8,
                   combined_score=0.86, study_design="rct",
                   quality_tier="TIER_4_EXPERIMENTAL", summary="Sum1")

        p2 = upsert_paper(conn, doi="10.1101/f2", title="Beta Paper",
                          authors="Jones", abstract="Genomics cohort study",
                          source="biorxiv", published_date="2026-02-12")
        save_score(conn, paper_id=p2, relevance_score=0.7, quality_score=0.6,
                   combined_score=0.66, study_design="cohort",
                   quality_tier="TIER_3_CONTROLLED", summary="Sum2")

        p3 = upsert_paper(conn, doi="10.1101/f3", title="Gamma Paper",
                          authors="Lee", abstract="Case report on rare disease",
                          source="europepmc", published_date="2026-02-14")
        save_score(conn, paper_id=p3, relevance_score=0.5, quality_score=0.3,
                   combined_score=0.42, study_design="case_report",
                   quality_tier="TIER_1_ANECDOTAL", summary="Sum3")
        return p1, p2, p3

    def test_default_returns_all_sorted_by_combined(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn)
        assert len(results) == 3
        assert results[0]["doi"] == "10.1101/f1"  # highest combined

    def test_sort_by_date(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn, sort="date")
        assert results[0]["doi"] == "10.1101/f3"  # most recent

    def test_filter_by_source(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn, source="medrxiv")
        assert len(results) == 1
        assert results[0]["doi"] == "10.1101/f1"

    def test_filter_by_quality_tier(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn, quality_tier="TIER_4_EXPERIMENTAL")
        assert len(results) == 1

    def test_search_query(self):
        conn = _db()
        self._seed(conn)
        results = get_papers_filtered(conn, search="immunotherapy")
        assert len(results) == 1
        assert results[0]["doi"] == "10.1101/f1"

    def test_pagination(self):
        conn = _db()
        self._seed(conn)
        page1 = get_papers_filtered(conn, limit=2, offset=0)
        page2 = get_papers_filtered(conn, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 1

    def test_returns_total_count(self):
        conn = _db()
        self._seed(conn)
        results, total = get_papers_filtered(conn, limit=2, offset=0, with_total=True)
        assert len(results) == 2
        assert total == 3
```

Add `get_papers_filtered` to the import at top of `tests/test_db.py`.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::TestPapersFiltered -v`
Expected: ImportError

**Step 3: Implement get_papers_filtered**

Add to `bmnews/db/operations.py`:

```python
def get_papers_filtered(
    conn: Any,
    *,
    sort: str = "combined",
    source: str = "",
    quality_tier: str = "",
    study_design: str = "",
    search: str = "",
    limit: int = 20,
    offset: int = 0,
    with_total: bool = False,
) -> list[dict] | tuple[list[dict], int]:
    """Flexible paper query with sorting, filtering, search, and pagination.

    Args:
        sort: One of 'combined', 'relevance', 'quality', 'date'.
        source: Filter by source (e.g., 'medrxiv').
        quality_tier: Filter by quality tier (e.g., 'TIER_4_EXPERIMENTAL').
        study_design: Filter by study design (e.g., 'rct').
        search: Search title and abstract (LIKE match).
        limit: Max results per page.
        offset: Skip this many results.
        with_total: If True, return (results, total_count) tuple.

    Returns:
        List of paper dicts, or (list, int) if with_total is True.
    """
    ph = _placeholder(conn)
    params: list = []
    conditions: list[str] = []

    if source:
        conditions.append(f"p.source = {ph}")
        params.append(source)
    if quality_tier:
        conditions.append(f"s.quality_tier = {ph}")
        params.append(quality_tier)
    if study_design:
        conditions.append(f"s.study_design = {ph}")
        params.append(study_design)
    if search:
        conditions.append(f"(p.title LIKE {ph} OR p.abstract LIKE {ph})")
        params.extend([f"%{search}%", f"%{search}%"])

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    sort_map = {
        "combined": "s.combined_score DESC",
        "relevance": "s.relevance_score DESC",
        "quality": "s.quality_score DESC",
        "date": "p.published_date DESC",
    }
    order_by = sort_map.get(sort, "s.combined_score DESC")

    base_query = f"""
        FROM papers p
        JOIN scores s ON s.paper_id = p.id
        {where}
    """

    if with_total:
        total = fetch_scalar(
            conn,
            f"SELECT COUNT(*) {base_query}",
            tuple(params),
        )

    rows = fetch_all(
        conn,
        f"""
        SELECT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier
        {base_query}
        ORDER BY {order_by}
        LIMIT {ph} OFFSET {ph}
        """,
        tuple(params + [limit, offset]),
    )

    results = [_row_to_dict(r) for r in rows]
    if with_total:
        return results, total or 0
    return results
```

Also add `fetch_scalar` to the import at the top of `operations.py` if not already there (it is already imported).

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py::TestPapersFiltered -v`
Expected: 7 passed

**Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: all pass

**Step 6: Commit**

```bash
git add bmnews/db/operations.py tests/test_db.py
git commit -m "feat(db): add get_papers_filtered with sort, filter, search, pagination"
```

---

### Task 5: Flask App Factory

**Files:**
- Create: `bmnews/gui/app.py`
- Create: `tests/test_gui_app.py`

**Step 1: Write the failing test**

Create `tests/test_gui_app.py`:

```python
"""Tests for the GUI Flask app factory."""

from __future__ import annotations

import pytest
from bmlib.db import connect_sqlite
from bmnews.config import AppConfig
from bmnews.db.schema import init_db


@pytest.fixture
def app():
    """Create a test Flask app with in-memory SQLite."""
    from bmnews.gui.app import create_app

    config = AppConfig()
    conn = connect_sqlite(":memory:")
    init_db(conn)
    app = create_app(config, conn)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestAppFactory:
    def test_creates_flask_app(self, app):
        assert app is not None
        assert app.config["TESTING"] is True

    def test_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"htmx" in resp.data.lower() or b"HTMX" in resp.data

    def test_static_files_served(self, client):
        resp = client.get("/static/vendor/htmx.min.js")
        assert resp.status_code == 200
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_gui_app.py -v`
Expected: ImportError — `bmnews.gui.app` not found

**Step 3: Implement the app factory**

Create `bmnews/gui/app.py`:

```python
"""Flask application factory for the BioMedicalNews GUI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask

from bmnews.config import AppConfig

logger = logging.getLogger(__name__)

GUI_DIR = Path(__file__).parent
TEMPLATES_DIR = GUI_DIR / "templates"
STATIC_DIR = GUI_DIR / "static"


def create_app(config: AppConfig, conn: Any) -> Flask:
    """Create and configure the Flask application.

    Args:
        config: Application configuration.
        conn: Open database connection.

    Returns:
        Configured Flask app.
    """
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=str(STATIC_DIR),
    )

    # Store config and db connection in app context
    app.config["BMNEWS_CONFIG"] = config
    app.config["BMNEWS_DB"] = conn

    # Register blueprints
    from bmnews.gui.routes.papers import papers_bp
    from bmnews.gui.routes.settings import settings_bp
    from bmnews.gui.routes.pipeline import pipeline_bp

    app.register_blueprint(papers_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(pipeline_bp)

    # Root route serves the main page shell
    @app.route("/")
    def index():
        from flask import render_template
        return render_template("base.html")

    return app
```

**Step 4: Create stub blueprints and base template**

Create `bmnews/gui/routes/papers.py`:

```python
"""Paper list and reading pane routes."""

from flask import Blueprint

papers_bp = Blueprint("papers", __name__)
```

Create `bmnews/gui/routes/settings.py`:

```python
"""Settings and template editor routes."""

from flask import Blueprint

settings_bp = Blueprint("settings", __name__)
```

Create `bmnews/gui/routes/pipeline.py`:

```python
"""Pipeline execution routes."""

from flask import Blueprint

pipeline_bp = Blueprint("pipeline", __name__)
```

Create `bmnews/gui/templates/base.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BioMedical News</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='css/app.css') }}">
    <script src="{{ url_for('static', filename='vendor/htmx.min.js') }}"></script>
    <script src="{{ url_for('static', filename='vendor/split-grid.min.js') }}"></script>
</head>
<body>
    <nav class="tab-bar">
        <a href="/" class="tab active" hx-boost="true">Papers</a>
        <a href="/settings" class="tab" hx-boost="true">Settings</a>
        <div class="spacer"></div>
        <input type="search" name="q" placeholder="Search..."
               hx-get="/search" hx-target="#paper-list" hx-swap="innerHTML"
               hx-trigger="input changed delay:300ms">
    </nav>

    <main class="split-container" id="main-content">
        <div class="paper-list-pane" id="paper-list-pane">
            <div class="list-controls">
                <select name="sort" hx-get="/papers" hx-target="#paper-list"
                        hx-swap="innerHTML" hx-include="[name='source'],[name='tier']">
                    <option value="combined">Combined Score</option>
                    <option value="relevance">Relevance</option>
                    <option value="quality">Quality</option>
                    <option value="date">Date</option>
                </select>
            </div>
            <div id="paper-list" hx-get="/papers" hx-trigger="load" hx-swap="innerHTML">
            </div>
        </div>

        <div class="gutter" id="gutter"></div>

        <div class="reading-pane" id="reading-pane-pane">
            <div id="reading-pane" class="reading-pane-content">
                <div class="empty-state">
                    <p>Select a paper from the list to read its details.</p>
                </div>
            </div>
        </div>
    </main>

    <footer class="status-bar" id="status-bar">
        <span>Ready</span>
    </footer>

    <script src="{{ url_for('static', filename='js/app.js') }}"></script>
</body>
</html>
```

Create `bmnews/gui/static/css/app.css` (minimal to pass tests — full styling in Task 9):

```css
/* Minimal layout styles — expanded in Task 9 */
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
}

.tab-bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    border-bottom: 1px solid #ddd;
    background: #f8f9fa;
}

.tab-bar .tab { text-decoration: none; padding: 0.25rem 0.75rem; color: #333; }
.tab-bar .tab.active { font-weight: bold; border-bottom: 2px solid #0066cc; }
.tab-bar .spacer { flex: 1; }
.tab-bar input[type="search"] { padding: 0.25rem 0.5rem; border: 1px solid #ccc; border-radius: 4px; }

.split-container {
    display: grid;
    grid-template-columns: 1fr 6px 2fr;
    flex: 1;
    overflow: hidden;
}

.paper-list-pane { overflow-y: auto; border-right: 1px solid #e0e0e0; }
.gutter { cursor: col-resize; background: #e0e0e0; }
.reading-pane { overflow-y: auto; }
.reading-pane-content { padding: 1rem; }
.empty-state { color: #888; text-align: center; margin-top: 4rem; }

.status-bar {
    padding: 0.25rem 1rem;
    border-top: 1px solid #ddd;
    background: #f8f9fa;
    font-size: 0.85rem;
    color: #666;
}
```

Create `bmnews/gui/static/js/app.js`:

```javascript
/* Split pane initialization and minor UI glue. */
document.addEventListener("DOMContentLoaded", function () {
    if (typeof Split !== "undefined") {
        Split({
            columnGutters: [{
                track: 1,
                element: document.getElementById("gutter"),
            }],
        });
    }
});
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/test_gui_app.py -v`
Expected: 3 passed

**Step 6: Commit**

```bash
git add bmnews/gui/ tests/test_gui_app.py
git commit -m "feat(gui): flask app factory with base template and stub routes"
```

---

### Task 6: Papers Route — List and Reading Pane

**Files:**
- Modify: `bmnews/gui/routes/papers.py`
- Create: `bmnews/gui/templates/fragments/paper_list.html`
- Create: `bmnews/gui/templates/fragments/paper_card.html`
- Create: `bmnews/gui/templates/fragments/reading_pane.html`
- Modify: `tests/test_gui_app.py`

**Step 1: Write the failing tests**

Add to `tests/test_gui_app.py`:

```python
from bmnews.db.operations import upsert_paper, save_score


@pytest.fixture
def seeded_client(app):
    """Client with some papers in the database."""
    conn = app.config["BMNEWS_DB"]
    p1 = upsert_paper(conn, doi="10.1101/g1", title="Alpha Paper",
                       authors="Smith J", abstract="Cancer immunotherapy.",
                       source="medrxiv", published_date="2026-02-10")
    save_score(conn, paper_id=p1, relevance_score=0.9, quality_score=0.8,
               combined_score=0.86, summary="A strong trial.",
               study_design="rct", quality_tier="TIER_4_EXPERIMENTAL")

    p2 = upsert_paper(conn, doi="10.1101/g2", title="Beta Paper",
                       authors="Jones K", abstract="Genomics study.",
                       source="biorxiv", published_date="2026-02-12")
    save_score(conn, paper_id=p2, relevance_score=0.6, quality_score=0.5,
               combined_score=0.56, summary="Interesting cohort.",
               study_design="cohort", quality_tier="TIER_3_CONTROLLED")
    return app.test_client()


class TestPapersRoute:
    def test_papers_list(self, seeded_client):
        resp = seeded_client.get("/papers")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data
        assert b"Beta Paper" in resp.data

    def test_papers_sorted_by_date(self, seeded_client):
        resp = seeded_client.get("/papers?sort=date")
        assert resp.status_code == 200
        # Beta (2026-02-12) should come before Alpha (2026-02-10) when sorted by date desc
        alpha_pos = resp.data.index(b"Alpha Paper")
        beta_pos = resp.data.index(b"Beta Paper")
        assert beta_pos < alpha_pos

    def test_papers_filter_by_source(self, seeded_client):
        resp = seeded_client.get("/papers?source=medrxiv")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data
        assert b"Beta Paper" not in resp.data

    def test_paper_detail(self, seeded_client):
        # Get paper list first to know the ID
        conn = seeded_client.application.config["BMNEWS_DB"]
        from bmnews.db.operations import get_paper_by_doi
        paper = get_paper_by_doi(conn, "10.1101/g1")
        resp = seeded_client.get(f"/papers/{paper['id']}")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data
        assert b"Cancer immunotherapy" in resp.data
        assert b"A strong trial" in resp.data

    def test_paper_detail_not_found(self, seeded_client):
        resp = seeded_client.get("/papers/99999")
        assert resp.status_code == 404

    def test_search(self, seeded_client):
        resp = seeded_client.get("/search?q=immunotherapy")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data
        assert b"Beta Paper" not in resp.data

    def test_papers_more_pagination(self, seeded_client):
        resp = seeded_client.get("/papers/more?offset=0&limit=1")
        assert resp.status_code == 200
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gui_app.py::TestPapersRoute -v`
Expected: 404 errors — routes not implemented yet

**Step 3: Implement the papers blueprint**

Replace `bmnews/gui/routes/papers.py`:

```python
"""Paper list and reading pane routes."""

from __future__ import annotations

from flask import Blueprint, current_app, render_template, request, abort

from bmnews.db.operations import get_papers_filtered, get_paper_with_score

papers_bp = Blueprint("papers", __name__)


@papers_bp.route("/papers")
def paper_list():
    """Return the paper list fragment."""
    conn = current_app.config["BMNEWS_DB"]
    sort = request.args.get("sort", "combined")
    source = request.args.get("source", "")
    tier = request.args.get("tier", "")
    design = request.args.get("design", "")

    papers, total = get_papers_filtered(
        conn, sort=sort, source=source, quality_tier=tier,
        study_design=design, limit=20, offset=0, with_total=True,
    )

    return render_template(
        "fragments/paper_list.html",
        papers=papers, total=total, offset=0, limit=20,
        sort=sort, source=source, tier=tier, design=design,
    )


@papers_bp.route("/papers/more")
def paper_list_more():
    """Return additional paper cards for infinite scroll / load-more."""
    conn = current_app.config["BMNEWS_DB"]
    sort = request.args.get("sort", "combined")
    source = request.args.get("source", "")
    tier = request.args.get("tier", "")
    design = request.args.get("design", "")
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 20, type=int)

    papers = get_papers_filtered(
        conn, sort=sort, source=source, quality_tier=tier,
        study_design=design, limit=limit, offset=offset,
    )

    return render_template(
        "fragments/paper_list.html",
        papers=papers, total=None, offset=offset, limit=limit,
        sort=sort, source=source, tier=tier, design=design,
        append=True,
    )


@papers_bp.route("/papers/<int:paper_id>")
def paper_detail(paper_id: int):
    """Return the reading pane fragment for a single paper."""
    conn = current_app.config["BMNEWS_DB"]
    paper = get_paper_with_score(conn, paper_id)
    if paper is None:
        abort(404)
    return render_template("fragments/reading_pane.html", paper=paper)


@papers_bp.route("/search")
def search():
    """Search papers by query string."""
    conn = current_app.config["BMNEWS_DB"]
    q = request.args.get("q", "").strip()
    if not q:
        return paper_list()

    papers, total = get_papers_filtered(
        conn, search=q, limit=20, offset=0, with_total=True,
    )

    return render_template(
        "fragments/paper_list.html",
        papers=papers, total=total, offset=0, limit=20,
        sort="combined", source="", tier="", design="",
    )
```

**Step 4: Create the fragment templates**

Create `bmnews/gui/templates/fragments/paper_card.html`:

```html
<div class="paper-card {% if selected %}selected{% endif %}"
     hx-get="/papers/{{ paper.id }}"
     hx-target="#reading-pane"
     hx-swap="innerHTML">
    <div class="card-title">{{ paper.title|truncate(80) }}</div>
    <div class="card-meta">
        <span class="source-badge source-{{ paper.source }}">{{ paper.source }}</span>
        <span class="date">{{ paper.published_date }}</span>
    </div>
    <div class="card-score">
        <span class="score-badge" style="--score: {{ paper.combined_score }}">
            {{ (paper.combined_score * 100)|int }}%
        </span>
        <span class="tier-badge">{{ paper.quality_tier|replace("_", " ")|replace("TIER ", "T") }}</span>
    </div>
</div>
```

Create `bmnews/gui/templates/fragments/paper_list.html`:

```html
{% for paper in papers %}
    {% include "fragments/paper_card.html" %}
{% endfor %}

{% if papers|length == 0 %}
    <div class="empty-state">
        <p>No papers found.</p>
    </div>
{% endif %}

{% if papers|length >= limit and not append %}
    <button class="load-more"
            hx-get="/papers/more?offset={{ offset + limit }}&sort={{ sort }}&source={{ source }}&tier={{ tier }}&design={{ design }}"
            hx-target="this"
            hx-swap="outerHTML">
        Load more
    </button>
{% endif %}

{% if total is not none %}
<div id="paper-count" hx-swap-oob="innerHTML:#status-bar">
    Showing {{ papers|length }} of {{ total }} papers · Sorted by {{ sort }}
</div>
{% endif %}
```

Create `bmnews/gui/templates/fragments/reading_pane.html`:

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
        <span class="score-badge relevance">Relevance: {{ (paper.relevance_score * 100)|int }}%</span>
        <span class="score-badge quality">Quality: {{ paper.quality_tier|replace("_", " ") }}</span>
        <span class="score-badge design">{{ paper.study_design|upper }}</span>
    </div>

    {% if paper.summary %}
    <section class="summary-section">
        <h2>Summary</h2>
        <p>{{ paper.summary }}</p>
    </section>
    {% endif %}

    <section class="abstract-section">
        <h2>Abstract</h2>
        <p>{{ paper.abstract }}</p>
    </section>

    <div class="paper-actions">
        {% if paper.url %}
        <a href="{{ paper.url }}" target="_blank" class="btn btn-primary">Open DOI ↗</a>
        {% elif paper.doi %}
        <a href="https://doi.org/{{ paper.doi }}" target="_blank" class="btn btn-primary">Open DOI ↗</a>
        {% endif %}
    </div>
</article>
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/test_gui_app.py -v`
Expected: all pass

**Step 6: Commit**

```bash
git add bmnews/gui/ tests/test_gui_app.py
git commit -m "feat(gui): paper list, detail, and search routes with HTMX templates"
```

---

### Task 7: Settings Route — Config Display and Save

**Files:**
- Modify: `bmnews/gui/routes/settings.py`
- Create: `bmnews/gui/templates/fragments/settings.html`
- Create: `bmnews/gui/templates/fragments/template_editor.html`
- Modify: `tests/test_gui_app.py`
- Modify: `bmnews/config.py` (add `save_config` function)

**Step 1: Write the failing tests**

Add to `tests/test_gui_app.py`:

```python
class TestSettingsRoute:
    def test_settings_page(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert b"Sources" in resp.data or b"sources" in resp.data

    def test_save_settings(self, client):
        resp = client.post("/settings/save", data={
            "sources.lookback_days": "14",
            "scoring.min_relevance": "0.6",
        })
        assert resp.status_code == 200
        config = client.application.config["BMNEWS_CONFIG"]
        assert config.sources.lookback_days == 14
        assert config.scoring.min_relevance == 0.6

    def test_template_list(self, client):
        resp = client.get("/settings/templates")
        assert resp.status_code == 200

    def test_template_load(self, client):
        resp = client.get("/settings/template/digest_email.html")
        assert resp.status_code == 200
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gui_app.py::TestSettingsRoute -v`
Expected: 404 errors

**Step 3: Add save_config to config.py**

Add to `bmnews/config.py` after `write_default_config`:

```python
def save_config(config: AppConfig, path: str | Path | None = None) -> Path:
    """Write current config values back to TOML file.

    Overwrites the existing config file with current in-memory values.
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH
    path = Path(path).expanduser()

    lines = []
    lines.append("[general]")
    lines.append(f'log_level = "{config.log_level}"')
    if config.template_dir:
        lines.append(f'template_dir = "{config.template_dir}"')
    lines.append("")

    # Helper for simple sections
    def _write_section(name: str, dc) -> None:
        lines.append(f"[{name}]")
        for field_name in dc.__dataclass_fields__:
            value = getattr(dc, field_name)
            if isinstance(value, bool):
                lines.append(f"{field_name} = {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                lines.append(f"{field_name} = {value}")
            elif isinstance(value, list):
                items = ", ".join(f'"{v}"' for v in value)
                lines.append(f"{field_name} = [{items}]")
            elif isinstance(value, str):
                lines.append(f'{field_name} = "{value}"')
        lines.append("")

    _write_section("database", config.database)
    _write_section("sources", config.sources)
    _write_section("llm", config.llm)
    _write_section("scoring", config.scoring)
    _write_section("quality", config.quality)
    _write_section("transparency", config.transparency)
    _write_section("user", config.user)
    _write_section("email", config.email)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
```

**Step 4: Implement the settings blueprint**

Replace `bmnews/gui/routes/settings.py`:

```python
"""Settings and template editor routes."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, render_template, request

from bmnews.config import AppConfig, save_config
from bmnews.pipeline import TEMPLATES_DIR

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings")
def settings_page():
    """Render the settings page."""
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    return render_template("fragments/settings.html", config=config)


@settings_bp.route("/settings/save", methods=["POST"])
def save_settings():
    """Save settings from form data back to config."""
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]

    # Map dotted form field names to config attributes
    for key, value in request.form.items():
        parts = key.split(".", 1)
        if len(parts) == 2:
            section_name, field_name = parts
            section = getattr(config, section_name, None)
            if section is not None and hasattr(section, field_name):
                field = section.__dataclass_fields__[field_name]
                if field.type in ("bool", bool) or "bool" in str(field.type):
                    setattr(section, field_name, value.lower() in ("true", "1", "on", "yes"))
                elif field.type in ("int", int) or "int" in str(field.type):
                    setattr(section, field_name, int(value))
                elif field.type in ("float", float) or "float" in str(field.type):
                    setattr(section, field_name, float(value))
                elif "list" in str(field.type):
                    setattr(section, field_name, [v.strip() for v in value.split(",") if v.strip()])
                else:
                    setattr(section, field_name, value)

    # Save to disk (skip in testing)
    if not current_app.config.get("TESTING"):
        save_config(config)

    return '<div class="flash success">Settings saved.</div>'


@settings_bp.route("/settings/templates")
def template_list():
    """Return list of available templates."""
    templates = sorted(TEMPLATES_DIR.glob("*.*"))
    names = [t.name for t in templates]
    return render_template("fragments/template_editor.html", template_names=names, content="", current="")


@settings_bp.route("/settings/template/<name>")
def template_load(name: str):
    """Load a template file for editing."""
    # Check user override dir first
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    user_dir = Path(config.template_dir).expanduser() if config.template_dir else None

    if user_dir and (user_dir / name).exists():
        content = (user_dir / name).read_text(encoding="utf-8")
    elif (TEMPLATES_DIR / name).exists():
        content = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    else:
        content = ""

    templates = sorted(TEMPLATES_DIR.glob("*.*"))
    names = [t.name for t in templates]
    return render_template("fragments/template_editor.html",
                           template_names=names, content=content, current=name)


@settings_bp.route("/settings/template/<name>", methods=["POST"])
def template_save(name: str):
    """Save edited template to user override directory."""
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    content = request.form.get("content", "")

    user_dir = Path(config.template_dir).expanduser() if config.template_dir else Path("~/.bmnews/templates").expanduser()
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / name).write_text(content, encoding="utf-8")

    return '<div class="flash success">Template saved.</div>'


@settings_bp.route("/settings/template/<name>/reset", methods=["POST"])
def template_reset(name: str):
    """Reset template to default by deleting user override."""
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    user_dir = Path(config.template_dir).expanduser() if config.template_dir else Path("~/.bmnews/templates").expanduser()

    override = user_dir / name
    if override.exists():
        override.unlink()

    # Reload default
    return template_load(name)
```

**Step 5: Create settings template**

Create `bmnews/gui/templates/fragments/settings.html`:

```html
<div class="settings-page">
    <h1>Settings</h1>

    <form hx-post="/settings/save" hx-target="#settings-flash" hx-swap="innerHTML">

        <section class="settings-section">
            <h2>Sources</h2>
            <label><input type="checkbox" name="sources.medrxiv" value="true"
                   {% if config.sources.medrxiv %}checked{% endif %}> medRxiv</label>
            <label><input type="checkbox" name="sources.biorxiv" value="true"
                   {% if config.sources.biorxiv %}checked{% endif %}> bioRxiv</label>
            <label><input type="checkbox" name="sources.europepmc" value="true"
                   {% if config.sources.europepmc %}checked{% endif %}> Europe PMC</label>
            <label>Lookback days:
                <input type="number" name="sources.lookback_days" value="{{ config.sources.lookback_days }}" min="1" max="90">
            </label>
        </section>

        <section class="settings-section">
            <h2>Scoring</h2>
            <label>Min relevance:
                <input type="range" name="scoring.min_relevance" min="0" max="1" step="0.05"
                       value="{{ config.scoring.min_relevance }}"
                       oninput="this.nextElementSibling.textContent = this.value">
                <span>{{ config.scoring.min_relevance }}</span>
            </label>
            <label>Min combined:
                <input type="range" name="scoring.min_combined" min="0" max="1" step="0.05"
                       value="{{ config.scoring.min_combined }}"
                       oninput="this.nextElementSibling.textContent = this.value">
                <span>{{ config.scoring.min_combined }}</span>
            </label>
        </section>

        <section class="settings-section">
            <h2>LLM</h2>
            <label>Provider:
                <select name="llm.provider">
                    <option value="ollama" {% if config.llm.provider == "ollama" %}selected{% endif %}>Ollama</option>
                    <option value="anthropic" {% if config.llm.provider == "anthropic" %}selected{% endif %}>Anthropic</option>
                </select>
            </label>
            <label>Model: <input type="text" name="llm.model" value="{{ config.llm.model }}"></label>
            <label>Temperature:
                <input type="number" name="llm.temperature" value="{{ config.llm.temperature }}" step="0.1" min="0" max="2">
            </label>
            <label>Concurrency:
                <input type="number" name="llm.concurrency" value="{{ config.llm.concurrency }}" min="1" max="10">
            </label>
        </section>

        <section class="settings-section">
            <h2>User</h2>
            <label>Name: <input type="text" name="user.name" value="{{ config.user.name }}"></label>
            <label>Email: <input type="email" name="user.email" value="{{ config.user.email }}"></label>
            <label>Research interests (comma-separated):
                <input type="text" name="user.research_interests"
                       value="{{ config.user.research_interests|join(', ') }}">
            </label>
        </section>

        <section class="settings-section">
            <h2>Email Delivery</h2>
            <label><input type="checkbox" name="email.enabled" value="true"
                   {% if config.email.enabled %}checked{% endif %}> Enable email delivery</label>
            <label>SMTP host: <input type="text" name="email.smtp_host" value="{{ config.email.smtp_host }}"></label>
            <label>SMTP port: <input type="number" name="email.smtp_port" value="{{ config.email.smtp_port }}"></label>
            <label>Subject prefix: <input type="text" name="email.subject_prefix" value="{{ config.email.subject_prefix }}"></label>
            <label>Max papers: <input type="number" name="email.max_papers" value="{{ config.email.max_papers }}"></label>
        </section>

        <div class="settings-actions">
            <button type="submit" class="btn btn-primary">Save Settings</button>
        </div>
        <div id="settings-flash"></div>
    </form>

    <section class="settings-section">
        <h2>Templates</h2>
        <div hx-get="/settings/templates" hx-trigger="load" hx-swap="innerHTML" id="template-area"></div>
    </section>
</div>
```

Create `bmnews/gui/templates/fragments/template_editor.html`:

```html
<div class="template-editor">
    <select hx-get="/settings/template/{value}" hx-target="#template-area" hx-swap="innerHTML"
            onchange="this.setAttribute('hx-get', '/settings/template/' + this.value); htmx.process(this); htmx.trigger(this, 'change')">
        <option value="">Select a template...</option>
        {% for name in template_names %}
        <option value="{{ name }}" {% if name == current %}selected{% endif %}>{{ name }}</option>
        {% endfor %}
    </select>

    {% if current %}
    <form hx-post="/settings/template/{{ current }}" hx-target="#template-flash" hx-swap="innerHTML">
        <textarea name="content" class="template-textarea" rows="20">{{ content }}</textarea>
        <div class="template-actions">
            <button type="submit" class="btn btn-primary">Save Template</button>
            <button type="button" class="btn btn-secondary"
                    hx-post="/settings/template/{{ current }}/reset"
                    hx-target="#template-area" hx-swap="innerHTML">Reset to Default</button>
        </div>
        <div id="template-flash"></div>
    </form>
    {% endif %}
</div>
```

**Step 6: Run tests**

Run: `pytest tests/test_gui_app.py -v`
Expected: all pass

**Step 7: Commit**

```bash
git add bmnews/gui/ bmnews/config.py tests/test_gui_app.py
git commit -m "feat(gui): settings page with config editing and template editor"
```

---

### Task 8: Pipeline Route — Trigger Fetch & Score from GUI

**Files:**
- Modify: `bmnews/gui/routes/pipeline.py`
- Create: `bmnews/gui/templates/fragments/status_bar.html`
- Modify: `tests/test_gui_app.py`

**Step 1: Write the failing test**

Add to `tests/test_gui_app.py`:

```python
from unittest.mock import patch


class TestPipelineRoute:
    def test_run_pipeline_returns_status(self, client):
        with patch("bmnews.gui.routes.pipeline.run_pipeline") as mock_run:
            resp = client.post("/pipeline/run")
            assert resp.status_code == 200
            assert b"Pipeline" in resp.data or b"pipeline" in resp.data
            mock_run.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_gui_app.py::TestPipelineRoute -v`
Expected: 404

**Step 3: Implement the pipeline route**

Replace `bmnews/gui/routes/pipeline.py`:

```python
"""Pipeline execution routes."""

from __future__ import annotations

import logging
import threading

from flask import Blueprint, current_app, render_template

from bmnews.config import AppConfig

pipeline_bp = Blueprint("pipeline", __name__)
logger = logging.getLogger(__name__)

# Simple lock to prevent concurrent pipeline runs
_pipeline_lock = threading.Lock()


@pipeline_bp.route("/pipeline/run", methods=["POST"])
def run():
    """Trigger a pipeline run in a background thread."""
    from bmnews.pipeline import run_pipeline

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]

    if not _pipeline_lock.acquire(blocking=False):
        return render_template("fragments/status_bar.html",
                               message="Pipeline already running...", status="busy")

    try:
        run_pipeline(config)
        message = "Pipeline complete — papers fetched, scored, and digested."
        status = "success"
    except Exception as e:
        logger.exception("Pipeline error")
        message = f"Pipeline error: {e}"
        status = "error"
    finally:
        _pipeline_lock.release()

    return render_template("fragments/status_bar.html", message=message, status=status)
```

Create `bmnews/gui/templates/fragments/status_bar.html`:

```html
<span class="status-{{ status }}">{{ message }}</span>
```

**Step 4: Run test**

Run: `pytest tests/test_gui_app.py::TestPipelineRoute -v`
Expected: pass

**Step 5: Commit**

```bash
git add bmnews/gui/ tests/test_gui_app.py
git commit -m "feat(gui): pipeline run route with status feedback"
```

---

### Task 9: Full CSS Styling

**Files:**
- Modify: `bmnews/gui/static/css/app.css`

This task replaces the minimal CSS from Task 5 with the full email-client styling. No tests needed — this is pure visual.

**Step 1: Write the complete stylesheet**

Replace `bmnews/gui/static/css/app.css` with a complete stylesheet covering:

- **Color scheme:** clean light theme with blue accents, dark text
- **Tab bar:** horizontal nav with active state highlight
- **Split pane grid:** `grid-template-columns: minmax(250px, 30%) 6px 1fr` with gutter styling
- **Paper cards:** compact cards with hover/selected states, score badge color-coding (green >0.7, amber 0.5-0.7, red <0.5)
- **Reading pane:** clean article typography, section spacing, score badges
- **Settings page:** form sections with labels, inputs, sliders, checkboxes, save/flash messages
- **Template editor:** monospace textarea, full-width
- **Status bar:** subtle footer with status color indicators
- **Responsive breakpoints:**
  - `@media (max-width: 1200px)`: `grid-template-columns: minmax(250px, 40%) 6px 1fr`
  - `@media (max-width: 800px)`: single column, hide gutter, stack panels
- **Score badge colors:**
  - `.score-badge[style*="--score: 0.7"]` and above: `#28a745` (green)
  - `.score-badge` between 0.5-0.7: `#ffc107` (amber)
  - `.score-badge` below 0.5: `#dc3545` (red)
- **Source badges:** different colors for medrxiv (blue), biorxiv (green), europepmc (purple)
- **Buttons:** `.btn-primary` (blue), `.btn-secondary` (grey outline)
- **Flash messages:** `.flash.success` (green), `.flash.error` (red)
- **Scrollbar styling:** subtle custom scrollbars for the two panels

Use CSS custom properties (variables) for the color scheme so a dark mode could be added later.

**Step 2: Verify the app loads correctly**

Run: `python -c "from bmnews.gui.app import create_app; print('CSS path OK')"`
Expected: no errors

**Step 3: Commit**

```bash
git add bmnews/gui/static/css/app.css
git commit -m "feat(gui): complete CSS styling for email-client layout"
```

---

### Task 10: Launcher — pywebview Window with Flask Thread

**Files:**
- Create: `bmnews/gui/launcher.py`
- Modify: `tests/test_gui_app.py`

**Step 1: Write a test for the launcher module**

Add to `tests/test_gui_app.py`:

```python
class TestLauncher:
    def test_find_free_port(self):
        from bmnews.gui.launcher import _find_free_port
        port = _find_free_port()
        assert 1024 < port < 65536

    def test_create_flask_app_from_config(self):
        from bmnews.gui.launcher import _build_app
        config = AppConfig()
        app, conn = _build_app(config)
        assert app is not None
        assert conn is not None
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_gui_app.py::TestLauncher -v`
Expected: ImportError

**Step 3: Implement the launcher**

Create `bmnews/gui/launcher.py`:

```python
"""Desktop GUI launcher — opens pywebview window with Flask backend."""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from bmnews.config import AppConfig
from bmnews.db.schema import init_db, open_db

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_app(config: AppConfig) -> tuple[Any, Any]:
    """Create the Flask app and database connection."""
    from bmnews.gui.app import create_app

    conn = open_db(config)
    init_db(conn)
    app = create_app(config, conn)
    return app, conn


def launch(config: AppConfig, port: int | None = None) -> None:
    """Launch the desktop GUI.

    Args:
        config: Application configuration.
        port: Fixed port number. If None, a free port is chosen.
    """
    import webview

    if port is None:
        port = _find_free_port()

    app, conn = _build_app(config)

    # Start Flask in a daemon thread
    ready = threading.Event()

    def run_server():
        # Signal ready just before starting (werkzeug takes over the thread)
        ready.set()
        app.run(host="127.0.0.1", port=port, use_reloader=False, threaded=True)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    ready.wait(timeout=5)

    # Open the native window
    window = webview.create_window(
        "BioMedical News",
        f"http://127.0.0.1:{port}",
        width=1200,
        height=800,
        min_size=(600, 400),
    )
    webview.start()

    # Cleanup
    conn.close()
    logger.info("GUI closed")
```

**Step 4: Run tests**

Run: `pytest tests/test_gui_app.py::TestLauncher -v`
Expected: 2 passed

**Step 5: Commit**

```bash
git add bmnews/gui/launcher.py tests/test_gui_app.py
git commit -m "feat(gui): pywebview launcher with Flask background thread"
```

---

### Task 11: CLI Integration — bmnews gui Command

**Files:**
- Modify: `bmnews/cli.py`

**Step 1: Write a test**

Add to `tests/test_gui_app.py`:

```python
from click.testing import CliRunner
from bmnews.cli import main


class TestGuiCLI:
    def test_gui_command_exists(self):
        runner = CliRunner()
        result = runner.invoke(main, ["gui", "--help"])
        assert result.exit_code == 0
        assert "Launch" in result.output or "launch" in result.output or "GUI" in result.output
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_gui_app.py::TestGuiCLI -v`
Expected: "No such command 'gui'"

**Step 3: Add the gui command to cli.py**

Add to `bmnews/cli.py` after the `search` command:

```python
@main.command()
@click.option("--port", default=None, type=int, help="Fixed port for Flask server (default: auto).")
@click.pass_context
def gui(ctx: click.Context, port: int | None) -> None:
    """Launch the desktop GUI."""
    try:
        from bmnews.gui.launcher import launch
    except ImportError:
        click.echo("GUI dependencies not installed. Run: pip install bmnews[gui]")
        sys.exit(1)

    launch(ctx.obj["config"], port=port)
```

**Step 4: Run test**

Run: `pytest tests/test_gui_app.py::TestGuiCLI -v`
Expected: pass

**Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: all pass

**Step 6: Commit**

```bash
git add bmnews/cli.py tests/test_gui_app.py
git commit -m "feat(cli): add 'bmnews gui' command with optional --port flag"
```

---

### Task 12: End-to-End Smoke Test

**Files:**
- Modify: `tests/test_gui_app.py`

**Step 1: Write an end-to-end test**

Add to `tests/test_gui_app.py`:

```python
class TestEndToEnd:
    """Full workflow: load page, list papers, click paper, view detail."""

    def test_full_workflow(self, seeded_client):
        # 1. Load main page
        resp = seeded_client.get("/")
        assert resp.status_code == 200
        assert b"BioMedical News" in resp.data

        # 2. Paper list loads
        resp = seeded_client.get("/papers")
        assert resp.status_code == 200
        assert b"Alpha Paper" in resp.data

        # 3. Click a paper — get detail
        conn = seeded_client.application.config["BMNEWS_DB"]
        from bmnews.db.operations import get_paper_by_doi
        paper = get_paper_by_doi(conn, "10.1101/g1")
        resp = seeded_client.get(f"/papers/{paper['id']}")
        assert resp.status_code == 200
        assert b"Cancer immunotherapy" in resp.data
        assert b"A strong trial" in resp.data

        # 4. Search
        resp = seeded_client.get("/search?q=Genomics")
        assert resp.status_code == 200
        assert b"Beta Paper" in resp.data
        assert b"Alpha Paper" not in resp.data

        # 5. Settings page
        resp = seeded_client.get("/settings")
        assert resp.status_code == 200

        # 6. Save settings
        resp = seeded_client.post("/settings/save", data={
            "sources.lookback_days": "30",
        })
        assert resp.status_code == 200
        config = seeded_client.application.config["BMNEWS_CONFIG"]
        assert config.sources.lookback_days == 30
```

**Step 2: Run the test**

Run: `pytest tests/test_gui_app.py::TestEndToEnd -v`
Expected: pass

**Step 3: Run full test suite one final time**

Run: `pytest tests/ -v`
Expected: all pass

**Step 4: Commit**

```bash
git add tests/test_gui_app.py
git commit -m "test(gui): add end-to-end smoke test for full GUI workflow"
```

---

### Task 13: Manual Visual Verification

This is a non-automated step. Launch the GUI with real data and verify:

**Step 1: Ensure you have data in the database**

Run: `bmnews run --days 3`
(This fetches recent papers, scores them, and creates a digest)

**Step 2: Launch the GUI**

Run: `bmnews gui`

**Step 3: Verify checklist**

- [ ] Window opens with title "BioMedical News"
- [ ] Paper list loads in left pane
- [ ] Cards show title, score badge, source, date
- [ ] Clicking a card loads the reading pane on the right
- [ ] Reading pane shows title, authors, scores, summary, abstract
- [ ] "Open DOI" button opens in external browser
- [ ] Sort dropdown changes paper order
- [ ] Search box filters papers
- [ ] "Load more" button works if >20 papers
- [ ] Split pane is resizable by dragging the gutter
- [ ] Settings tab shows current config values
- [ ] Saving settings updates values
- [ ] Template editor loads and displays template content
- [ ] Status bar updates appropriately

**Step 4: Fix any visual issues**

Adjust CSS, templates, or routes as needed based on manual testing.

**Step 5: Commit any fixes**

```bash
git add -u
git commit -m "fix(gui): visual polish from manual testing"
```
