# `bmnews run --days` and `--show_cached` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `--days` and `--show_cached` parameters to `bmnews run` so users can control fetch lookback and review previously generated digests.

**Architecture:** New DB query function `get_cached_digest_papers` joins digest_papers → papers → scores, filtered by published_date. Pipeline gets `show_cached` path that queries DB and re-renders via existing Jinja2 templates. CLI wires the two new Click options through to pipeline.

**Tech Stack:** Click (CLI), SQLite/PostgreSQL (DB), Jinja2 (templates), pytest (tests)

---

### Task 1: Add `get_cached_digest_papers` DB query

**Files:**
- Modify: `bmnews/db/operations.py:193-215` (add after `get_papers_for_digest`)
- Test: `tests/test_db.py`

**Step 1: Write the failing test**

Add to `tests/test_db.py`:

```python
from bmnews.db.operations import get_cached_digest_papers

class TestCachedDigestPapers:
    def test_returns_papers_from_previous_digests(self):
        conn = _db()
        pid1 = upsert_paper(conn, doi="10.1101/c1", title="Cached 1",
                            abstract="A1", published_date="2026-02-10")
        pid2 = upsert_paper(conn, doi="10.1101/c2", title="Not in digest",
                            abstract="A2", published_date="2026-02-10")
        save_score(conn, paper_id=pid1, combined_score=0.8, summary="Sum1")
        save_score(conn, paper_id=pid2, combined_score=0.7, summary="Sum2")
        record_digest(conn, [pid1], delivery_method="stdout")

        cached = get_cached_digest_papers(conn)
        assert len(cached) == 1
        assert cached[0]["doi"] == "10.1101/c1"
        assert cached[0]["combined_score"] == 0.8

    def test_filters_by_published_date(self):
        conn = _db()
        pid_old = upsert_paper(conn, doi="10.1101/old", title="Old Paper",
                               abstract="A", published_date="2020-01-01")
        pid_new = upsert_paper(conn, doi="10.1101/new", title="New Paper",
                               abstract="B", published_date="2026-02-12")
        save_score(conn, paper_id=pid_old, combined_score=0.8)
        save_score(conn, paper_id=pid_new, combined_score=0.9)
        record_digest(conn, [pid_old, pid_new], delivery_method="stdout")

        cached = get_cached_digest_papers(conn, days=7)
        assert len(cached) == 1
        assert cached[0]["doi"] == "10.1101/new"

    def test_no_days_returns_all_cached(self):
        conn = _db()
        pid_old = upsert_paper(conn, doi="10.1101/old2", title="Old",
                               abstract="A", published_date="2020-01-01")
        pid_new = upsert_paper(conn, doi="10.1101/new2", title="New",
                               abstract="B", published_date="2026-02-12")
        save_score(conn, paper_id=pid_old, combined_score=0.8)
        save_score(conn, paper_id=pid_new, combined_score=0.9)
        record_digest(conn, [pid_old, pid_new], delivery_method="stdout")

        cached = get_cached_digest_papers(conn)
        assert len(cached) == 2

    def test_empty_when_no_digests(self):
        conn = _db()
        upsert_paper(conn, doi="10.1101/x", title="X", abstract="A",
                     published_date="2026-02-10")
        cached = get_cached_digest_papers(conn)
        assert cached == []
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::TestCachedDigestPapers -v`
Expected: FAIL with `ImportError: cannot import name 'get_cached_digest_papers'`

**Step 3: Write minimal implementation**

Add to `bmnews/db/operations.py` after `get_papers_for_digest` (after line 215):

