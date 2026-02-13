# Contributing Guide

## Development setup

```bash
git clone https://github.com/hherb/BioMedicalNews.git
cd BioMedicalNews
pip install -e ".[dev]"
pytest
```

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
def upsert_paper(conn: Any, *, doi: str, title: str, ...) -> int:
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

Adding a new paper source (e.g., PubMed, Semantic Scholar):

### 1. Create the fetcher module

Create `bmnews/fetchers/newsource.py`:

```python
"""Fetcher for NewSource.

Uses the NewSource API: https://api.newsource.org/
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from bmnews.fetchers.base import FetchedPaper

logger = logging.getLogger(__name__)

API_URL = "https://api.newsource.org/search"


def fetch_newsource(
    lookback_days: int = 7,
    timeout: float = 30.0,
) -> list[FetchedPaper]:
    """Fetch recent papers from NewSource."""
    end = date.today()
    start = end - timedelta(days=lookback_days)

    papers: list[FetchedPaper] = []

    with httpx.Client(timeout=timeout) as client:
        # Implement API call and pagination
        resp = client.get(API_URL, params={...})
        resp.raise_for_status()

        for item in resp.json()["results"]:
            paper = FetchedPaper(
                doi=item["doi"],
                title=item["title"],
                authors=item["authors"],
                abstract=item["abstract"],
                url=f"https://doi.org/{item['doi']}",
                source="newsource",
                published_date=item["date"],
                categories=item.get("subject", ""),
                metadata={...},  # Source-specific fields
            )
            papers.append(paper)

    logger.info("Fetched %d papers from NewSource", len(papers))
    return papers
```

### 2. Register in `__init__.py`

```python
# bmnews/fetchers/__init__.py
from bmnews.fetchers.newsource import fetch_newsource

__all__ = [..., "fetch_newsource"]
```

### 3. Add config toggle

In `config.py`, add to `SourcesConfig`:

```python
@dataclass
class SourcesConfig:
    ...
    newsource: bool = False
```

Add to `DEFAULT_CONFIG_TOML`:

```toml
[sources]
...
newsource = false
```

### 4. Wire into the pipeline

In `pipeline.py`, add to `run_fetch()`:

```python
if config.sources.newsource:
    logger.info("Fetching from NewSource...")
    papers.extend(fetch_newsource(lookback_days=lookback))
```

### 5. Add tests

In `tests/test_fetchers.py`, mock the HTTP call and verify `FetchedPaper` output.

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
4. Run tests: `pytest`
5. Run lint: `ruff check bmnews/ tests/`
6. Push and open a PR against `main`

PRs should include:
- Tests for new functionality
- Updated documentation if user-facing behavior changes
- A clear description of what changed and why
