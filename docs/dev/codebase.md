# Codebase Guide

Module-by-module walkthrough of the bmnews source code.

## `bmnews/__init__.py`

Package root. Defines `__version__`.

## `bmnews/cli.py`

CLI entry point using Click. Defines the `main` group and all subcommands.

**Key patterns:**
- `@click.group(cls=_CleanFailureGroup)` on `main()` with global options (`--config`, `--verbose`, `--version`)
- Config is loaded in `main()` and stored in `ctx.obj["config"]` for subcommands
- Lazy imports: each command imports pipeline functions inside the function body, keeping startup fast
- Each command is thin — it calls pipeline functions and reports results
- `_CleanFailureGroup.invoke()` converts an exception no command handled into a `ClickException`: the user sees `Error: <command> failed: <type>: <message>` and exit code 1, and the traceback is logged with `exc_info` at DEBUG so `bmnews -v` still prints it. It lives on the group, not on each command, so a command added later cannot forget it. `ClickException`, `click.exceptions.Exit` and `click.Abort` pass through untouched — all three are `RuntimeError` subclasses, so a bare `except Exception` would swallow them, flattening `UsageError`'s exit code 2 into 1 and dropping the exit code `notify` sets when every delivery failed

**Commands:**
- `run(days, show_cached)` — full pipeline or cached display
- `fetch(days)` — sync only (fetch and store are one call)
- `score()` — score unscored papers
- `digest(output)` — render and deliver
- `notify(watch, count, all, dry_run, list)` — deliver watch notifications. `--all` and `--count` both set the batch size, so setting both is a `UsageError` rather than a silent choice
- `transparency(limit, refresh, paper_id, list_only, dry_run)` — assess research integrity via `bmnews.transparency.service`. `--list` is checked before the `[transparency] enabled` guard, so stored results stay readable after the feature is switched off; `--list` combined with `--refresh`/`--dry-run` is a `UsageError`, since one means "analyse nothing" and the others mean "analyse differently"
- `init(config_path)` — first-time setup
- `gui(port)` — launch the desktop GUI; a missing `gui` extra is reported as an install hint, not a traceback
- `search(query, limit)` — keyword search with direct SQL

## `bmnews/config.py`

TOML configuration loading with typed dataclass access.

**Key components:**
- `DEFAULT_CONFIG_DIR` / `DEFAULT_CONFIG_PATH` — `~/.bmnews/config.toml`
- Section dataclasses: `DatabaseConfig`, `SourcesConfig`, `LLMConfig`, `ScoringConfig`, `QualityConfig`, `TransparencyConfig`, `UserConfig`, `EmailConfig`, `NotificationsConfig`
- `AppConfig` — top-level dataclass aggregating all sections plus `log_level` and `template_dir`
- `load_config(path)` — loads TOML, applies values onto dataclass defaults, ignores unknown keys
- `write_default_config(path)` — writes `DEFAULT_CONFIG_TOML` if file doesn't exist
- `save_config(config, path)` — writes the config back out (the GUI settings pane uses this)
- `_apply_section(dc, data)` — maps dict keys to dataclass attributes; also handles the rename in `_DEPRECATED_KEYS`

**Design notes:**
- Uses `tomllib` (stdlib since Python 3.11) for TOML parsing
- Unknown config keys are silently ignored (forward compatibility). That is why `notify/watches.py` re-validates the notification tables and *warns* about keys it does not recognise — a misspelled criterion would otherwise sit in the config doing nothing
- `_DEPRECATED_KEYS: dict[type, dict[str, str]]` maps a renamed key to its current name, per section dataclass — currently just `TransparencyConfig: {"min_score_threshold": "min_combined_score"}`. `_apply_section()` assigns only to attributes a dataclass already has, so without this map a rename would silently discard a value the user had deliberately set and fall back to the default instead of warning about it
- `sources.source_options`, `notifications.channels` and `notifications.watches` are dicts keyed by name. That shape is forced by `save_config`: `_toml_value` stringifies list elements (an array-of-tables would round-trip as Python dict reprs) and `_write_section` emits three table levels, so anything deeper is dropped on every GUI save
- All fields have defaults, so the app works even with an empty config

## `bmnews/constants.py`