```python
def get_cached_digest_papers(conn: Any, days: int | None = None) -> list[dict]:
    """Get papers that were included in previous digests.

    Args:
        conn: DB-API connection.
        days: If provided, only return papers with published_date
              within the last N days.

    Returns:
        List of paper dicts with scoring data, same format as
        get_papers_for_digest.
    """
    ph = _placeholder(conn)
    is_sqlite = "sqlite3" in type(conn).__module__

    if days is not None:
        if is_sqlite:
            date_filter = f"AND p.published_date >= date('now', '-' || {ph} || ' days')"
        else:
            date_filter = f"AND p.published_date >= (CURRENT_DATE - ({ph} || ' days')::interval)::text"
        params: tuple = (days,)
    else:
        date_filter = ""
        params = ()

    rows = fetch_all(
        conn,
        f"""
        SELECT DISTINCT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier
        FROM papers p
        JOIN scores s ON s.paper_id = p.id
        JOIN digest_papers dp ON dp.paper_id = p.id
        WHERE 1=1 {date_filter}
        ORDER BY s.combined_score DESC
        """,
        params,
    )
    return [_row_to_dict(r) for r in rows]
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py::TestCachedDigestPapers -v`
Expected: PASS (all 4 tests)

**Step 5: Commit**

```bash
git add bmnews/db/operations.py tests/test_db.py
git commit -m "feat: add get_cached_digest_papers DB query"
```

---

### Task 2: Add `show_cached_digests` to pipeline

**Files:**
- Modify: `bmnews/pipeline.py:205-218` (update `run_pipeline` signature, add `show_cached_digests`)
- Test: `tests/test_pipeline.py` (new file — tests for the two new pipeline behaviors)

**Step 1: Write the failing test**

Create `tests/test_pipeline.py`:

```python
"""Tests for bmnews.pipeline show_cached and days parameter."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from bmlib.db import connect_sqlite
from bmlib.templates import TemplateEngine

from bmnews.config import AppConfig, load_config
from bmnews.db.schema import init_db
from bmnews.db.operations import upsert_paper, save_score, record_digest
from bmnews.pipeline import show_cached_digests


def _test_config():
    """Build a minimal config pointing at an in-memory DB."""
    config = load_config(None)
    config.database.backend = "sqlite"
    config.database.sqlite_path = ":memory:"
    return config


def _seeded_db():
    """Return a conn with papers, scores, and a digest recorded."""
    conn = connect_sqlite(":memory:")
    init_db(conn)
    pid = upsert_paper(conn, doi="10.1101/cached1", title="Cached Paper",
                       abstract="Abs", published_date="2026-02-10",
                       source="medrxiv")
    save_score(conn, paper_id=pid, combined_score=0.8, relevance_score=0.9,
               quality_score=0.7, summary="Great paper.")
    record_digest(conn, [pid], delivery_method="stdout")
    return conn


class TestShowCachedDigests:
    @patch("bmnews.pipeline.open_db")
    def test_renders_cached_papers(self, mock_open_db):
        mock_open_db.return_value = _seeded_db()
        config = _test_config()
        text = show_cached_digests(config)
        assert "Cached Paper" in text

    @patch("bmnews.pipeline.open_db")
    def test_returns_empty_when_no_cached(self, mock_open_db):
        conn = connect_sqlite(":memory:")
        init_db(conn)
        mock_open_db.return_value = conn
        config = _test_config()
        text = show_cached_digests(config)
        assert text == ""
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline.py -v`
Expected: FAIL with `ImportError: cannot import name 'show_cached_digests'`

**Step 3: Write minimal implementation**

Add to `bmnews/pipeline.py` after imports — add `get_cached_digest_papers` to the imports from `bmnews.db.operations`:

```python
from bmnews.db.operations import (
    upsert_paper,
    get_unscored_papers,
    save_score,
    get_papers_for_digest,
    get_cached_digest_papers,
    record_digest,
)
```

Add new function before `run_pipeline`:

