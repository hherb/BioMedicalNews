# System Architecture

## Pipeline overview

bmnews processes papers through a linear pipeline with four stages:

```
┌─────────┐    ┌─────────┐    ┌─────────┐    ┌──────────┐
│  FETCH  │───▶│  STORE  │───▶│  SCORE  │───▶│  DIGEST  │
└─────────┘    └─────────┘    └─────────┘    └──────────┘
     │              │              │               │
 API calls      Database       LLM calls      Rendering
 (httpx)        (bmlib.db)     (bmlib.llm)    + delivery
```

Each stage is an independent function in `pipeline.py` that can be run individually via the CLI or composed into the full pipeline with `bmnews run`.

## Data flow

```
                      medRxiv API ─┐
                      bioRxiv API ──┼──▶ list[FetchedPaper]
                      EuropePMC API ┘         │
                                              ▼
                                    ┌─────────────────┐
                                    │   upsert_paper() │ ──▶ papers table
                                    └─────────────────┘
                                              │
                                    get_unscored_papers()
                                              │
                                              ▼
                               ┌──────────────────────────┐
                               │   score_papers()          │
                               │   ├─ RelevanceAgent.score()│ ◀── LLM
                               │   └─ classify_from_metadata│ ◀── bmlib.quality
                               └──────────────────────────┘
                                              │
                                    save_score() ──▶ scores table
                                              │
                                    get_papers_for_digest()
                                              │
                                              ▼
                               ┌──────────────────────────┐
                               │   render_digest()         │ ◀── Jinja2 templates
                               │   send_email() / stdout   │
                               └──────────────────────────┘
                                              │
                                    record_digest() ──▶ digests + digest_papers
```

## Module dependency graph

```
cli.py
  └── pipeline.py
        ├── config.py (AppConfig)
        ├── db/schema.py (open_db, init_db)
        ├── db/operations.py (upsert, get, save, record)
        ├── fetchers/ (fetch_medrxiv, fetch_biorxiv, fetch_europepmc)
        ├── scoring/scorer.py (score_papers)
        │     ├── scoring/relevance_agent.py (RelevanceAgent)
        │     │     └── bmlib.agents.BaseAgent
        │     └── bmlib.quality.metadata_filter
        └── digest/
              ├── renderer.py (render_digest)
              │     └── bmlib.templates.TemplateEngine
              └── sender.py (send_email)

External dependencies:
  bmlib.llm        ── LLMClient, LLMMessage, LLMResponse
  bmlib.db         ── connect_*, execute, fetch_*, transaction
  bmlib.templates  ── TemplateEngine
  bmlib.agents     ── BaseAgent
  bmlib.quality    ── classify_from_metadata, QualityAssessment, StudyDesign, QualityTier
```

## Key design decisions

### Pure functions for database operations

All database operations in `db/operations.py` are pure functions that take a DB-API connection as the first argument:

```python
def upsert_paper(conn, *, doi, title, ...) -> int:
def get_unscored_papers(conn, limit=100) -> list[dict]:
def save_score(conn, *, paper_id, ...) -> None:
```

This makes testing trivial (pass an in-memory SQLite connection), avoids global state, and keeps the code composable.

### Backend-aware SQL

SQLite and PostgreSQL use different SQL syntax in a few places:
- Parameter placeholders: `?` (SQLite) vs `%s` (PostgreSQL)
- Auto-increment: `AUTOINCREMENT` vs `SERIAL`
- Timestamps: `datetime('now')` vs `NOW()`
- Upsert returning: `lastrowid` vs `RETURNING id`

The `_placeholder(conn)` helper detects the backend by inspecting `type(conn).__module__` and returns the correct placeholder. Schema DDL is maintained as two separate strings (`SCHEMA_SQLITE` and `SCHEMA_POSTGRESQL`) in `db/schema.py`.

### Template-driven prompts

LLM prompts are Jinja2 templates, not Python strings. This lets users customize scoring behavior without modifying code. The `TemplateEngine` from bmlib resolves templates from a user directory first, falling back to the package `templates/` directory.

### Weighted combined scoring

The combined score formula is:

```
combined_score = 0.6 * relevance_score + 0.4 * quality_score
```

Relevance is weighted higher because users care most about topic match. Quality prevents low-evidence papers (editorials, letters) from dominating.

### Incremental processing

Each pipeline stage only processes what's needed:
- **Fetch** — upserts papers, so re-fetching the same date range is safe (idempotent)
- **Score** — only scores papers without an existing score entry
- **Digest** — only includes papers not yet linked to a digest via `digest_papers`

This means running `bmnews run` multiple times is safe and won't duplicate work.

### Concurrency model

Scoring supports configurable concurrency via `ThreadPoolExecutor`:
- `concurrency = 1` — sequential, suitable for Ollama (local LLM, one request at a time)
- `concurrency > 1` — parallel scoring threads, suitable for API providers (Anthropic) that handle concurrent requests

## Entry points

The CLI (`cli.py`) uses Click with a group/command pattern:

```
main (group)
  ├── run      → pipeline.run_pipeline()
  ├── fetch    → pipeline.run_fetch() + run_store()
  ├── score    → pipeline.run_score()
  ├── digest   → pipeline.run_digest()
  ├── init     → config.write_default_config() + schema.init_db()
  └── search   → direct SQL via bmlib.db.fetch_all()
```

The `main` group handles config loading and logging setup. Each command gets the config via Click's context object (`ctx.obj["config"]`).

## Configuration architecture

Configuration follows a layered approach:

1. **Defaults** — hardcoded in `AppConfig` dataclass field defaults
2. **Config file** — TOML file loaded by `load_config()`, overrides defaults
3. **CLI flags** — `--days`, `--verbose`, `-c` override config values at runtime

The `AppConfig` dataclass contains nested section dataclasses (`DatabaseConfig`, `SourcesConfig`, `LLMConfig`, etc.), providing type-safe access throughout the codebase.
