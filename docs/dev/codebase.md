# Codebase Guide

Module-by-module walkthrough of the bmnews source code.

## `bmnews/__init__.py`

Package root. Defines `__version__`.

## `bmnews/cli.py`

CLI entry point using Click. Defines the `main` group and all subcommands.

**Key patterns:**
- `@click.group()` on `main()` with global options (`--config`, `--verbose`, `--version`)
- Config is loaded in `main()` and stored in `ctx.obj["config"]` for subcommands
- Lazy imports: each command imports pipeline functions inside the function body, keeping startup fast
- Each command is thin — it calls pipeline functions and reports results

**Commands:**
- `run(days, show_cached)` — full pipeline or cached display
- `fetch(days)` — fetch and store only
- `score()` — score unscored papers
- `digest(output)` — render and deliver
- `init(config_path)` — first-time setup
- `search(query)` — keyword search with direct SQL

## `bmnews/config.py`

TOML configuration loading with typed dataclass access.

**Key components:**
- `DEFAULT_CONFIG_DIR` / `DEFAULT_CONFIG_PATH` — `~/.bmnews/config.toml`
- Section dataclasses: `DatabaseConfig`, `SourcesConfig`, `LLMConfig`, `ScoringConfig`, `QualityConfig`, `TransparencyConfig`, `UserConfig`, `EmailConfig`
- `AppConfig` — top-level dataclass aggregating all sections plus `log_level` and `template_dir`
- `load_config(path)` — loads TOML, applies values onto dataclass defaults, ignores unknown keys
- `write_default_config(path)` — writes `DEFAULT_CONFIG_TOML` if file doesn't exist
- `_apply_section(dc, data)` — maps dict keys to dataclass attributes

**Design notes:**
- Uses `tomllib` (stdlib since Python 3.11) for TOML parsing
- Unknown config keys are silently ignored (forward compatibility)
- All fields have defaults, so the app works even with an empty config

## `bmnews/pipeline.py`

Central orchestration module. Contains the main pipeline functions and builder helpers.

**Builder functions:**
- `build_template_engine(config)` — creates `TemplateEngine` with user dir and package defaults
- `build_llm_client(config)` — creates `LLMClient` from provider/host/key settings

**Pipeline stages:**
- `run_fetch(config)` → `list[FetchedPaper]` — calls enabled fetchers
- `run_store(config, papers)` → `int` — upserts papers into DB
- `run_score(config)` → `int` — scores unscored papers with LLM + quality
- `run_digest(config, output)` → `str` — renders and delivers digest
- `show_cached_digests(config, days)` → `str` — re-renders previous digest papers
- `run_pipeline(config, days, show_cached)` — orchestrates all stages

**Design notes:**
- Each `run_*` function opens and closes its own DB connection
- `run_pipeline` short-circuits to `show_cached_digests` when `show_cached=True`
- The `days` parameter overrides `config.sources.lookback_days` at runtime

## `bmnews/fetchers/`

Source-specific API clients that return normalized `FetchedPaper` objects.

### `base.py`

Defines the `FetchedPaper` dataclass — the normalized representation shared across all fetchers:

```python
@dataclass
class FetchedPaper:
    doi: str           # DOI or "pmid:12345" identifier
    title: str
    authors: str       # Semicolon-separated
    abstract: str
    url: str           # DOI link or direct URL
    source: str        # "medrxiv", "biorxiv", "europepmc"
    published_date: str
    categories: str    # Semicolon-separated
    metadata: dict     # Source-specific metadata
```

### `medrxiv.py`

Fetches from the medRxiv/bioRxiv public API.

- **API:** `https://api.medrxiv.org/details/{server}/{start_date}/{end_date}/{cursor}`
- **Pagination:** cursor-based, 100 results per page
- `fetch_medrxiv(lookback_days)` and `fetch_biorxiv(lookback_days)` are thin wrappers around `_fetch_rxiv(server, ...)`
- Metadata captured: version, type, category, JATS XML path

### `europepmc.py`

Fetches from the Europe PMC REST API.

- **API:** `https://www.ebi.ac.uk/europepmc/webservices/rest/search`
- **Pagination:** cursor-mark based
- Default query: `SRC:PPR` (preprints only) filtered by date range
- Custom query: wraps user query with date filter
- Falls back to PMID when DOI is unavailable (`doi="pmid:12345"`)
- Metadata captured: pmid, pmcid, source, pub_type list, journal, cited_by count, open access status

## `bmnews/scoring/`

LLM-based relevance scoring and quality assessment.

### `relevance_agent.py`

`RelevanceAgent` extends `bmlib.agents.BaseAgent` to score papers for relevance.

- Renders `relevance_system.txt` (system prompt) and `relevance_scoring.txt` (user prompt with paper data)
- Calls LLM in JSON mode
- Parses response with `BaseAgent.parse_json()` (handles markdown code blocks)
- Returns dict with `relevance_score`, `summary`, `relevance_rationale`, `key_findings`
- Clamps score to 0.0–1.0
- Falls back to score 0.0 on parse failure

### `scorer.py`

Orchestrates scoring for a batch of papers.

- `score_papers(papers, llm, model, template_engine, interests, concurrency, quality_tier)` — main entry point
- `_score_single(paper, agent, interests, quality_tier)` — scores one paper:
  1. Calls `RelevanceAgent.score()` for relevance + summary
  2. Calls `_assess_quality()` for quality assessment
  3. Computes combined score: `0.6 * relevance + 0.4 * quality`
- `_assess_quality(paper, max_tier)` — runs `classify_from_metadata()` from bmlib
- `_extract_pub_types(paper)` — extracts publication types from `metadata_json` and `categories`
- `_quality_tier_to_score(assessment)` — maps `QualityAssessment` to 0.0–1.0 score

**Concurrency:** Uses `ThreadPoolExecutor` when `concurrency > 1`. Errors in individual papers are logged but don't stop the batch.

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

- `SCHEMA_SQLITE` / `SCHEMA_POSTGRESQL` — DDL strings for all tables
- `init_db(conn)` — creates tables if they don't exist
- `open_db(config)` — returns a DB-API connection based on config

### `operations.py`

Pure-function CRUD operations. Every function takes `conn` as the first argument. See [Database](database.md) for the full operation reference.
