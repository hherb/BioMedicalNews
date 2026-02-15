# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**bmnews** (v0.2.1) is a biomedical news reader that fetches preprints from medRxiv, bioRxiv, Europe PMC, PubMed, and OpenAlex, scores them for relevance and quality using LLMs, and delivers curated digests via email, file, stdout, or a desktop GUI. Built on [bmlib](https://github.com/hherb/bmlib) for LLM abstraction, database utilities, quality assessment, fetcher registry, fulltext retrieval, and template rendering.

## Development Commands

```bash
# Install (editable, with all extras)
uv pip install -e ".[all]"
# ALWAYS use uv to install/upgrade or otherwise manipulate packages
# DO NOT use pip directly

# For local bmlib development (changes reflected immediately)
cd /path/to/bmlib && uv pip install -e ".[dev]"
cd /path/to/BioMedicalNews && uv pip install -e ".[dev]"

# Run tests
pytest                                              # all tests
pytest tests/test_db.py                             # single file
pytest tests/test_db.py::TestPapers::test_upsert    # single test

# Lint and format
ruff check bmnews/ tests/                           # lint
ruff format --check bmnews/ tests/                  # format check
ruff check --fix bmnews/ tests/                     # auto-fix
ruff format bmnews/ tests/                          # auto-format

# CLI — all commands support -c/--config and -v/--verbose
bmnews init                     # initialize database + config (~/.bmnews/config.toml)
bmnews run                      # full pipeline: fetch → store → score → digest
bmnews run --days 3             # override lookback days
bmnews run --show_cached        # re-render previously digested papers
bmnews fetch --days 7           # fetch papers only
bmnews score                    # score unscored papers only
bmnews digest -o digest.html    # generate digest (to file, email, or stdout)
bmnews search "oncology"        # search stored papers by keyword
bmnews gui                      # launch desktop GUI (pywebview + Flask + HTMX)
bmnews gui --port 8080          # launch GUI on specific port
```

## Architecture

### Pipeline

Linear pipeline with four independent stages, each runnable individually via CLI or composed with `bmnews run`:

```
FETCH (httpx) → STORE (bmlib.db) → SCORE (bmlib.llm) → DIGEST (Jinja2 + SMTP)
```

All stages are **incremental**: fetch upserts (idempotent), score skips already-scored papers, digest only includes papers not yet in a prior digest.

### Directory Structure

```
bmnews/
├── __init__.py          # Package version (0.2.1)
├── cli.py               # Click CLI commands (run, fetch, score, digest, init, gui, search)
├── config.py            # TOML config loading (AppConfig + nested section dataclasses)
├── pipeline.py          # Orchestration: fetch → store → score → digest (progress callbacks)
├── db/
│   ├── schema.py        # Database connection factory (open_db, init_db)
│   ├── operations.py    # Pure-function CRUD (all SQL lives here, ~490 lines)
│   └── migrations.py    # 3 versioned migrations (papers → paper_tags → fulltext columns)
├── fetchers/
│   ├── base.py          # FetchedPaper dataclass (normalized paper representation)
│   ├── medrxiv.py       # medRxiv/bioRxiv API fetcher (legacy, now via bmlib registry)
│   └── europepmc.py     # Europe PMC REST API fetcher (local implementation)
├── scoring/
│   ├── scorer.py        # Orchestrates relevance (LLM) + quality (bmlib.quality) scoring
│   └── relevance_agent.py  # LLM-based relevance scoring agent (extends BaseAgent)
├── digest/
│   ├── renderer.py      # Jinja2 digest rendering (HTML + plain text)
│   └── sender.py        # SMTP email delivery (TLS, multipart MIME)
└── gui/
    ├── app.py           # Flask application factory (registers blueprints)
    ├── launcher.py      # pywebview window launcher (geometry persistence, port auto-detect)
    ├── helpers.py        # HTML formatting helpers (abstract section parsing)
    ├── routes/
    │   ├── papers.py    # Paper list/detail, search, fulltext retrieval
    │   ├── pipeline.py  # Async pipeline execution + status polling
    │   └── settings.py  # Settings UI, dynamic model selector
    ├── static/
    │   ├── css/app.css  # Email-client UI styling
    │   ├── js/app.js    # Split-pane, tabs, fulltext toggle, HTMX events
    │   └── vendor/      # htmx.min.js (v2.x), split-grid.min.js
    └── templates/
        ├── base.html    # Main layout (nav tabs, split pane, status footer)
        └── fragments/   # HTMX partial templates (paper_list, reading_pane, settings, etc.)

templates/                     # Email digest + LLM prompt templates (Jinja2)
├── digest_email.html          # HTML email digest
├── digest_text.txt            # Plain-text email digest
├── relevance_system.txt       # LLM system prompt for relevance scoring
└── relevance_scoring.txt      # LLM user prompt (paper title, abstract, interests)

tests/                         # Test suite (10 files)
docs/plans/                    # Implementation design documents and plans
bmlib_patch/                   # bmlib source archive and patch files
```

