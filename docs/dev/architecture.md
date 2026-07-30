# System Architecture

## Pipeline overview

bmnews processes papers through a linear pipeline:

```
┌─────────┐    ┌─────────┐    ┌─────────┐    ┌───────────────┐    ┌──────────┐    ┌──────────┐
│  FETCH  │───▶│  STORE  │───▶│  SCORE  │───▶│  TRANSPARENCY │───▶│  NOTIFY  │───▶│  DIGEST  │
└─────────┘    └─────────┘    └─────────┘    └───────────────┘    └──────────┘    └──────────┘
     │              │              │                 │                 │               │
 API calls      Database       LLM calls      5 external APIs    Watch alerts     Rendering
 (httpx)        (bmlib.db)     (bmlib.llm)   (bmlib.transparency) (per paper)      + delivery
```

Each stage is an independent function that can be run individually via the CLI or composed into the full pipeline with `bmnews run`. FETCH and STORE are one `bmlib.publications.sync()` call; SCORE and DIGEST live in `pipeline.py`; TRANSPARENCY lives in `bmnews/transparency/service.py`; NOTIFY lives in `notify/service.py`.

**Neither TRANSPARENCY nor NOTIFY is gated on new scores**, unlike DIGEST. A run with nothing newly scored still has work to do: a delivery that failed last time is retried, a watch whose threshold was just loosened now matches papers already stored, and a paper that just crossed `min_combined_score` after a re-score is newly eligible for analysis. Both are also wrapped so a failure cannot take the run down — sync and scoring have already done the expensive work by then, and TRANSPARENCY specifically depends on five external APIs, any of which can be down without that costing the digest.

