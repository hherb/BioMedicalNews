# Testing Guide

## Running tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_db.py

# Run a specific test class or method
pytest tests/test_db.py::TestPapers::test_upsert_and_retrieve

# Run with coverage
pytest --cov=bmnews
```

## Test structure

```
tests/
  __init__.py
  test_config.py      # Config loading, defaults, TOML parsing
  test_db.py          # Database schema, CRUD operations, digest tracking
  test_fetchers.py    # Fetcher data normalization
  test_scoring.py     # Quality tier scoring, pub type extraction
  test_digest.py      # Digest rendering
  test_pipeline.py    # Pipeline integration, CLI commands, show_cached
```

## Test patterns

### In-memory SQLite database

All database tests use in-memory SQLite to avoid file I/O and ensure isolation:

```python
from bmlib.db import connect_sqlite
from bmnews.db.schema import init_db

def _db():
    conn = connect_sqlite(":memory:")
    init_db(conn)
    return conn
```

Each test creates a fresh database. No cleanup needed.

### Seeded database

For tests that need pre-populated data (e.g., testing digest rendering or cached paper retrieval):

```python
def _seeded_db():
    conn = connect_sqlite(":memory:")
    init_db(conn)
    pid = upsert_paper(conn, doi="10.1101/test", title="Test Paper",
                       abstract="Abstract", published_date="2026-02-10",
                       source="medrxiv")
    save_score(conn, paper_id=pid, combined_score=0.8, relevance_score=0.9,
               quality_score=0.7, summary="Great paper.")
    record_digest(conn, [pid], delivery_method="stdout")
    return conn
```

### Mocking external dependencies

Pipeline tests mock `open_db` to inject an in-memory database, avoiding filesystem access:

```python
from unittest.mock import patch

class TestShowCachedDigests:
    @patch("bmnews.pipeline.open_db")
    def test_renders_cached_papers(self, mock_open_db):
        mock_open_db.return_value = _seeded_db()
        config = _test_config()
        text = show_cached_digests(config)
        assert "Test Paper" in text
```

CLI tests use Click's `CliRunner` for end-to-end command testing:

```python
from click.testing import CliRunner
from bmnews.cli import main

class TestRunCLI:
    @patch("bmnews.pipeline.open_db")
    def test_run_show_cached(self, mock_open_db):
        mock_open_db.return_value = _seeded_db()
        runner = CliRunner()
        result = runner.invoke(main, ["run", "--show_cached"])
        assert result.exit_code == 0
        assert "Test Paper" in result.output
```

### Testing without LLM calls

Scoring tests focus on the non-LLM parts — quality tier mapping and metadata extraction — to avoid needing a running LLM:

```python
from bmlib.quality.data_models import QualityAssessment, StudyDesign
from bmnews.scoring.scorer import _quality_tier_to_score

class TestQualityTierToScore:
    def test_rct(self):
        a = QualityAssessment.from_metadata(StudyDesign.RCT)
        score = _quality_tier_to_score(a)
        assert score == 0.8
```

For tests that do need LLM interaction (integration tests), mock the LLM client or use a test fixture that returns canned responses.

### Test config helper

Pipeline tests use a helper that returns a config pointing at an in-memory database:

```python
def _test_config():
    config = load_config(None)  # Load defaults
    config.database.backend = "sqlite"
    config.database.sqlite_path = ":memory:"
    return config
```

## What to mock

| Component | Mock when | Don't mock when |
|-----------|-----------|-----------------|
| Database (`open_db`) | Testing pipeline/CLI logic | Testing DB operations directly |
| LLM calls | Testing scoring orchestration | Integration tests with a live LLM |
| HTTP APIs (httpx) | Testing fetcher parsing | Integration tests with live APIs |
| SMTP (smtplib) | Testing email delivery flow | Never (always mock) |
| Filesystem | Testing config loading | Testing with real temp files |

## Writing tests for new features

### New database operation

1. Add test class in `tests/test_db.py`
2. Use `_db()` helper for a clean database
3. Test both the happy path and edge cases

```python
class TestNewOperation:
    def test_basic_case(self):
        conn = _db()
        # Set up data
        pid = upsert_paper(conn, doi="10.1101/x", title="X")
        # Call your operation
        result = your_new_operation(conn, pid)
        # Assert
        assert result == expected

    def test_empty_case(self):
        conn = _db()
        result = your_new_operation(conn, 999)
        assert result is None
```

### New fetcher

1. Add test in `tests/test_fetchers.py`
2. Mock `httpx.Client` to return canned API responses
3. Verify the `FetchedPaper` fields are correctly populated

### New scoring feature

1. Add test in `tests/test_scoring.py`
2. Test the scoring logic without LLM calls where possible
3. Mock `RelevanceAgent.score()` for integration tests

### New CLI command

1. Add test in `tests/test_pipeline.py`
2. Use `CliRunner` from Click
3. Mock database and external dependencies

```python
class TestNewCommand:
    @patch("bmnews.pipeline.open_db")
    def test_new_command(self, mock_open_db):
        mock_open_db.return_value = _seeded_db()
        runner = CliRunner()
        result = runner.invoke(main, ["new-command", "--flag"])
        assert result.exit_code == 0
```

## Running lint

```bash
ruff check bmnews/ tests/
ruff format --check bmnews/ tests/
```
