# Desktop GUI Design: Email-Client-Style Paper Reader

**Date:** 2026-02-14
**Status:** Approved

## Overview

A desktop GUI for BioMedicalNews with an email-client-style layout: paper list on the left, reading pane on the right, and a settings tab for configuration and template editing.

## Technology Stack

| Component | Library | Size | Role |
|-----------|---------|------|------|
| Native window | pywebview >=5.0 | ~50KB | OS-native webview wrapper |
| HTTP server | Flask >=3.0 | ~1MB | Local-only server in background thread |
| Dynamic UI | HTMX | ~14KB | HTML-over-the-wire, fragment swapping |
| Split panes | Split.js | ~2KB | Resizable left/right panels |
| Styling | Custom CSS | - | CSS Grid/Flexbox, responsive |
| Templates | Jinja2 | (existing) | Server-side HTML rendering |

**Why this stack:**

- pywebview provides a native desktop window using the OS's built-in webview (WebView2 on Windows, WebKit on macOS, WebKitGTK on Linux). No bundled browser engine, minimal footprint.
- Flask runs on localhost as a background thread, serving HTML to the webview. HTMX needs HTTP endpoints, and Flask maps naturally to the existing pure-function data access layer.
- HTMX handles all interactivity by requesting HTML fragments from Flask routes and swapping them into the DOM. No client-side rendering, no JSON serialization, no JS framework.
- Split.js provides the resizable divider between paper list and reading pane in ~2KB of vanilla JS.
- The existing Jinja2 infrastructure (bmlib.templates.TemplateEngine) is reused for rendering fragments.

## Architecture

```
┌──────────────────────────────────────────────┐
│  pywebview native window                      │
│  ┌──────────────────────────────────────────┐ │
│  │  HTML/CSS/HTMX frontend                  │ │
│  │                                          │ │
│  │  HTMX request ──► Flask route            │ │
│  │  ◄── HTML fragment response              │ │
│  └──────────────────────────────────────────┘ │
│           │                                   │
│     Flask (localhost:random_port)              │
│           │                                   │
│     bmnews.db.operations (existing code)      │
│           │                                   │
│     SQLite database                           │
└──────────────────────────────────────────────┘
```

The Flask server binds to a random free port on localhost. pywebview opens a native window pointing at `http://localhost:{port}`. All data access goes through the existing `bmnews.db.operations` functions — no new data layer.

## Layout

```
┌─────────────────────────────────────────────────────┐
│  [Papers]  [Settings]              🔍 Search...     │  ← Tab bar
├──────────────────┬──────────────────────────────────┤
│                  │                                   │
│  Paper Card 1    │  Paper Title                      │
│  ├ score badge   │  Authors · Date · Source          │
│  └ snippet...    │  ──────────────────────           │
│                  │                                   │
│  Paper Card 2    │  Relevance: 92%  Quality: Tier 4  │
│  ├ score badge   │  Design: RCT                      │
│  └ snippet...    │  ──────────────────────           │
│                  │                                   │
│  Paper Card 3  ◄─┼─ resizable ──►                   │
│  ├ score badge   │  Summary                          │
│  └ snippet...    │  LLM-generated summary text...    │
│                  │                                   │
│                  │  Abstract                         │
│  ...             │  Full abstract text here with     │
│                  │  scrollable content...             │
│                  │                                   │
│                  │  [Open DOI ↗]  [Mark Read]        │
├──────────────────┴──────────────────────────────────┤
│  Showing 42 papers · Sorted by combined score       │  ← Status bar
└─────────────────────────────────────────────────────┘
```

### Left Panel (Paper List)

- Cards show: title (truncated), combined score badge (color-coded), source icon, date
- Sort controls at top: combined score, relevance, quality, date
- Filter chips: source (medRxiv/bioRxiv/EuropePMC), quality tier, study design
- Click-to-load pagination: 20 papers at a time (HTMX append pattern)
- Selected card gets highlighted border

### Right Panel (Reading Pane)

- Header: full title, authors, date, source, DOI link
- Score section: relevance %, quality tier badge, study design badge
- Summary: LLM-generated summary (collapsible)
- Abstract: full abstract text, scrollable
- Action buttons: Open DOI in external browser, mark read

### Settings Tab

Replaces the split view when active. Sections:

- **General:** log level
- **Sources:** enable/disable medRxiv, bioRxiv, EuropePMC; lookback days
- **LLM:** provider, model, temperature, concurrency
- **Scoring:** min relevance threshold (slider), min combined threshold (slider)
- **Quality:** assessment tier, min quality tier filter
- **Email:** SMTP settings, subject prefix, max papers
- **User:** name, email, research interests (tag-input list)
- **Templates:** dropdown to select template, monospace textarea editor, save/reset buttons

Changes write back to `config.toml` via the existing config system.

### Responsive Behavior

| Width | Layout |
|-------|--------|
| >1200px | Side-by-side, 30/70 default split ratio |
| 800-1200px | Side-by-side, 40/60 default split ratio |
| <800px | Stacked: paper list full-width, click pushes to full-width reading view with back button |

## Flask Routes

### Page Routes (full HTML)

| Route | Purpose |
|-------|---------|
| `GET /` | Main shell: tab bar, split layout, loads paper list + empty reading pane |
| `GET /settings` | Settings page (full page swap via `hx-boost`) |

### Fragment Routes (partial HTML for HTMX)