TRANSPARENCY **informs only** — its result is stored and displayed, never used to filter or re-rank. See [bmlib Integration](bmlib-integration.md#bmlibtransparency--research-integrity-analysis) for why, and for the retry ceiling that keeps an unindexed preprint from being re-queried forever.

## Data flow

```
        medRxiv ─┐
        bioRxiv ──┤
         PubMed ──┼──▶ bmlib.publications.sync()  ──▶ publications  (+ fulltext_sources)
       OpenAlex ──┤    one FetchedRecord per paper      │            + download_days
      EuropePMC ─┘    (bmnews's fetcher, same registry) │              (resume state)
                                                        │
                                       run_sync() stores the source `extras`
                                       blob alongside it ──▶ paper_extras
                                              │
                                    get_unscored_papers()
                                              │
                                              ▼
                               ┌───────────────────────────┐
                               │  score_papers()           │
                               │  ├─ RelevanceAgent.score()│ ◀── LLM
                               │  └─ QualityManager        │ ◀── bmlib.quality
                               └───────────────────────────┘
                                              │
                          save_score() ──▶ scores    save_paper_tags() ──▶ paper_tags
                                              │
                                  get_transparency_candidates()
                                  (combined_score ≥ min_combined_score,
                                   no result yet or unknown + attempts < 3)
                                              │
                                              ▼
                                  TransparencyAnalyzer.analyze()  ◀── CrossRef, Europe PMC,
                                              │                       PubMed, OpenAlex,
                                  save_transparency() ──▶ transparency  ClinicalTrials.gov
                                              │
                        ┌─────────────────────┴─────────────────────┐
                        ▼                                           ▼
              collect_matches()                          get_papers_for_digest()
        (derived queue, per watch+channel)          (top papers not yet in a digest)
                        │                                           │
                        ▼                                           ▼
            ┌───────────────────────┐                 ┌──────────────────────────┐
            │ notify_* templates    │                 │  render_digest()         │ ◀── Jinja2
            │ email / Matrix adapter│                 │  send_email() / stdout   │
            └───────────────────────┘                 └──────────────────────────┘
                        │                                           │
       record_notifications() ──▶ notifications      record_digest() ──▶ digests
                                                                      + digest_papers
```

Both `collect_matches()` and `get_papers_for_digest()` see `transparency_risk` / `transparency_score` on every paper they read (the `transparency` join rides along on `_PAPER_COLUMNS` / `_NOTIFY_PAPER_COLUMNS`), but neither reads them for selection — only the notify/digest templates render the risk badge.

Papers live in **bmlib's** `publications` table; bmnews owns only the scoring and delivery tables hanging off it. There is no `papers` table — migration 4 moved storage onto bmlib and dropped it. A paper dict is a join of `publications`, `paper_extras` and (when present) `scores`, assembled in one place by `_row_to_paper()`. See [Database](database.md).

Note that the two delivery paths are independent by design: `notifications` is *not* recorded in `digest_papers`, so a paper alerted on by a watch still appears in the next digest.

## Module dependency graph

```
cli.py
  └── pipeline.py
        ├── config.py (AppConfig)
        ├── db/schema.py (open_db, init_db → db/migrations.py)
        ├── db/operations.py (store, get, save, record — all SQL)
        ├── bmlib.publications.sync (fetch + store in one call; the registry
        │     holds medrxiv, biorxiv, pubmed, openalex + europepmc, which
        │     bmnews/fetchers/ registers into it)
        ├── scoring/scorer.py (score_papers)
        │     ├── scoring/relevance_agent.py (RelevanceAgent)
        │     │     └── bmlib.agents.BaseAgent
        │     └── bmlib.quality (QualityManager, QualityFilter)
        ├── transparency/service.py (deferred import — run_transparency)
        │     └── bmlib.transparency (TransparencyAnalyzer, TransparencySettings)
        ├── notify/service.py (deferred import — run_notify)
        │     ├── notify/watches.py (parse + validate)
        │     ├── notify/matcher.py (pure paper × watch → bool)
        │     ├── notify/renderer.py → templating.py
        │     └── notify/channels/ (email → digest/sender.py; matrix → httpx)
        └── digest/
              ├── renderer.py (render_digest)
              │     └── bmlib.templates.TemplateEngine
              └── sender.py (send_email)

gui/
  ├── app.py (Flask factory) → routes/ (papers, settings, pipeline, watches)
  ├── jobs.py (the one background job: lock, status, thread)
  ├── launcher.py (pywebview window)
  └── routes/papers.py → bmlib.fulltext.FullTextService

External dependencies:
  bmlib.llm           ── LLMClient, list_providers()
  bmlib.db            ── connect_*, execute, fetch_*, transaction, placeholder,
                         is_sqlite, Migration, run_migrations, create_tables
  bmlib.publications  ── sync, store_publication, register_source, FetchedRecord
  bmlib.templates     ── TemplateEngine
  bmlib.agents        ── BaseAgent
  bmlib.quality       ── QualityManager, QualityAssessment, StudyDesign, QualityTier
  bmlib.fulltext      ── FullTextService
  bmlib.transparency  ── TransparencyAnalyzer, TransparencyRisk, TransparencySettings
```

## Key design decisions

### Pure functions for database operations

All database operations in `db/operations.py` are pure functions that take a DB-API connection as the first argument, with keyword-only arguments for writes:

```python
def store_paper(conn, *, title, doi=None, pmid=None, ...) -> int:
def get_unscored_papers(conn, limit=500) -> list[dict]:
def save_score(conn, *, paper_id, ...) -> None:
```

This makes testing trivial (pass a connection from `tests.backends.new_db()`), avoids global state, and keeps the code composable.

### Backend-aware SQL

SQLite and PostgreSQL use different SQL syntax in a few places:
- Parameter placeholders: `?` (SQLite) vs `%s` (PostgreSQL)
- Auto-increment: `AUTOINCREMENT` vs `SERIAL`
- Timestamps: `datetime('now')` vs `NOW()`
- Case-insensitive search: `LIKE` vs `ILIKE`
- Unnesting a JSON array: `json_each` vs `json_array_elements_text` — which the `sources` filter needs, since `publications.sources` is a JSON array

`placeholder(conn)` and `is_sqlite(conn)` come from `bmlib.db` and are the only backend test anywhere in bmnews. Schema DDL is maintained as a pair of strings **per migration** in `db/migrations.py`; `db/schema.py` holds no DDL, only `open_db()` and an `init_db()` that runs the pending migrations.

### Versioned migrations

`init_db()` is called on every connection open and applies whatever migrations are pending, so there is no separate setup step and an old database upgrades itself. Migration 4 is the one to know about: it moved paper storage onto `bmlib.publications` and dropped bmnews's own `papers` table. It is destructive and one-way, and a row keyed on neither DOI nor PMID is written to `~/.bmnews/stranded-papers.json` rather than silently lost. See [Database](database.md).

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
- **Sync** — `download_days` records each fetched day per source, so only missing or failed days are re-fetched. Records are deduplicated by DOI *and* PMID, so a paper arriving from a second source merges rather than duplicating
- **Score** — only scores papers without an existing score entry
- **Transparency** — only analyses papers with no result yet, or an `unknown` one whose `attempts` has not reached the ceiling; a determinate result is never re-selected
- **Notify** — skips papers already delivered for that watch and channel; a `failed` row stays in the derived queue and retries
- **Digest** — only includes papers not yet linked to a digest via `digest_papers`

This means running `bmnews run` multiple times is safe and won't duplicate work.

### Concurrency model

Scoring supports configurable concurrency via `ThreadPoolExecutor`:
- `concurrency = 1` — sequential, suitable for Ollama (local LLM, one request at a time)
- `concurrency > 1` — parallel scoring threads, suitable for API providers (Anthropic) that handle concurrent requests

Transparency analysis uses the same `ThreadPoolExecutor` pattern (`transparency.concurrency`, default 3), but with one important difference: **one analyzer instance is shared across the whole pool**, because bmlib's rate limit is a lock held by the instance, not the process — a second analyzer would double the request rate against APIs that asked for one caller. Throughput is capped by that shared pacing regardless of thread count, so raising concurrency hides per-request latency rather than multiplying speed.

## Entry points

The CLI (`cli.py`) uses Click with a group/command pattern:

```
main (group)
  ├── run      → pipeline.run_pipeline()
  ├── fetch    → pipeline.run_sync()
  ├── score    → pipeline.run_score()
  ├── digest   → pipeline.run_digest()
  ├── notify   → notify.service.run_notify()  (--watch, --count, --all,
  │                                            --dry-run, --list)
  ├── transparency → transparency.service.run_transparency()  (--limit,
  │                  --refresh, --paper-id, --list, --dry-run)
  ├── init     → config.write_default_config() + schema.init_db()
  ├── gui      → gui.launcher.launch()
  └── search   → direct SQL via bmlib.db.fetch_all()
```

The `main` group handles config loading and logging setup. Each command gets the config via Click's context object (`ctx.obj["config"]`).

## GUI

`bmnews gui` is a second entry point alongside the CLI: pywebview supplies the
native window, Flask the HTTP backend, HTMX the partial-page updates. Routes,
fragments and key features are documented in `bmnews/gui/CLAUDE.md`, not
repeated here.

One thing worth stating at the architecture level: a pipeline run and a
notification delivery both write to the same database from a background
thread, and must never do so at once. `gui/jobs.py` owns the single lock,
status dict and daemon thread both go through, so starting one while the
other is busy is refused rather than raced. `gui/app.py` registers four
blueprints — `papers`, `settings`, `pipeline`, and `watches`
(`gui/routes/watches.py`) — the last of which renders the pane that lets a
user pull a watch's next batch or drain its queue without leaving the app.

## Configuration architecture

Configuration follows a layered approach:

1. **Defaults** — hardcoded in `AppConfig` dataclass field defaults
2. **Config file** — TOML file loaded by `load_config()`, overrides defaults
3. **CLI flags** — `--days`, `--verbose`, `-c` override config values at runtime

The `AppConfig` dataclass contains nested section dataclasses (`DatabaseConfig`, `SourcesConfig`, `LLMConfig`, etc.), providing type-safe access throughout the codebase.