### Module Dependency Graph

```
cli.py → pipeline.py → config.py (AppConfig dataclass)
                      → db/schema.py (open_db, init_db, migrations)
                      → db/operations.py (pure-function CRUD)
                      → bmlib.publications.fetchers (registry: medrxiv, biorxiv, pubmed, openalex)
                      → fetchers/europepmc.py (local implementation → FetchedPaper)
                      → scoring/scorer.py → scoring/relevance_agent.py → bmlib.agents.BaseAgent
                                          → bmlib.quality.metadata_filter
                      → digest/renderer.py → bmlib.templates.TemplateEngine
                      → digest/sender.py (SMTP)

gui/ → app.py (Flask factory) → routes/ (papers, settings, pipeline blueprints)
     → launcher.py (pywebview wrapper)
     → helpers.py (abstract HTML formatting)
     → routes/papers.py → bmlib.fulltext.FullTextService (on-demand fulltext retrieval)
```

### bmlib Integration

bmlib is a companion library providing shared infrastructure. Key modules used:

| bmlib module | bmnews usage |
|---|---|
| `bmlib.db` | `connect_sqlite`, `connect_postgresql`, `execute`, `fetch_one`, `fetch_all`, `fetch_scalar`, `transaction`, `Migration`, `create_tables` |
| `bmlib.llm` | `LLMClient` with `"provider:model"` format (e.g., `"ollama:llama3.1"`, `"anthropic:claude-sonnet-4-5-20250929"`) |
| `bmlib.agents` | `BaseAgent` — provides `render_template()`, `chat()`, `parse_json()` |
| `bmlib.templates` | `TemplateEngine` with user-dir override (`~/.bmnews/templates/`) → package `templates/` fallback |
| `bmlib.quality` | `classify_from_metadata()`, `QualityAssessment`, `StudyDesign`, `QualityTier` |
| `bmlib.fulltext` | `FullTextService` (3-tier: Europe PMC → Unpaywall → DOI), JATS XML parser, `FullTextError` |
| `bmlib.publications` | `list_sources()`, `get_fetcher()`, `source_names()`, `FetchedRecord` — registry-driven fetcher system for medRxiv, bioRxiv, PubMed, OpenAlex |

### Source Fetching

Sources are managed via a **dual system**:

1. **bmlib registry** — medRxiv, bioRxiv, PubMed, OpenAlex are fetched via `bmlib.publications.fetchers.get_fetcher()`. Returns `FetchedRecord` objects that are converted to `FetchedPaper` in `pipeline._record_to_fetched_paper()`.
2. **Local implementation** — Europe PMC is implemented locally in `bmnews/fetchers/europepmc.py` with cursor-mark pagination and custom query support.

Sources are configured via `config.sources.enabled` list (e.g., `["medrxiv", "europepmc", "biorxiv", "pubmed", "openalex"]`). Per-source options can be set in `config.sources.source_options`.

### Database

SQLite (default) or PostgreSQL. 5 tables with 3 versioned migrations in `db/migrations.py`:
- **papers** — fetched paper metadata (DOI, title, authors, abstract, pmid, pmcid, fulltext_html, fulltext_source)
- **scores** — scoring results (relevance, quality, combined scores, summary, study_design, quality_tier, assessment JSON)
- **digests** / **digest_papers** — digest delivery tracking (many-to-many)
- **paper_tags** — per-paper interest tags matched during scoring

