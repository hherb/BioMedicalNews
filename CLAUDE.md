# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**bmnews** (v0.3.0) is a biomedical news reader that fetches preprints from medRxiv, bioRxiv, Europe PMC, PubMed, and OpenAlex, scores them for relevance and quality using LLMs, and delivers curated digests via email, file, stdout, or a desktop GUI. Built on [bmlib](https://github.com/hherb/bmlib) for LLM abstraction, database utilities, quality assessment, fetcher registry, fulltext retrieval, and template rendering.

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

# The DB tests run once per backend. Without a DSN the PostgreSQL half skips;
# point BMNEWS_TEST_PG_DSN at a live server to run it (CI does this from a
# `services: postgres` container). The tests create and drop their own schemas,
# so give them a scratch database, not one with anything in it.
BMNEWS_TEST_PG_DSN=postgresql://bmnews:bmnews@localhost:5432/bmnews_test pytest
pytest -k postgresql                                # just the PostgreSQL runs

# Lint and format
ruff check bmnews/ tests/                           # lint
ruff format --check bmnews/ tests/                  # format check
ruff check --fix bmnews/ tests/                     # auto-fix
ruff format bmnews/ tests/                          # auto-format
```

## Architecture

### Pipeline

Linear pipeline with four independent stages, each runnable individually via CLI or composed with `bmnews run`:

```
SYNC (bmlib.publications.sync) → SCORE (bmlib.llm) → NOTIFY (watches) → DIGEST (Jinja2 + SMTP)
```

All stages are **incremental**: sync records each fetched day in `download_days` and re-fetches only the days that are missing or failed, score skips already-scored papers, notify skips papers already delivered for that watch and channel, and digest only includes papers not yet in a prior digest.

`run_pipeline()` gates DIGEST on `scored > 0` but **not** NOTIFY: a run with nothing newly scored still has a failed delivery to retry and a just-loosened watch to honour. NOTIFY is also wrapped so a failure cannot take the run down — the expensive stages have already finished by then.

### Directory Structure

```
bmnews/
├── __init__.py          # Package version (0.3.0)
├── cli.py               # Click CLI commands (run, fetch, score, digest, notify, init, gui, search)
├── config.py            # TOML config loading (AppConfig + nested section dataclasses)
├── constants.py         # Fixed application constants (scoring weights, page sizes, timeouts)
├── metadata.py          # Defensive decoding of the paper_extras.metadata_json blob
├── pipeline.py          # Orchestration: sync → score → notify → digest (progress callbacks)
├── templating.py        # TEMPLATES_DIR + build_template_engine (digest, notify and GUI all need them)
├── db/
│   ├── schema.py        # Database connection factory (open_db, init_db)
│   ├── operations.py    # Pure-function CRUD (all SQL lives here)
│   └── migrations.py    # 6 versioned migrations (the 4th moves storage onto bmlib)
├── fetchers/
│   ├── __init__.py      # Registers bmnews-supplied sources with bmlib's registry
│   └── europepmc.py     # Europe PMC fetcher (bmlib registry calling convention)
├── scoring/
│   ├── scorer.py        # Orchestrates relevance (LLM) + quality (bmlib.quality) scoring
│   └── relevance_agent.py  # LLM-based relevance scoring agent (extends BaseAgent)
├── digest/
│   ├── renderer.py      # Jinja2 digest rendering (HTML + plain text)
│   └── sender.py        # SMTP email delivery (TLS, multipart MIME)
├── notify/              # Watch-based alerts (CLI, pipeline stage, GUI pane)
│   ├── watches.py       # Watch/Channel dataclasses, validated from config dicts
│   ├── matcher.py       # Pure `(paper, watch) -> bool`. No I/O, no LLM
│   ├── renderer.py      # Renders a batch into the notify_* templates
│   ├── service.py       # run_notify(): select, page, dispatch, record
│   └── channels/        # Delivery adapters, dispatched by a channel's `kind`
│       ├── __init__.py  # ChannelError, Message, build_adapter, transaction_key
│       ├── email.py     # Wraps digest/sender.py over the [email] SMTP settings
│       └── matrix.py    # One authenticated HTTP PUT — no SDK, no E2EE
└── gui/
    ├── app.py           # Flask application factory (registers blueprints)
    ├── launcher.py      # pywebview window launcher (geometry persistence, port auto-detect)
    ├── helpers.py        # HTML formatting helpers (abstract section parsing)
    ├── jobs.py          # The one background job: lock, status, thread, status-bar fragment
    ├── routes/
    │   ├── papers.py    # Paper list/detail, search, fulltext retrieval
    │   ├── pipeline.py  # Async pipeline execution + status polling
    │   ├── settings.py  # Settings UI, dynamic model selector
    │   └── watches.py   # Watches pane: counts, delivery, refresh
    ├── static/
    │   ├── css/app.css  # Email-client UI styling
    │   ├── js/app.js    # Split-pane, tabs, fulltext toggle, HTMX events
    │   └── vendor/      # htmx.min.js (v2.x), split-grid.min.js
    └── templates/
        ├── base.html    # Main layout (nav tabs, split pane, status footer)
        └── fragments/   # HTMX partial templates (paper_list, reading_pane, settings,
                          # watches_view, watch_list, watch_poller, etc.)

