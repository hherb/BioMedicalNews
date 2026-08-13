# bmlib Integration

bmnews delegates shared infrastructure to [bmlib](https://github.com/hherb/bmlib), a companion library providing LLM abstraction, database utilities, template rendering, quality assessment, and more.

This guide covers which bmlib modules bmnews uses, how they're integrated, and how to extend both projects together.

## Dependency relationship

bmlib is installed as a Git dependency:

```toml
# pyproject.toml
dependencies = [
    "bmlib @ git+https://github.com/hherb/bmlib.git@v0.9.1",
]
```

Optional dependency groups pull in bmlib extras:

```toml
[project.optional-dependencies]
anthropic = ["bmlib[anthropic]"]
ollama = ["bmlib[ollama]"]
postgresql = ["bmlib[postgresql]"]
transparency = ["bmlib[transparency]"]
gui = ["pywebview>=5.0", "flask>=3.0"]
```

**The version is pinned to a released tag, and that is the only pin the repository has.** `uv.lock` is gitignored, so an unpinned git dependency would be resolved afresh per machine and on every CI run — no two checkouts necessarily on the same bmlib, and a push to bmlib able to break bmnews with no change here. Bumping bmlib is therefore an edit to `pyproject.toml`, reviewable as its own one-line pull request:

```bash
# 1. edit pyproject.toml: @v0.9.1 -> @v0.10.0
uv lock --upgrade-package bmlib   # 2. re-resolve the local lock to the new tag
uv run pytest tests/ -q           # 3. the suite is what says the bump is safe
```

A green suite says the bump is *API*-safe. It cannot say the bump is
value-neutral, and the v0.6.0 → v0.9.1 jump was not: see
"[What a bmlib bump can move](#what-a-bmlib-bump-can-move)" below for the three
changes that moved stored values, none of which broke a test.

> **`uv run` re-syncs bmlib to whatever `uv.lock` resolved the tag to** — so installing a newer bmlib by hand is silently undone on the next `uv run`. When bmnews starts using a bmlib symbol the pinned tag predates, the whole suite fails at import; the fix is to move the pin above, not to install around it.

## What a bmlib bump can move

A bmlib release can change what bmnews *stores* without changing a signature
bmnews calls, and the suite cannot see it: every fetcher and analyzer is
mocked, so a test asserts what bmnews does with a value, never what bmlib now
produces. Read the release's CHANGELOG for "moves stored values" before
bumping — bmlib marks each such entry explicitly.

The v0.6.0 → v0.9.1 jump had three, and each needed a response here:

| What moved | Why it matters to bmnews | Response |
|---|---|---|
| **PubMed titles and abstracts became Markdown** (0.8.0) — `**LABEL:** text` sections, `*em*` / `~sub~` / `^sup^`, and prose backslash-escaped over ``\ ` * ~ ^`` | The reading pane's formatter read HTML, so the markers rendered raw: literal `**BACKGROUND:**`, `CO~2~`, `\*1`. Titles were also being *truncated* at the first inline tag before this, so stored titles predating 0.8.0 may be short | `bmnews/markup.py` — abstracts become tags in the reading pane, titles are flattened to text in `_row_to_paper()`. **Truncated titles are not repaired** (issue #35) |
| **`transparency_score` rises** (0.7.0) — structured `<DataBankList>` deposition now scores, and three trial registries PubMed emits were unrecognised | A paper already holding a determinate result is never re-selected by `get_transparency_candidates()`, so old rows keep their old score for ever and the corpus silently splits into two populations | `bmnews transparency --refresh`, which walks the corpus by `analyzed_at ASC NULLS FIRST` rather than redoing one batch. It is a manual step nothing prompts for — issue #37 |
| **Europe PMC free PDFs** (0.9.1) — the allow-list recognised only `"Free"`, which is 4.3% of entries; 95.7% read `"Open access"` | The reading pane now gets extracted text and a **View PDF** button where it used to get a bare link. Outbound traffic to Europe PMC rises accordingly | Nothing in this bump — but text cached *before* it stays frozen as a bare link, with no re-fetch path (issue #38) |

The general shape: **the pin is API-compatible far more often than it is
value-compatible.** A bump whose suite passes first time is the normal case,
not evidence that nothing changed.

### Re-syncing does not repair a stored value

Worth knowing before reaching for it as a remedy, because it looks like one and
reports success. Two independent things stop it, and neither warns:

1. **The days are never re-fetched.** `run_sync()` calls `bmlib.publications.sync()`
   without `recheck_days`, which defaults to `0` — a day recorded `completed` in
   `download_days` is skipped. `bmnews fetch` exposes only `--days`; no force or
   recheck flag exists anywhere in the CLI.
2. **The merge discards the incoming value even if it were re-fetched.** bmlib's
   `_merge_publication()` never overwrites an existing non-NULL field: `title` is
   absent from its `UPDATE` entirely, and `abstract` is written as
   `COALESCE(abstract, ?)`.

So the run prints `Sync complete: 0 added, N merged, 0 failed` — which reads as
success — and nothing was repaired. Repairing stored prose needs a migration on
the pattern of migration 6, which cleared stale full text *and* purged bmlib's
disk cache rather than trusting a re-fetch. That is issue #35.

## bmlib modules used by bmnews

### `bmlib.llm` — LLM provider abstraction

**Used in:** `pipeline.py` (client creation), `scoring/relevance_agent.py` (via BaseAgent)

`LLMClient` provides a unified interface to multiple LLM providers:

```python
from bmlib.llm import LLMClient

# Created in pipeline.build_llm_client()
llm = LLMClient(
    default_provider="ollama",
    ollama_host="http://localhost:11434",
)
```

**Key concepts:**
- Model strings use `"provider:model_name"` format (e.g., `"ollama:llama3.1"`, `"anthropic:claude-sonnet-4-5-20250929"`)
- The client routes requests to the correct provider
- Tracks token usage and costs
- Supports JSON mode for structured responses

bmnews doesn't call `LLMClient` directly for scoring — it goes through `BaseAgent`. The client is constructed in `build_llm_client()` and passed to the scoring layer.

### `bmlib.db` — Database abstraction

**Used in:** `db/schema.py`, `db/operations.py`, `db/migrations.py`, `pipeline.py`, `cli.py`

Pure functions over DB-API connections:

```python
from bmlib.db import (
    connect_sqlite,       # Used in open_db() for SQLite backend
    connect_postgresql,   # Used in open_db() for PostgreSQL backend
    execute,              # Used in all write operations
    fetch_one,            # Used in get_paper_by_doi()
    fetch_all,            # Used in queries returning multiple rows
    fetch_scalar,         # Used in paper_exists()
    transaction,          # Used as context manager for atomic writes; nests
    placeholder,          # "?" or "%s" for the connection's backend
    is_sqlite,            # Selects between paired DDL / SQL variants
    Migration,            # One versioned schema step
    run_migrations,       # Applies the pending ones — what init_db() calls
    create_tables,        # Applies one migration's DDL string
)
```

**Pattern:** Every operation in `db/operations.py` takes a `conn` parameter. bmlib handles the actual SQL execution, cursor management, and transaction boundaries.

```python
# Example from operations.py
def get_paper(conn, paper_id):
    ph = _placeholder(conn)
    row = fetch_one(
        conn,
        f"SELECT {_PAPER_COLUMNS} {_PAPER_FROM} WHERE p.id = {ph}",
        (paper_id,),
    )
    return _row_to_paper(row) if row else None
```

`transaction()` **nests**: an inner block joins the outer one rather than committing under it, which is what lets `store_paper()` wrap a `store_publication()` call that opens its own. Nesting is counted by bmlib rather than read from psycopg2's transaction status — psycopg2 opens a transaction on the first statement of any kind, so a bare `SELECT` leaves the connection `INTRANS` and status-based detection would classify ordinary blocks as nested and silently stop committing.

### `bmlib.publications` — Paper storage and the source registry

**Used in:** `pipeline.py` (sync), `db/operations.py` (store, lookup), `db/migrations.py` (migration 4), `fetchers/__init__.py` (registration)

This is where papers live. bmnews has no `papers` table of its own — migration 4 moved storage here.

```python
from bmlib.publications import (
    sync,                    # The whole fetch-and-store cycle, one call
    ensure_schema,           # publications, fulltext_sources, download_days
    store_publication,       # Dedupes on normalised DOI, then PMID
    get_publication_by_doi, get_publication_by_pmid,
    register_source, source_names,   # The registry every source goes through
    FetchedRecord, FetchResult, SourceDescriptor, SyncProgress, SyncReport,
)
```

**`sync()` owns the fetch loop.** It walks the lookback window, skips days already recorded complete in `download_days`, stores each day in one transaction (the write lock is not held across network I/O), and deduplicates records by DOI *and* PMID. `pipeline._progress_reporter()` renders `SyncProgress` down to bmnews's `on_progress(str)` callback.

**The registry is the only dispatch path.** A fetcher matches this convention:

```python
def fetch_x(client, target_date, *, on_record, on_progress=None, **config) -> FetchResult:
```

medRxiv, bioRxiv, PubMed and OpenAlex ship with bmlib; bmnews registers Europe PMC into the same registry from `bmnews/fetchers/__init__.py`. Adding a source anywhere it is registered makes it selectable by name in `config.sources.enabled` with no further bmnews changes.

`FetchedRecord.publication_types` matters: it feeds bmlib's free Tier-1 quality classification, and dropping it silently forces every paper onto the LLM classifier.

### `bmlib.fulltext` — Full-text retrieval

**Used in:** `gui/routes/papers.py`

`FullTextService` retrieves full text on demand through three tiers — Europe PMC, then Unpaywall, then the DOI — parses JATS XML, and raises `FullTextError` when nothing can be had. It keeps a **disk cache that is consulted before the database**, which is why migration 6 had to delete cache files as well as clear the rows: clearing the row alone would have the next request served the same stale file.

The retrieved body is cached in bmnews's `paper_extras`, not in bmlib's `fulltext_sources` — that table records *where* full text lives, not the fetched text.

### `bmlib.templates` — Jinja2 template engine

**Used in:** `pipeline.py` (engine creation), `scoring/relevance_agent.py` (via BaseAgent), `digest/renderer.py`

```python
from bmlib.templates import TemplateEngine

# Created in pipeline.build_template_engine()
engine = TemplateEngine(
    user_dir=Path("~/.bmnews/templates"),   # User overrides (optional)
    default_dir=Path("templates/"),          # Built-in defaults
)
```

**Resolution order:** user directory first, then default directory. This lets users override any template without modifying the package.

The engine is used directly in `render_digest()` and indirectly through `BaseAgent.render_template()` in the scoring agent.

### `bmlib.agents` — Base agent class

**Used in:** `scoring/relevance_agent.py`

`BaseAgent` provides the scaffolding for LLM-powered agents:

```python
from bmlib.agents.base import BaseAgent

class RelevanceAgent(BaseAgent):
    def score(self, title, abstract, interests, categories):
        prompt = self.render_template("relevance_scoring.txt", ...)
        system = self.render_template("relevance_system.txt")
        response = self.chat(
            [self.system_msg(system), self.user_msg(prompt)],
            json_mode=True,
        )
        result = self.parse_json(response.content)
        return result
```

**What BaseAgent provides:**
- `render_template(name, **kwargs)` — renders a Jinja2 template via the engine
- `system_msg(content)` / `user_msg(content)` / `assistant_msg(content)` — creates `LLMMessage` objects
- `chat(messages, json_mode=False)` — sends messages to the LLM and returns an `LLMResponse`
- `parse_json(text)` — extracts JSON from LLM output, handling markdown code blocks

**Constructor:** `BaseAgent(llm, model, template_engine)` — receives all dependencies from the outside, nothing is hardcoded.

### `bmlib.quality` — Quality assessment pipeline

**Used in:** `scoring/scorer.py`

Quality assessment goes through `QualityManager`, which escalates through the tiers up to the ceiling a `QualityFilter` sets:

```python
from bmlib.quality import QualityAssessment, QualityFilter, QualityManager, QualityTier

manager = QualityManager(
    llm=llm, classifier_model=model, assessor_model=model, template_engine=engine
)
# _build_quality_filter() clamps how deep the assessment may go:
#   1 = metadata only (free), 2 = LLM classifier, 3 = deep analysis
assessment = manager.assess(
    title=title,
    abstract=abstract,
    publication_types=_extract_pub_types(paper),
    filter_settings=_build_quality_filter(max_tier),
)
# QualityAssessment carries:
#   .study_design (StudyDesign enum)
#   .quality_tier (QualityTier enum)
#   .quality_score (float)
```

The ceiling comes from config: `quality.default_tier` clamped by `quality.max_tier`. When `quality.enabled` is false the stage is skipped entirely and the combined score is the relevance score alone.

**Quality data models:**
- `StudyDesign` — the study-design vocabulary; scores store the **value** spelling (`"rct"`, not `"RCT"`)
- `QualityTier` — the tiers, from `TIER_1_ANECDOTAL` upward, plus `UNCLASSIFIED`
- `QualityAssessment` — dataclass with design, tier, score, bias risk, strengths, limitations
- `DESIGN_TO_TIER` / `DESIGN_TO_SCORE` — the evidence hierarchy. It lives here, **not** in `bmnews.constants`

`UNCLASSIFIED` papers are never excluded by a tier floor — unjudged is not judged-and-rejected. `scoring.scorer.tiers_below()` is the one place that rule is implemented, and the notification matcher reuses it so the digest and watches agree.

### `bmlib.transparency` — Research-integrity analysis

**Used in:** `bmnews/transparency/service.py` (the stage), `db/operations.py` (storage and the read path), `pipeline.py` (placement), `cli.py` (`bmnews transparency`)

A fifth pipeline stage between SCORE and NOTIFY. `bmnews.transparency.service.run_transparency()` selects scored papers above the `transparency.min_combined_score` cost gate, hands each to a single shared `TransparencyAnalyzer`, and stores whatever comes back — it **informs only**, never filters or re-ranks.

```python
from bmlib.transparency import TransparencyAnalyzer, TransparencyRisk, TransparencySettings

analyzer = TransparencyAnalyzer(
    email=config.user.email or DEFAULT_CONTACT_EMAIL,
    pubmed_api_key=...,   # reused from sources.source_options.pubmed, not duplicated in config
    settings=TransparencySettings(
        enabled=True,   # always True here — see build_settings()'s docstring for why
        score_threshold=config.transparency.score_threshold,
        max_concurrent_analyses=config.transparency.concurrency,
    ),
)
result = analyzer.analyze(str(paper["id"]), pmid=paper.get("pmid"), doi=paper.get("doi"))
```

**The settings mapping is smaller than bmlib's settings object.** `TransparencySettings` also carries `industry_funding_triggers_downgrade` and `missing_coi_triggers_downgrade` (both default `True`) and `tier_downgrade_amount`; `build_settings()` in `bmnews/transparency/service.py` leaves all three at bmlib's defaults rather than exposing them in `[transparency]`. The two `*_triggers_downgrade` flags matter regardless of the tier downgrade being unused here — they also shape `calculate_risk_level()`, which is what makes an industry-funded paper with restricted data read HIGH instead of MEDIUM. `filtering_enabled` stays `False` because this caller does not filter, and the settings object should not claim otherwise.

**`tier_downgrade_applied` is stored, never applied.** bmlib's result carries a flag saying a paper's quality tier *would* be downgraded for transparency reasons. `result_json` (the whole `TransparencyResult.to_dict()`) keeps it, but nothing in bmnews reads it back into a score — a value derived from five external APIs must not be able to move a `combined_score` the user has already acted on. Filtering and the downgrade are both additive later, not oversights now.

**The retry ceiling is the one thing not to undo.** bmlib's reachability flag is set only on an HTTP 200, so `UNREACHABLE` covers both a network outage and a paper indexed in none of the five APIs — the two cannot be told apart. `transparency.attempts` bounds automatic retries at `TRANSPARENCY_MAX_ATTEMPTS` (3); `bmnews transparency --refresh` resets it to 1. Removing the ceiling means re-querying every unindexed preprint, four to eight requests each, on every run, forever.

**The bmlib pin (0.5.1) is untouched.** The design deliberately avoids `TransparencyUnknownReason`, which that version does not export — `parse_transparency()` in `bmnews/metadata.py` decodes `result_json` as a plain dict rather than through bmlib's `TransparencyResult.from_dict()` for the same reason: that classmethod raises on a member it does not recognise. Bumping to bmlib 0.6.0 needs no bmnews code change; `unknown_reason` then starts appearing inside the stored JSON on its own.

Install with `uv pip install -e ".[transparency]"` — though note the extra is vestigial: it resolves to `bmlib[transparency]`, which is `httpx>=0.25`, already a core bmnews dependency. It installs nothing new; it exists to name the feature, not to gate a real dependency.

## Extending bmnews with bmlib

### Adding a new agent

To create a new agent (e.g., for deeper paper analysis):

1. Create a new module in `bmnews/scoring/`:

```python
from bmlib.agents.base import BaseAgent

class AnalysisAgent(BaseAgent):
    def analyze(self, title, abstract):
        prompt = self.render_template("analysis_prompt.txt", title=title, abstract=abstract)
        system = self.render_template("analysis_system.txt")
        response = self.chat([self.system_msg(system), self.user_msg(prompt)])
        return self.parse_json(response.content)
```

2. Add templates in `templates/`
3. Wire it into `scorer.py` or `pipeline.py`

### Adding a new fetcher source

Prefer adding it to **bmlib's** registry — bmnews then picks it up with nothing but a config change. If it must live here, follow the Europe PMC pattern and do **not** add a second dispatch path in `pipeline.run_sync()`:

1. Write `bmnews/fetchers/newsource.py` matching the registry convention — `fetcher(client, target_date, *, on_record, on_progress=None, **config)`, emitting `FetchedRecord` and returning a `FetchResult`
2. Add a `SourceDescriptor` and a `register_source(...)` call to `register_local_sources()` in `bmnews/fetchers/__init__.py`
3. Add tests with a fake HTTP client in `tests/test_fetchers.py`

### Adding a new database utility

If you need a new database operation:

1. Add the function to `bmnews/db/operations.py` following the existing pattern
2. Use `bmlib.db` functions (`execute`, `fetch_all`, etc.) for execution
3. Use `placeholder(conn)` and `is_sqlite(conn)` for backend-aware SQL
4. Add tests in `tests/test_db.py` — they run against both backends

### Changing a bmlib symbol bmnews depends on

If the operation needs something bmlib does not expose yet, upstream it rather than reaching around it. Backend-aware SQL in `bmlib.publications` was added this way (hherb/bmlib#28) instead of dropping PostgreSQL support from bmnews.

### Developing bmlib alongside bmnews

For local development of both projects:

```bash
# Clone both
git clone https://github.com/hherb/bmlib.git
git clone https://github.com/hherb/BioMedicalNews.git

# Install bmlib in editable mode
cd bmlib && uv pip install -e ".[dev]"

# Install bmnews (it will use the local bmlib)
cd ../BioMedicalNews && uv pip install -e ".[dev]"
```

Changes to bmlib are then reflected in bmnews without reinstalling — but note the caveat above: `uv run` re-syncs bmlib to the commit `uv.lock` resolved the pinned tag to, undoing an editable install. Work through `uv pip install -e` alone while developing both, and move the pin in `pyproject.toml` once the bmlib change is released.

Always use `uv` to install or upgrade packages in this project; do not call `pip` directly.