```python
def show_cached_digests(config: AppConfig, days: int | None = None) -> str:
    """Re-render previously digested papers to stdout.

    Args:
        config: Application config.
        days: If provided, filter to papers published in the last N days.

    Returns:
        Rendered text, or empty string if no cached papers.
    """
    conn = open_db(config)
    init_db(conn)

    papers = get_cached_digest_papers(conn, days=days)
    conn.close()

    if not papers:
        logger.info("No cached digest papers found")
        return ""

    templates = build_template_engine(config)
    text_body = render_digest(
        papers, templates,
        subject_prefix=config.email.subject_prefix,
        fmt="text",
    )
    print(text_body)
    return text_body
```

Update `run_pipeline` signature and body:

```python
def run_pipeline(
    config: AppConfig,
    days: int | None = None,
    show_cached: bool = False,
) -> None:
    """Execute the full pipeline: fetch → store → score → digest.

    Args:
        config: Application config.
        days: Override lookback_days for fetching.
        show_cached: If True, skip pipeline and show cached digests.
    """
    if show_cached:
        text = show_cached_digests(config, days=days)
        if not text:
            logger.info("No cached digest papers found")
        return

    if days is not None:
        config.sources.lookback_days = days

    logger.info("Starting pipeline run")

    papers = run_fetch(config)
    if papers:
        run_store(config, papers)

    scored = run_score(config)
    if scored > 0:
        run_digest(config)

    logger.info("Pipeline complete")
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_pipeline.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add bmnews/pipeline.py tests/test_pipeline.py
git commit -m "feat: add show_cached_digests and days param to run_pipeline"
```

---

### Task 3: Wire CLI options to `run` command

**Files:**
- Modify: `bmnews/cli.py:33-38` (add Click options to `run`)

**Step 1: Write the failing test**

Add to `tests/test_pipeline.py`:

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
        assert "Cached Paper" in result.output

    @patch("bmnews.pipeline.open_db")
    def test_run_show_cached_with_days(self, mock_open_db):
        mock_open_db.return_value = _seeded_db()
        runner = CliRunner()
        result = runner.invoke(main, ["run", "--show_cached", "--days", "30"])
        assert result.exit_code == 0
        assert "Cached Paper" in result.output

    def test_run_days_without_show_cached(self):
        """--days without --show_cached passes through to pipeline."""
        runner = CliRunner()
        with patch("bmnews.pipeline.run_pipeline") as mock_pipeline:
            result = runner.invoke(main, ["run", "--days", "14"])
            assert result.exit_code == 0
            mock_pipeline.assert_called_once()
            call_kwargs = mock_pipeline.call_args
            # days=14 should be passed through
            assert call_kwargs[1].get("days") == 14 or call_kwargs[0][1] == 14
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline.py::TestRunCLI -v`
Expected: FAIL — `run` command doesn't accept `--show_cached` or `--days`

**Step 3: Write minimal implementation**

Replace the `run` command in `bmnews/cli.py` (lines 33-38):

```python
@main.command()
@click.option("--days", default=None, type=int, help="Override lookback days for fetching.")
@click.option("--show_cached", is_flag=True, default=False,
              help="Show cached digests instead of running pipeline.")
@click.pass_context
def run(ctx: click.Context, days: int | None, show_cached: bool) -> None:
    """Run the full pipeline: fetch → score → digest."""
    from bmnews.pipeline import run_pipeline
    run_pipeline(ctx.obj["config"], days=days, show_cached=show_cached)
```

**Step 4: Run all tests**

Run: `pytest tests/ -v`
Expected: ALL PASS

**Step 5: Run linter**

Run: `ruff check bmnews/ tests/`
Expected: No errors

**Step 6: Commit**

```bash
git add bmnews/cli.py tests/test_pipeline.py
git commit -m "feat: add --days and --show_cached options to bmnews run"
```

---

### Task 4: Manual smoke test

**Step 1:** Run `bmnews run --help` and verify both `--days` and `--show_cached` appear in usage.

**Step 2:** If a database exists with data, run `bmnews run --show_cached` and verify output.

**Step 3:** Run `bmnews run --show_cached --days 7` and verify filtering.