templates/                     # Email digest + notification + LLM prompt templates (Jinja2)
├── digest_email.html          # HTML email digest
├── digest_text.txt            # Plain-text email digest
├── notify_email.html/.txt     # Watch notification, email (styled like the digest)
├── notify_matrix.html/.txt    # Watch notification, Matrix (no CSS — see below)
├── relevance_system.txt       # LLM system prompt for relevance scoring
└── relevance_scoring.txt      # LLM user prompt (paper title, abstract, interests)

tests/                         # Test suite (14 files)
docs/plans/                    # Implementation design documents and plans
bmlib_patch/                   # bmlib source archive and patch files
```

### Module Dependency Graph

```
cli.py → pipeline.py → config.py (AppConfig dataclass)
                      → db/schema.py (open_db, init_db, migrations)
                      → db/operations.py (pure-function CRUD)
                      → bmlib.publications.sync (fetch + store, one call; registry:
                                            medrxiv, biorxiv, pubmed, openalex
                                            + europepmc, registered by bmnews.fetchers)
                      → scoring/scorer.py → scoring/relevance_agent.py → bmlib.agents.BaseAgent
                                          → bmlib.quality.metadata_filter
                      → digest/renderer.py → bmlib.templates.TemplateEngine
                      → digest/sender.py (SMTP)
                      → notify/service.py (deferred import) → notify/matcher.py (pure)
                                          → notify/watches.py (parse + validate)
                                          → notify/renderer.py → templating.py
                                          → notify/channels/ → email.py → digest/sender.py
                                                             → matrix.py (httpx PUT)

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
| `bmlib.llm.providers` | `list_providers()` — the authority on which `provider:` prefixes exist; never hardcode a provider list |
| `bmlib.agents` | `BaseAgent` — provides `render_template()`, `chat()`, `chat_json()`, `parse_json()` |
| `bmlib.templates` | `TemplateEngine` with user-dir override (`~/.bmnews/templates/`) → package `templates/` fallback |
| `bmlib.quality` | `QualityManager`, `QualityFilter`, `QualityAssessment`, `StudyDesign`, `QualityTier`, `DESIGN_TO_TIER`, `DESIGN_TO_SCORE` — the evidence hierarchy and its scores live here, not in `bmnews.constants` |
| `bmlib.fulltext` | `FullTextService` (3-tier: Europe PMC → Unpaywall → DOI), JATS XML parser, `FullTextError` |
| `bmlib.publications` | `sync()` — the whole fetch-and-store cycle; `ensure_schema()`, `store_publication()`, `get_publication_by_doi/pmid()` — the `publications` table bmnews's papers live in; `register_source()`, `source_names()`, `FetchedRecord`, `SyncProgress`, `SyncReport`, `SourceDescriptor` — the registry every source goes through |

**Not currently used** (see `docs/plans/` before adopting): `bmlib.transparency` (the `[transparency]` config section and extra are declared but unwired).

### Source Fetching

**Every** source resolves through bmlib's registry — there is no second dispatch path.

- medRxiv, bioRxiv, PubMed and OpenAlex ship with bmlib.
- Europe PMC is implemented in `bmnews/fetchers/europepmc.py` and registered into the same registry by `bmnews/fetchers/__init__.py` via `bmlib.publications.register_source()`. It follows the registry calling convention exactly: `fetcher(client, target_date, *, on_record, on_progress=None, **config)`, emitting `FetchedRecord` and returning a `FetchResult`.

