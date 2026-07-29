# bmlib Integration

bmnews delegates shared infrastructure to [bmlib](https://github.com/hherb/bmlib), a companion library providing LLM abstraction, database utilities, template rendering, quality assessment, and more.

This guide covers which bmlib modules bmnews uses, how they're integrated, and how to extend both projects together.

## Dependency relationship

bmlib is installed as a Git dependency:

```toml
# pyproject.toml
dependencies = [
    "bmlib @ git+https://github.com/hherb/bmlib.git",
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

> **`uv.lock` pins bmlib by commit, and `uv run` re-syncs to that pin** — so installing a newer bmlib by hand is silently undone on the next `uv run`. When bmnews starts using a bmlib symbol the pin predates, the whole suite fails at import. Move the pin with `uv lock --upgrade-package bmlib`.

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

### `bmlib.transparency` — Transparency analysis (not wired up)

**Declared but unused.** `bmnews.config` has a `[transparency]` section (`enabled`, `min_score_threshold`) and `pyproject.toml` declares a `transparency` extra, but no bmnews code calls the analyzer — setting `enabled = true` currently changes nothing. bmlib's analyzer queries CrossRef, Europe PMC, OpenAlex and ClinicalTrials.gov for publication-integrity data.

Wiring it up is the largest open item in bmnews; check `docs/plans/` and HANDOVER.md before starting. Install with `uv pip install -e ".[transparency]"`.

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

Changes to bmlib are then reflected in bmnews without reinstalling — but note the lock-file caveat above: `uv run` re-syncs bmlib to the commit `uv.lock` pins, undoing an editable install. Use `uv lock --upgrade-package bmlib` when the pin needs to move.

Always use `uv` to install or upgrade packages in this project; do not call `pip` directly.
