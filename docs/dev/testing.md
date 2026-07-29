# Testing Guide

## Running tests

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run a specific test file
uv run pytest tests/test_db.py

# Run a specific test class or method
uv run pytest tests/test_db.py::TestPapers::test_store_and_retrieve

# Run with coverage
uv run pytest --cov=bmnews
```

The database tests run **once per backend**. Without a DSN the PostgreSQL half skips (visibly, as a skip rather than a silent disappearance); point `BMNEWS_TEST_PG_DSN` at a live server to run it:

```bash
BMNEWS_TEST_PG_DSN=postgresql://bmnews:bmnews@localhost:5432/bmnews_test uv run pytest
uv run pytest -k postgresql        # just the PostgreSQL runs
```

The tests create and drop their own schemas, so give them a scratch database, not one with anything in it. CI runs this half from a `services: postgres` container.

## Test structure

```
tests/
  backends.py         # Not a test: per-backend parameterisation for test_db.py
  conftest.py         # Not a test: the db_backend fixture + the autouse GUI-jobs reset
  test_config.py      # Config loading, TOML parsing, backward-compat defaults
  test_db.py          # Every DB operation and migration — both backends
  test_digest.py      # HTML/text digest rendering
  test_fetchers.py    # Europe PMC fetcher + its registration in bmlib's registry
  test_fulltext_integration.py  # Fulltext service (Europe PMC/Unpaywall/DOI)
  test_gui_app.py     # Flask blueprints, HTMX responses, URL-scheme allowlist
  test_gui_helpers.py # Abstract HTML formatting
  test_gui_jobs.py    # The shared background job: refusal, lock release, cleanup
  test_gui_notify.py  # The watches pane
  test_notify.py      # Every watch criterion; watch/channel parsing and validation
  test_notify_channels.py  # Channel adapters and the four notify_* templates
  test_notify_service.py   # run_notify paging, dedup, retry, dry run, CLI
  test_pipeline.py    # run_sync storage, source dispatch, notify stage placement
  test_scoring.py     # Quality tier mapping, pub type extraction, tier floors
```

## Test patterns

### Per-backend databases

`test_db.py` opts every test in it into both backends:

```python
pytestmark = pytest.mark.usefixtures("db_backend")
```

Build databases with `tests.backends.new_db()` — never `connect_sqlite(":memory:")` directly, which would pin the test to SQLite and quietly skip the PostgreSQL SQL it was meant to cover. It returns an *unmigrated* connection on the active backend, so the caller decides which migrations to apply (which is what lets the migration tests build a database at an older version):

```python
from bmnews.db.schema import init_db
from tests.backends import new_db


def _db():
    conn = new_db()
    init_db(conn)
    return conn
```

In test helpers use `placeholder(conn)` and `bmlib.db.execute` rather than raw `conn.execute("… ?")`, for the same reason.

The non-DB suites (pipeline, GUI, fulltext) use in-memory SQLite: the backend-specific SQL all lives in `db/operations.py` and `db/migrations.py`, which `test_db.py` covers.

### Seeded database

For tests that need pre-populated data (e.g. testing digest rendering or cached paper retrieval):

```python
def _seeded_db():
    conn = _db()
    pid = store_paper(conn, doi="10.1101/test", title="Test Paper",
                      abstract="Abstract", published_date="2026-02-10",
                      source="medrxiv")
    save_score(conn, paper_id=pid, combined_score=0.8, relevance_score=0.9,
               quality_score=0.7, summary="Great paper.")
    record_digest(conn, [pid], delivery_method="stdout")
    return conn
```

`store_paper()` needs a DOI **or** a PMID and raises `ValueError` given neither — bmlib has nothing to key the record on.

### The shared GUI job state

`bmnews.gui.jobs` owns process state — one lock, one status dict, one thread — so any test driving `POST /pipeline/run` or a watches delivery would leak it into whatever runs next. The autouse `idle_jobs` fixture in `conftest.py` returns it to idle around **every** test in the suite, forcing the lock open if a worker outlived its test. Without that, one leaked job makes every later `jobs.start()` refuse, and the failures surface far from their cause.

A test that needs a file-backed database (`test_notify_service.py` does, because each `run_notify` opens its own connection and an in-memory database dies with the connection that made it) should use `tmp_path`.

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
from bmlib.quality import QualityAssessment, StudyDesign

from bmnews.scoring.scorer import _quality_tier_to_score


class TestQualityTierToScore:
    def test_rct(self):
        a = QualityAssessment.from_metadata(StudyDesign.RCT)
        score = _quality_tier_to_score(a)
        assert score == 0.8
```

For tests that do need LLM interaction (integration tests), mock `RelevanceAgent.score()` or use a fixture returning canned responses. **No unit test makes a real LLM call.**

The notification matcher is pure `(paper, watch) -> bool`, so every criterion is tested against literal paper dicts with no database, SMTP or LLM in the picture — that is the property that makes the criteria engine cheap to extend. Channel adapters are tested against a recording fake; delivery failures are asserted as `ChannelError`, since that is the only exception `run_notify()` reads as "this delivery did not happen".

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

1. Add test class in `tests/test_db.py` — it runs against **both** backends
2. Use the `_db()` helper for a clean database
3. Test both the happy path and edge cases

```python
class TestNewOperation:
    def test_basic_case(self):
        conn = _db()
        # Set up data
        pid = store_paper(conn, doi="10.1101/x", title="X")
        # Call your operation
        result = your_new_operation(conn, pid)
        # Assert
        assert result == expected

    def test_empty_case(self):
        conn = _db()
        result = your_new_operation(conn, 999)
        assert result is None
```

Run the PostgreSQL half before you push: without a DSN it skips, and backend-specific SQL is exactly what these tests exist to cover.

### New fetcher

1. Add test in `tests/test_fetchers.py`
2. Pass a fake HTTP client returning canned API responses
3. Verify the `FetchedRecord` fields — including `publication_types`, which feeds bmlib's free Tier-1 quality classification
4. Verify the source is registered in bmlib's registry (`source_names()`)

### New watch criterion

1. Add parsing and validation tests in `tests/test_notify.py` — the matcher runs against literal paper dicts
2. Add the SQL-narrowing half to `tests/test_db.py` if `get_notification_candidates()` changes
3. If the criterion is applied in Python, check `tests/test_notify_service.py` still shows paging with no gaps or repeats

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

### New GUI route

1. Add tests in `tests/test_gui_app.py` (or `test_gui_notify.py` for the watches pane) using the Flask test client
2. Build the app with `create_app()` and a test config
3. If the route starts background work, it goes through `gui/jobs.py` — assert that starting one while another runs is *refused*, not raced

## Running lint

```bash
uv run ruff check bmnews/ tests/
uv run ruff format --check bmnews/ tests/
```