`pipeline.run_sync()` hands the whole cycle to `bmlib.publications.sync()`: it walks the lookback window, skips days already recorded as complete in `download_days`, stores each day in one transaction, and deduplicates records by DOI *and* PMID. `SyncProgress` is rendered down to bmnews's `on_progress(str)` callback by `_progress_reporter()` so the GUI status bar keeps working.

Sources are configured via `config.sources.enabled` (e.g. `["medrxiv", "europepmc", "biorxiv", "pubmed", "openalex"]`); per-source options in `config.sources.source_options` are unpacked as keyword arguments to the fetcher (e.g. `query` for Europe PMC, `api_key` for PubMed).

`FetchedRecord.publication_types` feeds bmlib's free Tier-1 quality classification — dropping it silently forces every paper onto the LLM classifier. It reaches the scorer through the `publications.publication_types` column, via `_extract_pub_types()`.

### Database

SQLite (default) or PostgreSQL. Papers live in **bmlib's** tables; bmnews owns everything about scoring and delivery.

Owned by bmlib (`bmlib.publications.ensure_schema`):
- **publications** — the paper record (doi, pmid, pmcid, title, abstract, authors/keywords/publication_types/sources as JSON arrays, journal, is_open_access, license)
- **fulltext_sources** — full-text URLs a fetcher reported for a publication
- **download_days** — per-source, per-day fetch status, which is what makes sync resumable

Owned by bmnews:
- **scores** — scoring results (relevance, quality, combined scores, summary, study_design, quality_tier, assessment JSON)
- **digests** / **digest_papers** — digest delivery tracking (many-to-many)
- **paper_tags** — per-paper interest tags matched during scoring
- **paper_extras** — the leftovers bmlib has no column for: the source `extras` blob (`cited_by`), the GUI's cached full text, and the PDF that text was extracted from (`fulltext_pdf_url`, kept beside the HTML because extraction loses figures and layout). One publication can be fed by several sources, so `save_paper_metadata()` merges key by key rather than replacing the blob (a later value wins; a key it says nothing about survives).
- **notifications** — one row per *delivered* watch notification, unique on `(watch, paper_id, channel)`. The pending queue is **not** stored: it is derived per run as "papers this watch matches now, minus those already sent", which is what makes paging idempotent and stops an edited watch from leaving orphaned queue rows. A `failed` row stays in the derived queue, so it retries. This table must stay separate from `digest_papers` — `get_papers_for_digest()` excludes papers present there and nothing else, so recording a notification in it would silently suppress that paper's digest entry.

`scores`, `paper_tags`, `digest_papers` and `notifications` keep a column named `paper_id`; it references `publications(id)`. "Paper" stays bmnews's noun for the thing — the GUI routes are `/papers/<id>`.

Migrations in `db/migrations.py`:
1. `initial_schema` — papers, scores, digests, digest_papers tables
2. `add_paper_tags` — paper_tags table for interest matching
3. `add_fulltext_columns` — adds pmid, pmcid, fulltext_html, fulltext_source to papers; backfills pmid/pmcid from metadata_json for europepmc papers
4. `migrate_to_publications` — replays every `papers` row through `store_publication()` so bmlib's dedupe decides identity, repoints the three bmnews-owned tables that reference a paper (`scores`, `paper_tags`, `digest_papers` — `digests` itself carries no paper reference) at the resulting ids, and drops `papers`. Where two rows collapse into one publication, the surviving score is the highest `combined_score` (the one the digest showed); tags and digest links are unioned, and metadata merges key by key with the later row winning. **This migration is destructive and one-way**: a row that can be keyed on neither DOI nor PMID cannot be represented, so it is logged at ERROR and written to `~/.bmnews/stranded-papers.json` (`constants.STRANDED_PAPERS_PATH`) before `papers` is dropped.
5. `add_notifications` — the `notifications` table above
6. `add_fulltext_pdf_url` — adds `paper_extras.fulltext_pdf_url`, so a PDF the text was extracted from is kept *beside* the HTML rather than instead of it (extraction loses figures, tables and layout, so the reading pane offers both). Also clears full text stored under a preprint server's own name (`_STALE_FULLTEXT_SOURCES`) — those rows hold an abstract-only rendering of a body-less JATS document — **and deletes the matching file from bmlib's disk cache**. Both halves are needed: bmlib consults its cache before the database, so clearing the row alone would have the next request served the same file and stored again under the `cached` source name, out of reach of any filter keyed on the server's name. A cache that cannot be opened is logged and skipped rather than failing the migration.

