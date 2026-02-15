# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**bmnews** is a biomedical news reader that fetches preprints from medRxiv, bioRxiv, and Europe PMC, scores them for relevance and quality using LLMs, and delivers curated digests via email or a desktop GUI. Built on [bmlib](https://github.com/hherb/bmlib) for LLM abstraction, database utilities, quality assessment, and template rendering.

## Development Commands

```bash
# Install (editable, with all extras)
uv pip install -e ".[all]"
ALWAYS use uv to install /upgrade or otherwise manipulate packages
DO NOT use pip directly

# For local bmlib development (changes reflected immediately)
cd /path/to/bmlib && pip install -e ".[dev]"
cd /path/to/BioMedicalNews && pip install -e ".[dev]"

# Run tests
pytest                                              # all tests
pytest tests/test_db.py                             # single file
pytest tests/test_db.py::TestPapers::test_upsert    # single test

# Lint and format
ruff check bmnews/ tests/                           # lint
ruff format --check bmnews/ tests/                  # format check
ruff check --fix bmnews/ tests/                     # auto-fix
ruff format bmnews/ tests/                          # auto-format

# CLI
bmnews init          # initialize database + config (~/.bmnews/config.toml)
bmnews run           # full pipeline: fetch → store → score → digest
bmnews gui           # launch desktop GUI (pywebview + Flask + HTMX)
```

## Architecture

### Pipeline

Linear pipeline with four independent stages, each runnable individually via CLI or composed with `bmnews run`:

```
FETCH (httpx) → STORE (bmlib.db) → SCORE (bmlib.llm) → DIGEST (Jinja2 + SMTP)
```

All stages are **incremental**: fetch upserts (idempotent), score skips already-scored papers, digest only includes papers not yet in a prior digest.

### Module Dependency Graph

```
cli.py → pipeline.py → config.py (AppConfig dataclass)
                      → db/schema.py (open_db, init_db, migrations)
                      → db/operations.py (pure-function CRUD)
                      → fetchers/ (medrxiv.py, europepmc.py → FetchedPaper)
                      → scoring/scorer.py → scoring/relevance_agent.py → bmlib.agents.BaseAgent
                                          → bmlib.quality.metadata_filter
                      → digest/renderer.py → bmlib.templates.TemplateEngine
                      → digest/sender.py (SMTP)

gui/ → app.py (Flask factory) → routes/ (papers, settings, pipeline blueprints)
     → launcher.py (pywebview wrapper)
```

### bmlib Integration

bmlib is a companion library providing shared infrastructure. Key modules used:

| bmlib module | bmnews usage |
|---|---|
| `bmlib.db` | `connect_sqlite`, `connect_postgresql`, `execute`, `fetch_one`, `fetch_all`, `fetch_scalar`, `transaction` |
| `bmlib.llm` | `LLMClient` with `"provider:model"` format (e.g., `"ollama:llama3.1"`, `"anthropic:claude-sonnet-4-5-20250929"`) |
| `bmlib.agents` | `BaseAgent` — provides `render_template()`, `chat()`, `parse_json()` |
| `bmlib.templates` | `TemplateEngine` with user-dir override (`~/.bmnews/templates/`) → package `templates/` fallback |
| `bmlib.quality` | `classify_from_metadata()`, `QualityAssessment`, `StudyDesign`, `QualityTier` |
| `bmlib.fulltext` | JATS XML parser, Europe PMC/Unpaywall integration |

### Database

SQLite (default) or PostgreSQL. 4 tables with versioned migrations in `db/migrations.py`:
- **papers** — fetched paper metadata (DOI, title, authors, abstract, fulltext columns)
- **scores** — scoring results (relevance, quality, combined scores, summary, assessment JSON)
- **digests** / **digest_papers** — digest delivery tracking (many-to-many)

Backend-aware SQL: `_placeholder(conn)` returns `?` (SQLite) or `%s` (PostgreSQL). Schema DDL maintained as separate `SCHEMA_SQLITE` and `SCHEMA_POSTGRESQL` strings.

### Configuration

Layered: dataclass defaults → TOML file (`~/.bmnews/config.toml`) → CLI flags. `AppConfig` contains nested section dataclasses (`DatabaseConfig`, `SourcesConfig`, `LLMConfig`, etc.).

### Scoring

Combined score = `0.6 * relevance + 0.4 * quality`. Concurrency configurable via `ThreadPoolExecutor` (1 for local Ollama, >1 for API providers).

### GUI

Desktop app: pywebview (native window) + Flask (HTTP backend) + HTMX (frontend interactivity). Email-client layout with paper list, reading pane, settings, and pipeline controls.

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
- **No LLM calls in unit tests** — test scoring logic (tier mapping, metadata extraction) directly; mock `RelevanceAgent.score()` for integration tests.
- **Always mock SMTP** — never send real emails in tests.

## Adding New Functionality

### New fetcher source
1. Create `bmnews/fetchers/newsource.py` returning `list[FetchedPaper]`
2. Add config toggle in `config.py` `SourcesConfig`
3. Wire into `pipeline.py` `run_fetch()`
4. Add tests with mocked HTTP in `tests/test_fetchers.py`

### New LLM provider
Handled entirely in bmlib — no bmnews changes needed. Just update config to use `"newprovider:model-name"`.

### New database migration
Add to `db/migrations.py` following the existing versioned pattern.