Migrations:
1. `initial_schema` — papers, scores, digests, digest_papers tables
2. `add_paper_tags` — paper_tags table for interest matching
3. `add_fulltext_columns` — adds pmid, pmcid, fulltext_html, fulltext_source to papers; backfills pmid/pmcid from metadata_json for europepmc papers

Backend-aware SQL: `_placeholder(conn)` returns `?` (SQLite) or `%s` (PostgreSQL). Schema DDL maintained as separate SQLite and PostgreSQL strings per migration.

### Configuration

Layered: dataclass defaults → TOML file (`~/.bmnews/config.toml`) → CLI flags. `AppConfig` contains nested section dataclasses:

| Section | Dataclass | Key fields |
|---|---|---|
| `[general]` | top-level `AppConfig` | `log_level`, `template_dir` |
| `[database]` | `DatabaseConfig` | `backend` (sqlite/postgresql), `sqlite_path`, `pg_*` |
| `[sources]` | `SourcesConfig` | `enabled` (list), `lookback_days`, `source_options` (per-source dict) |
| `[llm]` | `LLMConfig` | `provider`, `model`, `temperature`, `max_tokens`, `concurrency`, `api_key`, `base_url` |
| `[scoring]` | `ScoringConfig` | `min_relevance`, `min_combined` |
| `[quality]` | `QualityConfig` | `enabled`, `default_tier`, `max_tier`, `min_quality_tier` |
| `[transparency]` | `TransparencyConfig` | `enabled`, `min_score_threshold` |
| `[user]` | `UserConfig` | `name`, `email`, `research_interests` |
| `[email]` | `EmailConfig` | `enabled`, `smtp_*`, `from_address`, `to_address`, `subject_prefix`, `max_papers` |

### Scoring

Two-tier scoring system:

1. **Relevance (LLM-based)** — `RelevanceAgent` sends paper title + abstract + user interests to LLM via Jinja2 template prompts. Returns JSON with `relevance_score` (0.0–1.0), `summary`, `key_findings`, `matched_tags`.
2. **Quality (bmlib.quality)** — Tiered assessment (metadata classification → LLM classifier → deep analysis). Maps `QualityTier` to a 0.0–1.0 score.

Combined score = `0.6 * relevance + 0.4 * quality`. Concurrency configurable via `ThreadPoolExecutor` (`concurrency=1` for local Ollama, `>1` for API providers).

**LLM providers** (6 supported via bmlib): Ollama, Anthropic, OpenAI, Deepseek, Mistral, Gemini. Model format: `"provider:model"` (e.g., `"anthropic:claude-3-haiku"`). The pipeline disambiguates bare model names with tags (e.g., `"llama3.1:latest"`) from provider-prefixed strings.

### GUI

Desktop app: pywebview (native window) + Flask (HTTP backend) + HTMX (frontend interactivity).

**Layout:** Email-client style with tab bar, resizable split panes (Split.js), paper list with infinite scroll, reading pane with fulltext toggle, settings form, and pipeline status footer.

**Key features:**
- **HTMX fragment-based updates** — paper list pagination, paper detail, settings, and pipeline status polling (500ms interval) use partial HTML responses
- **Async pipeline execution** — runs in daemon thread with `on_progress` and `on_scored` callbacks; OOB (out-of-band) HTMX swaps update individual paper cards and refresh the list
- **Auto-resume** — on app startup, automatically scores any unscored papers
- **Fulltext retrieval** — on-demand via `bmlib.fulltext.FullTextService` (Europe PMC → Unpaywall → DOI); JATS XML parsed to HTML; cached in `papers.fulltext_html`
- **Dynamic model selector** — auto-populated from provider APIs with local caching
- **Window geometry persistence** — saves/restores position and size in `~/.bmnews/window_state.json`
- **Sorting/filtering** — by date, score, source, quality tier, study design