Backend-aware SQL: `placeholder(conn)` (from `bmlib.db`) returns `?` (SQLite) or `%s` (PostgreSQL). Schema DDL maintained as separate SQLite and PostgreSQL strings per migration. The `sources` filter in `get_papers_filtered()` unnests a JSON array, so it is backend-specific too — `json_each` on SQLite, `json_array_elements_text` on PostgreSQL.

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
| `[notifications]` | `NotificationsConfig` | `enabled`, `channels` (dict of dicts), `watches` (dict of dicts) |

`channels` and `watches` are dicts keyed by name, like `sources.source_options`, and that shape is forced by `save_config`: `_toml_value` stringifies list elements, so an array-of-tables would round-trip as Python dict reprs, and `_write_section` emits three table levels, so anything deeper is dropped on every GUI save. `_apply_section` does no validation, so `notify/watches.py` parses these dicts into `Watch`/`Channel` and warns on unknown keys rather than ignoring them silently.

### Notifications

A **watch** is named criteria that alert on a matching paper as it is scored, separately from the periodic digest — a notified paper is still included in the next digest. Design: `docs/plans/2026-07-26-notification-service-design.md`.

Surfaces: the `bmnews notify` CLI (`--watch`, `--count`, `--all`, `--dry-run`, `--list`), the NOTIFY stage of `run_pipeline()`, and the GUI watches pane (`/watches`). The pane monitors and delivers; watches are still created and edited in `config.toml`.

Criteria are AND-combined; an empty list means "no constraint", and within one list criterion the test is `any`. `matcher._tier_ok()` reuses `scoring.scorer.tiers_below()`, so the tier floor exempts `UNCLASSIFIED` exactly as the digest does. The matcher reads `paper["tags"]`, which `publications` has no column for — `get_notification_candidates()` attaches them from `paper_tags` per chunk.

Three things constrain how this is built, and undoing any of them reintroduces a bug the design set out to avoid:

1. **The pending queue is derived, never stored** — "papers this watch matches now, minus those with a `sent` row for that channel". That is what makes paging idempotent (asking for five more re-runs the identical selection) and what stops an edited watch from leaving orphaned queue rows.
2. **The delivery cap must not become a SQL `LIMIT`.** SQL narrows on what is indexable (score floors, tier exclusion, the anti-join, the ordering); the matcher applies keywords, tags, sources, journal and study design in Python afterwards. Limiting to five rows before the matcher rejects three of them delivers two while more matches sit further down. `collect_matches()` scans in `NOTIFY_SCAN_CHUNK`-sized chunks until one comes back short and returns *every* pending match; `_deliver()` slices the batch off that. `NOTIFY_SCAN_CHUNK` is a scan window and never a delivery cap. The scan runs to exhaustion because `remaining` has to be exact, which is affordable only because `get_notification_candidates()` selects `_NOTIFY_PAPER_COLUMNS` — deliberately *not* `_PAPER_COLUMNS`, whose `p.*` drags the GUI's cached full text through a query that materialises every candidate.
3. **`notifications` stays out of `digest_papers`** — see the table description above.

Delivery adapters raise `ChannelError` rather than returning a boolean: `digest.sender.send_email` returns `False` on failure, and a `False` read as success would mark papers sent and drop them out of the derived queue with nobody having been told. `ChannelError` is the **only** exception `run_notify()` treats as "this delivery did not happen", so every transport failure has to be converted to one — `MatrixChannel._request()` wraps each HTTP call for exactly that reason, since an `httpx` connect error escaping raw would skip recording the attempt and abandon every watch still to be delivered in that run. A failed send records `status='failed'`, which keeps the paper queued for the next run — per channel, so Matrix succeeding while email fails retries only the email. The batch is recorded through `record_notifications()` in one transaction: half a batch recorded would send the rest again under a *different* `txnId`.

Matrix delivery is one authenticated HTTP PUT, no SDK. The `txnId` is derived from `(watch, channel, sorted paper_ids)` rather than randomly, because the homeserver treats a repeat as a retransmission — that closes the "message sent, row not yet written, crash" window. Encrypted rooms are **refused**, not deferred: a plain PUT there posts ciphertext nobody can read and reports success, and the content is alerts about public preprints. A non-`https` `homeserver` is refused too (loopback excepted) — the access token is a bearer credential on every request and does not expire on its own. The Matrix templates stay on headings, lists and links — that HTML subset has no CSS at all, which is why the digest's markup is not reused. All four `notify_*` templates escape their interpolations explicitly: bmlib's `TemplateEngine` runs with `autoescape=False` (it was built for plain-text LLM prompts) and the metadata is third-party.