Fixed behavioural values — scoring weights, page sizes, timeouts, `NOTIFY_SCAN_CHUNK`, `STRANDED_PAPERS_PATH`. Anything a *user* should be able to tune belongs in `config.py` instead. The evidence hierarchy and its scores live in `bmlib.quality`, not here.

Two transparency constants: `TRANSPARENCY_BATCH_SIZE` (100) bounds one run the same way `UNSCORED_BATCH_SIZE` bounds a scoring run — bmlib paces every outbound request 0.35s apart across the whole analyzer regardless of thread count, so this caps a run to a few minutes and leaves the rest queued. `TRANSPARENCY_MAX_ATTEMPTS` (3) is the retry ceiling described in [Database](database.md#transparency) — the fixed value behind `[transparency]`'s cost gate, not itself user-tunable.

## `bmnews/metadata.py`

Defensive decoding of the `paper_extras.metadata_json` blob — it comes from third-party sources and may be absent, empty, or not a dict. `parse_transparency()` does the same for `transparency.result_json`, deliberately as a plain dict rather than through bmlib's `TransparencyResult.from_dict()` — that classmethod raises on an `unknown_reason` member it does not recognise, which must not stop a paper's page from rendering when a newer bmlib starts writing one the pinned version predates.

## `bmnews/templating.py`

`TEMPLATES_DIR` and `build_template_engine(config)`. It lives outside `pipeline.py` because the digest, the notification renderer and the GUI all need an engine and none of them should have to import the pipeline to get one.

## `bmnews/pipeline.py`

Central orchestration module.

**Builder functions:**
- `build_llm_client(config)` — creates `LLMClient` from provider/host/key settings
- `_resolve_model_string(config)` — disambiguates a bare model name with a tag (`"llama3.1:latest"`) from a provider-prefixed string, asking `bmlib.llm.providers.list_providers()` which prefixes are real

**Pipeline stages:**
- `run_sync(config, on_progress)` → `SyncReport` — hands the whole fetch-and-store cycle to `bmlib.publications.sync()`, which walks the lookback window, skips days already recorded complete in `download_days`, and stores each day in one transaction. `_progress_reporter()` renders bmlib's `SyncProgress` down to bmnews's `on_progress(str)` callback so the GUI status bar keeps working; `_record_extras()` / `_store_extras()` keep the source `extras` blob in `paper_extras`
- `run_score(config)` → `int` — scores unscored papers with LLM + quality
- `run_digest(config, output)` → `str` — renders and delivers digest
- `show_cached_digests(config, days)` → `str` — re-renders previous digest papers
- `run_pipeline(config, days, show_cached)` — orchestrates all stages: SYNC → SCORE → TRANSPARENCY → NOTIFY → DIGEST
- `_run_transparency_stage(config, on_progress)` — the TRANSPARENCY stage, wrapped so a failure cannot take the run down; a no-op when `[transparency] enabled = false`
- `_run_notify_stage(config, on_progress)` — the NOTIFY stage, wrapped so a failure cannot take the run down

**Design notes:**
- Each `run_*` function opens and closes its own DB connection with `contextlib.closing`
- `run_pipeline` short-circuits to `show_cached_digests` when `show_cached=True`
- The `days` parameter overrides `config.sources.lookback_days` at runtime
- DIGEST is gated on `scored > 0`; **neither TRANSPARENCY nor NOTIFY is**. A run with nothing newly scored still has a failed delivery to retry, a just-loosened watch to honour, and a paper that just crossed `min_combined_score` to analyse

## `bmnews/fetchers/`

Every source resolves through **bmlib's registry** — there is no second dispatch path in bmnews. medRxiv, bioRxiv, PubMed and OpenAlex ship with bmlib; this package holds the one bmnews supplies and registers it into the same registry.

### `__init__.py`

`register_local_sources()` builds a `SourceDescriptor` and calls `bmlib.publications.register_source()`. Once registered, a source is selectable by name in `config.sources.enabled`, appears in the GUI settings list, and takes per-source options from `config.sources.source_options`.

### `europepmc.py`

Fetches from the Europe PMC REST API, following the registry calling convention exactly:

```python
def fetch_europepmc(client, target_date, *, on_record, on_progress=None, **config) -> FetchResult:
```

- **API:** `https://www.ebi.ac.uk/europepmc/webservices/rest/search`
- **Pagination:** cursor-mark based
- Default query: `SRC:PPR` (preprints only), filtered to the target date
- Emits a `FetchedRecord` per paper, including `publication_types` — dropping that field silently forces every paper onto the LLM quality classifier instead of bmlib's free Tier-1 classification
- Extras captured: `cited_by`, plus the identifiers and open-access status bmlib has columns for

## `bmnews/scoring/`

LLM-based relevance scoring and quality assessment.

### `relevance_agent.py`

`RelevanceAgent` extends `bmlib.agents.BaseAgent` to score papers for relevance.

- Renders `relevance_system.txt` (system prompt) and `relevance_scoring.txt` (user prompt with paper data)
- Calls LLM in JSON mode
- Parses response with `BaseAgent.parse_json()` (handles markdown code blocks)
- Returns dict with `relevance_score`, `summary`, `key_findings`, `matched_tags`
- Clamps score to 0.0–1.0
- Falls back to score 0.0 on parse failure

### `scorer.py`

Orchestrates scoring for a batch of papers.

- `score_papers(papers, llm, model, template_engine, interests, concurrency, quality_enabled, quality_tier, temperature, max_tokens, progress_callback)` — main entry point. `progress_callback(current, total, result)` fires once per paper on the calling thread, **including** the papers that failed to score — that is what makes `current` reach `total`, and a failure on the last one used to strand a status bar at `n-1/n` for good. `result` is `None` for those, which is why `pipeline._score_progress` checks `isinstance(result, dict)` before storing anything
- `_score_single(...)` — scores one paper: relevance via `RelevanceAgent.score()`, quality via `bmlib.quality.QualityManager`, then `RELEVANCE_WEIGHT * relevance + QUALITY_WEIGHT * quality`
- `_build_quality_filter(max_tier)` — clamps the assessment depth (1 = metadata only, 2 = LLM classifier, 3 = deep analysis)
- `_extract_pub_types(paper)` — reads `publications.publication_types`, which is what feeds Tier-1 classification
- `_quality_tier_to_score(assessment)` — maps a `QualityAssessment` to 0.0–1.0
- `tiers_below(min_tier)` — the tier floor, shared with the notification matcher so both exempt `UNCLASSIFIED` the same way

**Quality toggle:** when `config.quality.enabled` is false the quality stage is skipped entirely and the combined score is the relevance score alone.

**Concurrency:** `ThreadPoolExecutor` when `concurrency > 1`. Errors on individual papers are logged but don't stop the batch.

## `bmnews/transparency/`

The research-integrity stage: select, analyse, store. Query-based like `notify/service.py` rather than callback-driven, so it survives a crash mid-run and is testable without running the scorer at all.

### `service.py`

- `run_transparency(config, *, refresh, paper_id, limit, dry_run, on_progress) → TransparencyReport` — the whole stage. Returns immediately with an empty report when `[transparency] enabled = false`
- `build_settings(config) → TransparencySettings` — maps bmnews's four config fields onto bmlib's settings object; forces `enabled=True` regardless of config (the caller already checked) because a settings object claiming disabled would make bmlib hand back an UNKNOWN placeholder that then blocks the paper from ever being tried again
- `_build_analyzer(config) → TransparencyAnalyzer` — reuses the PubMed API key from `sources.source_options.pubmed` rather than duplicating it into `[transparency]`
- `_analyze_all(conn, analyzer, candidates, …)` — runs the pool. **One analyzer instance is shared across every worker** (bmlib's rate limit is per-instance), and **all storage happens on the calling thread** (a SQLite connection is not thread-safe) — the same shape `score_papers()` uses for its progress callback. Neither the analysis nor the write can take the run down: both are caught per paper and counted in `failed`, because a storage error escaping this loop discarded a report describing rows already committed, and only after the pool's exit had waited out every analysis still in flight (several external requests each). `on_progress` fires once per finished paper whatever the outcome, so its count reaches `total`
- `list_results(config, *, limit) → list[dict]` — read path for `bmnews transparency --list`
- `TransparencyReport` — dataclass: `candidates`, `analyzed`, `indeterminate` (subset of `analyzed`), `exhausted` (subset of `indeterminate`, now at the attempt ceiling), `failed` (the analysis raised, or it succeeded and the write raised — either way no row was written, so the paper stays queued)

**Informs only.** Nothing here filters a query or re-ranks a paper; bmlib's `tier_downgrade_applied` is stored in `result_json` and never read back into a score.

## `bmnews/notify/`

Watch-based alerts: named criteria that fire on a matching paper as it is scored, separately from the periodic digest. A notified paper is still included in the next digest.

### `watches.py`

`Watch` and `Channel` dataclasses parsed from the config dicts, and the validating boundary between TOML and the rest of the stage. An unknown key is warned about by name; a value that cannot mean anything raises `WatchConfigError` and that one watch is skipped rather than taking the run down. A channel name repeated in one watch is dropped with a warning — delivering to the same destination twice in a run is never what it meant.

### `matcher.py`

Pure `(paper, watch) -> bool`. No I/O, no LLM, so the criteria engine tests against literal dicts. Criteria are AND-combined; an empty list means "no constraint"; within one list criterion the test is `any`.

### `service.py`

`run_notify()` — select, page, dispatch, record — plus `collect_matches()` and `pending_counts()`.

The rule that shapes it: **the delivery cap must not become a SQL `LIMIT`.** SQL narrows on what is indexable (score floors, tier exclusion, the not-already-sent anti-join, the ordering); the matcher applies keywords, tags, sources, journal and study design in Python afterwards. `collect_matches()` scans in `NOTIFY_SCAN_CHUNK`-sized chunks until one comes back short and returns *every* pending match; `_deliver()` slices the batch off that.

### `renderer.py`

Renders a batch into the `notify_*` templates.

### `channels/`

Delivery adapters, dispatched by a channel's `kind`. `email.py` wraps `digest/sender.py` over the `[email]` SMTP settings; `matrix.py` is one authenticated HTTP PUT with no SDK.

Adapters raise `ChannelError` rather than returning a boolean — `send_email` returns `False` on failure, and a `False` read as success would mark papers sent and drop them out of the derived queue with nobody having been told. `ChannelError` is the **only** exception `run_notify()` treats as "this delivery did not happen", so every transport failure has to be converted into one.

## `bmnews/digest/`

Digest rendering and email delivery.

### `renderer.py`

- `render_digest(papers, template_engine, subject_prefix, fmt)` — renders papers list through Jinja2
- Selects `digest_email.html` for HTML or `digest_text.txt` for text
- Template variables: `papers`, `paper_count`, `subject_prefix`, `generated_at`

### `sender.py`

- `send_email(html_body, text_body, subject, from_address, to_address, ...)` — sends multipart email via SMTP
- Creates `MIMEMultipart("alternative")` with both text and HTML parts
- Supports STARTTLS
- Returns `True` on success, `False` on failure (logged, not raised)

## `bmnews/db/`

Database schema and operations. See [Database](database.md) for full details.

### `schema.py`

- `init_db(conn)` — applies pending migrations via `bmlib.db.run_migrations`; idempotent, called on every connection open
- `open_db(config)` — returns a DB-API connection based on config

No DDL lives here.

### `migrations.py`

The seven versioned migrations, each with a pair of DDL strings (SQLite / PostgreSQL). Migration 4 moved paper storage onto `bmlib.publications` and dropped bmnews's `papers` table; it is destructive and one-way. Migration 7 adds `transparency`, keyed on `paper_id` alone since there is exactly one result per paper.

### `operations.py`

Pure-function CRUD. Every function takes `conn` as the first argument, writes are keyword-only, and `_row_to_paper()` is the single place a row becomes a paper dict. See [Database](database.md) for the full operation reference.

## `bmnews/gui/`

The desktop GUI: pywebview supplies the native window, Flask the HTTP backend, HTMX the partial-page updates. `app.py` is the Flask factory and registers four blueprints (`papers`, `settings`, `pipeline`, `watches`); `jobs.py` owns the single lock, status dict and daemon thread that a pipeline run and a notification delivery both go through, so one is refused rather than raced while the other is busy.

It is documented in `bmnews/gui/CLAUDE.md`, which loads automatically when you work under `bmnews/gui/`, rather than being repeated here.