**Routes:**

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Main index (base.html) |
| GET | `/papers` | Paper list with pagination/filters |
| GET | `/papers/<id>` | Paper detail (reading pane) |
| GET | `/search?q=...` | Keyword search |
| POST | `/papers/<id>/fulltext` | Fetch and cache full text |
| GET | `/settings` | Settings form |
| POST | `/settings/save` | Save settings to config |
| POST | `/pipeline/run` | Start async pipeline |
| POST | `/pipeline/resume` | Resume scoring unscored papers |
| GET | `/pipeline/status` | Poll status (returns OOB updates) |

## Coding Conventions

- **Python 3.11+** — use modern syntax (`X | Y` unions, `tomllib`). Add `from __future__ import annotations` to every module.
- **ruff** — line-length=100, rules: E, F, I, N, W, UP.
- **Pure functions for DB operations** — take `conn` as first argument, no global state. Use `bmlib.db` helpers, not raw cursors.
- **Keyword-only args for writes** — `def save_score(conn, *, paper_id, ...)`.
- **Type hints** on all function signatures. **Google-style docstrings** on public functions/classes.
- **No ORM** — write explicit SQL. Use `_placeholder(conn)` for backend-aware placeholders.
- **Template-driven prompts** — LLM prompts are Jinja2 templates in `templates/`, not Python strings.
- **Dataclass models** with `to_dict()` / `from_dict()` for serialization.
- **Module-level loggers** — `logger = logging.getLogger(__name__)`.
- **AGPL-3.0 license**.
- **Commit messages** — conventional style: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.

## Testing Patterns

- **In-memory SQLite** for all DB tests — `connect_sqlite(":memory:")` + `init_db(conn)`.
- **Mocked HTTP** (httpx) for fetcher tests, **mocked `open_db`** for pipeline/CLI tests.
- **Click's `CliRunner`** for CLI command tests.
- **Flask test client** for GUI route tests (`create_app()` with test config, `client.get()`/`client.post()`).
- **No LLM calls in unit tests** — test scoring logic (tier mapping, metadata extraction) directly; mock `RelevanceAgent.score()` for integration tests.
- **Always mock SMTP** — never send real emails in tests.

Test files:
| File | Coverage |
|---|---|
| `test_config.py` | Config loading, TOML parsing, backward-compat defaults |
| `test_db.py` | All database operations, migrations, upsert, filtering, tagging, digests, fulltext |
| `test_digest.py` | HTML/text digest rendering |
| `test_fetchers.py` | Fetcher mocking |
| `test_fulltext_integration.py` | Fulltext service integration (Europe PMC/Unpaywall/DOI) |
| `test_gui_app.py` | Flask blueprints, HTMX responses, paper queries, pipeline status |
| `test_gui_helpers.py` | Abstract HTML formatting |
| `test_pipeline.py` | Show-cached flag, CLI integration, paper storage with PMID/PMCID |
| `test_scoring.py` | Quality tier mapping, publication type extraction |

## Adding New Functionality

### New fetcher source (via bmlib registry)
The preferred path is to add the fetcher to bmlib's registry. Once registered there, bmnews automatically picks it up — just add the source name to `config.sources.enabled`.

### New fetcher source (local)
1. Create `bmnews/fetchers/newsource.py` returning `list[FetchedPaper]`
2. Add the source name to `_LOCAL_SOURCES` in `pipeline.py`
3. Add a branch in `run_fetch()` to call the local fetcher
4. Add config toggle in `config.py` `SourcesConfig`
5. Add tests with mocked HTTP in `tests/test_fetchers.py`

### New LLM provider
Handled entirely in bmlib — no bmnews changes needed. Just update config to use `"newprovider:model-name"`. Add the provider name to the `known_providers` set in `pipeline.run_score()` for proper disambiguation.

### New database migration
Add to `db/migrations.py` following the existing versioned pattern:
1. Define SQL strings for both SQLite and PostgreSQL
2. Write a `_mNNN_description(conn)` function
3. Append a `Migration(N, "description", func)` to the `MIGRATIONS` list

### New GUI route
1. Create or extend a blueprint in `bmnews/gui/routes/`
2. Add HTMX fragment templates in `bmnews/gui/templates/fragments/`
3. Register the blueprint in `gui/app.py` `create_app()`
4. Add tests using Flask test client in `tests/test_gui_app.py`