### Scoring

Two-tier scoring system:

1. **Relevance (LLM-based)** — `RelevanceAgent` sends paper title + abstract + user interests to LLM via Jinja2 template prompts. Returns JSON with `relevance_score` (0.0–1.0), `summary`, `key_findings`, `matched_tags`.
2. **Quality (bmlib.quality)** — Tiered assessment (metadata classification → LLM classifier → deep analysis). Maps `QualityTier` to a 0.0–1.0 score.

Combined score = `RELEVANCE_WEIGHT * relevance + QUALITY_WEIGHT * quality` (0.6 / 0.4, defined in `bmnews/constants.py`). When `config.quality.enabled` is false, the quality stage is skipped entirely and the combined score is the relevance score alone. Concurrency configurable via `ThreadPoolExecutor` (`concurrency=1` for local Ollama, `>1` for API providers).

Config wiring: `llm.temperature` / `llm.max_tokens` are passed to `RelevanceAgent`; `quality.default_tier` is clamped by `quality.max_tier`; `quality.min_quality_tier` and `scoring.min_relevance` filter the digest in `get_papers_for_digest()`. `UNCLASSIFIED` papers are never excluded by a tier floor — unjudged is not judged-and-rejected.

**LLM providers** (via bmlib, see `list_providers()`): Ollama, Anthropic, OpenAI, Deepseek, Mistral, Gemini. Model format: `"provider:model"` (e.g., `"anthropic:claude-3-haiku"`). The pipeline disambiguates bare model names with tags (e.g., `"llama3.1:latest"`) from provider-prefixed strings.

### GUI

The desktop GUI (pywebview + Flask + HTMX) is documented in `bmnews/gui/CLAUDE.md`, which loads automatically when you work under `bmnews/gui/`.

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
- **No magic numbers** — fixed behavioural values live in `bmnews/constants.py`; anything a user should tune belongs in `bmnews/config.py`.
- **Close connections with `contextlib.closing`** — `with closing(open_db(config)) as conn:`, so a raised exception can't leak the handle.
- **Never rely on `cursor.lastrowid` after an upsert** — SQLite leaves it pointing at the last row actually *inserted* when `ON CONFLICT` takes the UPDATE path. Look the row up by its natural key instead (see `store_paper`, which re-reads by normalised DOI/PMID).
- **Decode a paper row exactly once** — `_row_to_paper()` is the only place the JSON array columns become lists and the outbound `url` is derived. Callers, templates and the scorer all take real lists; nothing re-parses JSON downstream.
- **AGPL-3.0 license**.
- **Commit messages** — conventional style: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.

## Testing Patterns

