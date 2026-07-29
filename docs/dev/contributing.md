# Contributing Guide

## Development setup

```bash
git clone https://github.com/hherb/BioMedicalNews.git
cd BioMedicalNews
uv pip install -e ".[all]"
uv run pytest
```

Always use `uv` to install, upgrade or otherwise manipulate packages here — never `pip` directly.

## Code style

- **Formatter/linter:** ruff
- **Line length:** 100 characters
- **Python version:** 3.11+ (use modern syntax — `X | Y` unions, `tomllib`, etc.)
- **Lint rules:** E, F, I, N, W, UP (pycodestyle, pyflakes, isort, naming, warnings, pyupgrade)

Run lint:

```bash
ruff check bmnews/ tests/
ruff format --check bmnews/ tests/
```

Auto-fix:

```bash
ruff check --fix bmnews/ tests/
ruff format bmnews/ tests/
```

## Conventions

### Type hints

All public functions should have type hints:

```python
def store_paper(conn: Any, *, title: str, doi: str | None = None, ...) -> int:
```

Use `from __future__ import annotations` at the top of every module for PEP 604 union syntax (`X | Y`).

### Docstrings

All public functions and classes should have docstrings. Use Google-style:

```python
def score_papers(papers: list[dict], llm: LLMClient, ...) -> list[dict]:
    """Score a list of papers for relevance and quality.

    Args:
        papers: List of paper dicts (from db).
        llm: LLM client instance.

    Returns:
        List of dicts with scoring results.
    """
```

### Pure functions for DB operations

Database operations take a connection as the first argument and have no side effects beyond the database:

```python
# Good
def get_paper(conn: Any, doi: str) -> dict | None:
    ...

# Bad — don't open connections inside operations
def get_paper(doi: str) -> dict | None:
    conn = open_db(config)  # Don't do this
    ...
```

### Keyword-only arguments for writes

Use keyword-only args (after `*`) for functions that write data. This prevents positional argument mistakes:

```python
def save_score(conn: Any, *, paper_id: int, relevance_score: float, ...) -> None:
```

### Two database traps worth knowing

**Never rely on `cursor.lastrowid` after an upsert.** SQLite leaves it pointing at the last row actually *inserted* when `ON CONFLICT` takes the UPDATE path, so the id you get back may belong to a different paper. Look the row up by its natural key instead — `store_paper()` re-reads by normalised DOI/PMID for exactly this reason.

**Decode a paper row exactly once.** `_row_to_paper()` is the only place the JSON array columns become lists and the outbound `url` is derived. Callers, templates and the scorer all receive real lists; nothing re-parses JSON downstream.

### Closing connections

Use `contextlib.closing` so a raised exception cannot leak the handle:

```python
with closing(open_db(config)) as conn:
    ...
```

### No magic numbers

Fixed behavioural values live in `bmnews/constants.py`; anything a *user* should be able to tune belongs in `bmnews/config.py`. The evidence hierarchy and its scores live in `bmlib.quality`, not in either.

### Logging

Use module-level loggers:

```python
import logging
logger = logging.getLogger(__name__)
```

Use appropriate levels:
- `logger.debug()` — API URLs, SQL queries, detailed flow
- `logger.info()` — high-level progress ("Fetched 42 papers", "Pipeline complete")
- `logger.warning()` — recoverable issues (parse errors, fallbacks)
- `logger.error()` — operation failures that affect results
- `logger.exception()` — errors with stack trace

## License headers

This project is AGPL-3.0. New files should include a brief module docstring but do not need a full license header — the LICENSE file covers the entire repository.

## How to add a new fetcher source

**Every** source resolves through bmlib's registry — there is no second dispatch path in `pipeline.run_sync()`, and adding one would bypass the resume tracking, cross-source dedupe and per-day transaction that `sync()` provides.

The preferred home for a new source is **bmlib itself**. Once registered there, bmnews picks it up with no code change at all: add its name to `config.sources.enabled`.

If the source has to live in bmnews, follow the Europe PMC pattern:

### 1. Create the fetcher module

Create `bmnews/fetchers/newsource.py` with a function matching the registry calling convention — one target day per call, emitting records through `on_record` rather than returning a list:

```python
"""Fetcher for NewSource.

Uses the NewSource API: https://api.newsource.org/
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable

from bmlib.publications import FetchedRecord, FetchResult

logger = logging.getLogger(__name__)

API_URL = "https://api.newsource.org/search"


def fetch_newsource(
    client: Any,
    target_date: date,
    *,
    on_record: Callable[[FetchedRecord], None],
    on_progress: Callable[..., None] | None = None,
    **config: Any,
) -> FetchResult:
    """Fetch one day of papers from NewSource."""
    date_str = target_date.isoformat()
    count = 0

    for item in _pages(client, target_date, **config):
        on_record(
            FetchedRecord(
                title=item["title"],
                source="newsource",
                # Empty optionals are None, not "" — so a later merge from
                # another source can fill them in.
                doi=item.get("doi") or None,
                pmid=item.get("pmid") or None,
                abstract=item.get("abstract", ""),
                authors=item.get("authors", []),
                publication_date=item["date"],
                # Feeds bmlib's free Tier-1 quality classification — dropping
                # it forces every paper onto the LLM classifier instead.
                publication_types=item.get("types", []),
                extras={...},  # Source-specific fields with no column
            )
        )
        count += 1

    return FetchResult(
        source="newsource", date=date_str, record_count=count, status="completed"
    )
```

A day that fails should return `FetchResult(..., status="failed", error=str(exc))` rather than raising: that is what `download_days` records so the day is re-fetched next run instead of being silently lost.

### 2. Register it

In `bmnews/fetchers/__init__.py`, add a `SourceDescriptor` and a `register_source()` call inside `register_local_sources()`. Declare the options the fetcher accepts so they can be set per-source in config.

### 3. That's it for wiring

No `SourcesConfig` field, no pipeline branch. The source is now selectable by name in `config.sources.enabled`, appears in the GUI settings list, and takes keyword options from `config.sources.source_options.<name>`.

### 4. Add tests

In `tests/test_fetchers.py`, pass a fake HTTP client returning canned responses, verify the emitted `FetchedRecord` fields, and assert the source is present in bmlib's registry.

## How to add a new LLM provider

LLM providers are managed by bmlib, not bmnews. To add a new provider:

1. Implement the provider in `bmlib/llm/providers/`
2. Register it in `bmlib/llm/client.py`
3. Add the optional dependency to bmlib's `pyproject.toml`
4. bmnews will automatically support it through the `"provider:model"` string format

No changes needed in bmnews code — just update the config:

```toml
[llm]
provider = "newprovider"
model = "newprovider:model-name"
```

## Commit messages

Follow conventional-style messages. Examples:

```
feat: add PubMed fetcher source
fix: handle missing DOI in EuropePMC response
docs: update configuration reference
test: add scoring edge case tests
refactor: extract shared fetcher pagination logic
```

## Pull request workflow

1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes
4. Run tests: `uv run pytest`. If you touched `db/operations.py` or `db/migrations.py`, run the PostgreSQL half too — it skips silently without a DSN, and that is where the backend-specific SQL lives:
   `BMNEWS_TEST_PG_DSN=… uv run pytest tests/test_db.py`
5. Run lint: `uv run ruff check bmnews/ tests/` and `uv run ruff format --check bmnews/ tests/`
6. Push and open a PR against `main`

PRs should include:
- Tests for new functionality
- Updated documentation if user-facing behavior changes
- A clear description of what changed and why