| Route | Trigger | HTMX Target | Purpose |
|-------|---------|-------------|---------|
| `GET /papers` | Page load, sort/filter change | `#paper-list` | Paper card list |
| `GET /papers?sort=date&source=medrxiv&tier=4` | Filter/sort controls | `#paper-list` | Filtered/sorted list |
| `GET /papers/more?offset=20` | "Load more" button | `#paper-list` (append) | Next batch of cards |
| `GET /papers/<id>` | Click paper card | `#reading-pane` | Reading pane content |
| `GET /search?q=immunotherapy` | Search input (debounced) | `#paper-list` | Search results |
| `POST /settings/save` | Save button | `#settings-flash` | Save config, return flash |
| `GET /settings/template/<name>` | Template dropdown | `#template-editor` | Load template into editor |
| `POST /settings/template/<name>` | Save template button | `#template-flash` | Write template file |
| `POST /settings/template/<name>/reset` | Reset button | `#template-editor` | Reload default template |
| `POST /pipeline/run` | "Fetch & Score" button | `#status-bar` | Trigger pipeline run |

### HTMX Interaction Patterns

Paper card click:
```html
<div class="paper-card"
     hx-get="/papers/42"
     hx-target="#reading-pane"
     hx-swap="innerHTML">
  ...
</div>
```

Sort control:
```html
<select hx-get="/papers"
        hx-target="#paper-list"
        hx-swap="innerHTML"
        hx-include="[name='source'],[name='tier']"
        name="sort">
  <option value="combined">Combined Score</option>
  <option value="relevance">Relevance</option>
  <option value="date">Date</option>
</select>
```

## File Organization

```
bmnews/
  gui/
    __init__.py          ← empty
    app.py               ← Flask app factory, route registration
    routes/
      __init__.py
      papers.py          ← /papers, /papers/<id>, /search
      settings.py        ← /settings, config save, template routes
      pipeline.py        ← /pipeline/run
    templates/
      base.html          ← page shell: tab bar, split layout, CSS/JS
      fragments/
        paper_list.html
        paper_card.html
        reading_pane.html
        settings.html
        template_editor.html
        status_bar.html
    static/
      css/
        app.css          ← split pane, cards, badges, responsive styles
      js/
        app.js           ← Split.js init, keyboard shortcuts, glue
      vendor/
        htmx.min.js      ← vendored (~14KB)
        split.min.js     ← vendored (~2KB)
    launcher.py          ← pywebview window, Flask thread, startup
```

### Key File Roles

**`launcher.py`** — Entry point. Creates the Flask app, finds a free port, starts Flask in a daemon thread, waits for ready, then opens a pywebview window pointing at `http://localhost:{port}`.

**`app.py`** — Flask app factory. Creates the Flask instance, registers blueprints (papers, settings, pipeline), injects config and db connection into app context, configures static file serving.

**Route files** — Flask Blueprints. Each route function reads query/form params, calls existing `bmnews.db.operations` functions, renders a Jinja2 fragment, returns HTML.

**`base.html`** — The single full page. Loads HTMX, Split.js, and CSS. Contains the tab bar, split-pane containers (`#paper-list` and `#reading-pane`), and status bar. HTMX swaps fragments into the containers.

### CLI Integration

New Click command in `cli.py`:

```
bmnews gui              ← launch desktop GUI
bmnews gui --port 8765  ← fixed port (for debugging)
```

### New Database Queries

One new query likely needed: `get_paper_with_score(conn, paper_id)` — fetch a single paper with its score data joined, for the reading pane. Everything else is served by existing operations.

## Dependencies

Added as optional extras in `pyproject.toml`:

```toml
[project.optional-dependencies]
gui = ["pywebview>=5.0", "flask>=3.0"]
```

CLI-only users (`pip install bmnews`) get no GUI dependencies. GUI users install with `pip install bmnews[gui]`.

No npm, no Node.js, no build step. HTMX and Split.js are vendored as static files.

## Packaging & Distribution

### Development

```bash
pip install -e ".[gui]"
bmnews gui
```

### Desktop Packaging (PyInstaller)

| Platform | Output | Size | Webview engine |
|----------|--------|------|----------------|
| Windows | `BioMedicalNews/` (one-dir) | ~25-40 MB | EdgeChromium (WebView2) |
| macOS | `BioMedicalNews.app` | ~25-40 MB | WebKit |
| Linux | `BioMedicalNews/` (one-dir or AppImage) | ~25-40 MB | WebKitGTK |

PyInstaller spec file (`bmnews.spec`):
- Collects `gui/templates/` and `gui/static/` as data files
- Collects existing `templates/` (digest templates) as data files
- Hidden imports for pywebview platform backends
- Single-directory mode (more reliable than one-file for webview apps)

### Platform Notes

- **Windows:** WebView2 ships with Windows 10/11. No extra runtime.
- **macOS:** WebKit always available. Code signing can be added later for Gatekeeper.
- **Linux:** Requires `libwebkit2gtk-4.0` (standard on most desktop distros). Document as dependency.

### Not in v1

- No auto-updater (user reinstalls for updates)
- No code signing / notarization (can add later)
- No Electron (wrong ecosystem, huge bundle)

## Design Decisions

1. **Flask over js_api bridge** — HTMX needs HTTP endpoints. Flask provides them naturally. The js_api bridge would require writing custom JS fetch logic, defeating the purpose of HTMX.

2. **Vendored JS over CDN** — Desktop app must work offline. HTMX and Split.js are vendored in `static/vendor/`.

3. **Optional dependency group** — GUI deps are separate so CLI-only installs stay lightweight.

4. **No new data layer** — Flask routes call existing `bmnews.db.operations` directly. Keeps the codebase simple and avoids duplication.

5. **Fragment-based templates** — Each HTMX target has its own Jinja2 template. Keeps templates small and focused. The page shell (`base.html`) is loaded once; everything else is swapped in.

6. **Random port** — Avoids conflicts with other local services. Port is only used internally between Flask and pywebview in the same process.