- **Both backends for DB tests** — `tests/test_db.py` sets `pytestmark = pytest.mark.usefixtures("db_backend")`, so every test in it runs once per backend. Build databases with `tests.backends.new_db()`, never `connect_sqlite(":memory:")` directly, and use `placeholder(conn)` / `bmlib.db.execute` in test helpers rather than raw `conn.execute("… ?")`. SQLite is in-memory; PostgreSQL runs only when `BMNEWS_TEST_PG_DSN` names a live server (CI's `test-postgresql` job), each connection isolated in a schema of its own, and is skipped otherwise.
- **In-memory SQLite** for the non-DB suites (pipeline, GUI, fulltext) — the backend-specific SQL all lives in `db/operations.py` and `db/migrations.py`, which `test_db.py` covers.
- **Mocked HTTP** (httpx) for fetcher tests, **mocked `open_db`** for pipeline/CLI tests.
- **Click's `CliRunner`** for CLI command tests.
- **Flask test client** for GUI route tests (`create_app()` with test config, `client.get()`/`client.post()`).
- **No LLM calls in unit tests** — test scoring logic (tier mapping, metadata extraction) directly; mock `RelevanceAgent.score()` for integration tests.
- **Always mock SMTP** — never send real emails in tests.

Test files:
| File | Coverage |
|---|---|
| `test_config.py` | Config loading, TOML parsing, backward-compat defaults |
| `test_db.py` | All database operations, migrations, storing/dedup, filtering, tagging, digests, paper extras, digest selection filters, notification candidate selection and batch recording (including its rollback), the v3 → v4 data migration, migration 6's cache purge, and NULL text columns decoding to strings — **run against SQLite and PostgreSQL** |
| `backends.py` / `conftest.py` | Not tests: the per-backend parameterisation `test_db.py` opts into, plus the suite-wide autouse fixture that returns `bmnews.gui.jobs`' process state to idle around every test — forcing its lock open if a worker outlived the test, since one leaked job would otherwise make every later `jobs.start()` refuse |
| `test_digest.py` | HTML/text digest rendering |
| `test_fetchers.py` | Europe PMC fetcher + its registration in bmlib's source registry |
| `test_fulltext_integration.py` | Fulltext service integration (Europe PMC/Unpaywall/DOI) |
| `test_gui_app.py` | Flask blueprints, HTMX responses, paper queries, pipeline status, the View PDF button, and the outbound-URL scheme allowlist |
| `test_gui_helpers.py` | Abstract HTML formatting |
| `test_gui_jobs.py` | The shared background job — refusal while one runs without clobbering its progress line, a raising target freeing the lock, a target that forgets to clear `running` |
| `test_gui_notify.py` | The watches pane — the count join with delivered/matching/remaining pinned in column order, an unresolved channel and an unparseable watch (both produce no counts at all) versus a disabled watch (counts render, buttons don't), a partly-resolved channel list naming what was dropped, watches that all fail to parse not reading as "none configured", the no-criteria summary, delivery and drain, failed-delivery and nothing-to-notify reporting, a multi-channel run counting notifications rather than papers, a delivery refused by a running job saying so, a disabled watch refused before any job starts, a slash in a watch name surviving into the URL, HTML in one being escaped, 404 on an unknown watch, the counts being skipped (but the config notices not) while a job runs, the 204-while-running refresh, and one unmocked pass against a real database |
| `test_notify.py` | Every watch criterion in isolation against literal paper dicts; watch/channel parsing, validation and unknown-key warnings |
| `test_notify_channels.py` | Channel adapters and the four templates: Matrix endpoint/auth/body shape, deterministic `txnId`, alias resolution, encrypted-room refusal, transport errors arriving as `ChannelError`, non-https homeserver refusal, HTML escaping of third-party metadata; email over mocked SMTP, including a `False` return raising |
| `test_notify_service.py` | `run_notify` — paging with no gaps or repeats, chunk-boundary exhaustion, dedup, per-channel retry, dry run leaving `sent_total` unmoved, contradictory CLI batch sizes, and the `bmnews notify` CLI. Plus `collect_matches` directly: scanning past the chunk window, and not carrying the full-text cache. File-backed SQLite, since each run opens its own connection |
| `test_pipeline.py` | Show-cached flag, CLI integration, `run_sync` storage via bmlib (identifiers, publication types, full-text sources, extras), source dispatch, per-source config, and the notify stage's placement (ungated, before the digest, failure-contained) |
| `test_scoring.py` | Quality tier mapping, publication type extraction, tier floors, quality toggle, generation settings, NULL-abstract normalisation, and one failing paper not aborting the run |

## Adding New Functionality

### New fetcher source (via bmlib registry)
The preferred path is to add the fetcher to bmlib's registry. Once registered there, bmnews automatically picks it up — just add the source name to `config.sources.enabled`.

### New fetcher source (local to bmnews)
Follow the Europe PMC pattern — do **not** add a second dispatch path in `pipeline.run_sync()`:
1. Create `bmnews/fetchers/newsource.py` with a function matching the registry convention: `fetcher(client, target_date, *, on_record, on_progress=None, **config)`, emitting `FetchedRecord` and returning a `FetchResult`
2. Add a `SourceDescriptor` and a `register_source(...)` call to `register_local_sources()` in `bmnews/fetchers/__init__.py`
3. Add tests with a fake HTTP client in `tests/test_fetchers.py`

The source is then selectable by name in `config.sources.enabled`, appears in the GUI settings list, and accepts per-source options through `config.sources.source_options` — all with no further changes.

### New LLM provider
Handled entirely in bmlib — no bmnews changes needed. Just update config to use `"newprovider:model-name"`; `_resolve_model_string()` asks `bmlib.llm.providers.list_providers()` which prefixes are real.

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
