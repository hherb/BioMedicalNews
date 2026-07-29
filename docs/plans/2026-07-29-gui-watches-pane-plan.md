# GUI Watches Pane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the desktop GUI a Watches tab that shows what each watch has delivered and still has queued, and lets the user pull the next batch or drain the queue.

**Architecture:** The shared background-job machinery (lock, status, daemon thread) moves out of `gui/routes/pipeline.py` into a new `gui/jobs.py`, because delivery and a pipeline run must not race on the same database. A new `gui/routes/watches.py` blueprint renders `notify.service.pending_counts()` joined onto `notify.watches.parse_watches()`, and starts `run_notify()` through `jobs.start()`. Counts refresh exactly once, when the job finishes, via a poller that gets a 204 while one is running.

**Tech Stack:** Flask blueprints, Jinja2 fragments, HTMX 2.x (OOB swaps, 204-no-swap), pytest with Flask's test client.

**Design document:** `docs/plans/2026-07-29-gui-watches-pane-design.md`

## Global Constraints

- Python 3.11+; `from __future__ import annotations` at the top of every module.
- ruff: line-length 100, rules E, F, I, N, W, UP. Run `uv run ruff check bmnews/ tests/` and `uv run ruff format --check bmnews/ tests/` before every commit.
- Type hints on all function signatures; Google-style docstrings on public functions and classes.
- Module-level loggers: `logger = logging.getLogger(__name__)`.
- Keyword-only arguments for functions that write or mutate.
- Use `uv run` for everything. Never `pip`.
- No database migration, no schema change, and no change to anything under `bmnews/notify/` — this plan only reads what the notification service already exposes.
- Commit messages are conventional (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`) and end with:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- Full suite must pass before each commit: `uv run pytest tests/ -q`.
- No PostgreSQL run needed: this plan touches neither `db/operations.py` nor `db/migrations.py`.

---

## File Structure

| File | Responsibility |
|---|---|
| `bmnews/gui/jobs.py` | **Create.** The one background job: lock, status dict, thread launch, progress, status-bar rendering, `wait_for_idle`. |
| `bmnews/gui/routes/pipeline.py` | **Modify.** Drops its private lock/status/launcher and delegates to `jobs`. Keeps `_scored_paper_ids` and the OOB card machinery. |
| `bmnews/gui/routes/watches.py` | **Create.** The pane: view model, `GET /watches`, the two POST routes, `GET /watches/rows`. |
| `bmnews/gui/templates/fragments/watches_view.html` | **Create.** Tab body: heading, notices, `#watch-list`, `#watch-poller`, Refresh. |
| `bmnews/gui/templates/fragments/watch_list.html` | **Create.** The rows. Swapped on refresh. |
| `bmnews/gui/templates/fragments/watch_poller.html` | **Create.** The one-line completion poller. Rendered from two places. |
| `bmnews/gui/templates/base.html` | **Modify.** A third nav tab. |
| `bmnews/gui/app.py` | **Modify.** Register `watches_bp`. |
| `bmnews/gui/static/css/app.css` | **Modify.** Watch card styling. |
| `tests/test_gui_jobs.py` | **Create.** `jobs.py` in isolation. |
| `tests/test_gui_notify.py` | **Create.** The pane and its four routes. |
| `tests/test_gui_app.py` | **Modify.** Replace the sleep-poll with `jobs.wait_for_idle()`. |

---

## Task 1: Extract the shared background job

**Files:**
- Create: `bmnews/gui/jobs.py`
- Modify: `bmnews/gui/routes/pipeline.py` (whole file)
- Test: `tests/test_gui_jobs.py` (create), `tests/test_gui_app.py:165-179` (modify)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, all in `bmnews.gui.jobs`:
  - `status() -> dict[str, Any]` — the live status dict (mutate in place)
  - `running() -> bool`
  - `progress(message: str) -> None`
  - `start(*, message: str, target: Callable[[], None], error_label: str) -> bool`
  - `render_status_bar() -> str`
  - `wait_for_idle(timeout: float = 5.0) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gui_jobs.py`:

```python
"""Tests for the GUI's shared background job."""

from __future__ import annotations

import threading

import pytest

from bmnews.gui import jobs


@pytest.fixture(autouse=True)
def idle_jobs():
    """Leave the module-level job state clean around every test."""
    jobs.wait_for_idle(5.0)
    jobs.status().update(running=False, message="Ready", status="idle", refresh_list=False)
    yield
    jobs.wait_for_idle(5.0)
    jobs.status().update(running=False, message="Ready", status="idle", refresh_list=False)


class TestStart:
    def test_runs_the_target_and_reports_busy(self):
        ran = threading.Event()

        def _target() -> None:
            ran.set()
            jobs.status().update(running=False, message="Done.", status="success")

        assert jobs.start(message="Working...", target=_target, error_label="Job error") is True
        assert jobs.wait_for_idle(5.0) is True
        assert ran.is_set()
        assert jobs.status()["message"] == "Done."
        assert jobs.status()["status"] == "success"

    def test_second_job_is_refused_while_one_runs(self):
        release = threading.Event()
        started = threading.Event()
        runs = []

        def _target() -> None:
            runs.append(1)
            started.set()
            release.wait(5.0)
            jobs.status().update(running=False, message="Done.", status="success")

        assert jobs.start(message="First...", target=_target, error_label="Job error") is True
        started.wait(5.0)
        assert jobs.start(message="Second...", target=_target, error_label="Job error") is False
        # The refusal must not overwrite the running job's own progress line.
        assert jobs.status()["message"] == "First..."
        release.set()
        assert jobs.wait_for_idle(5.0) is True
        assert runs == [1]

    def test_a_raising_target_publishes_an_error_and_frees_the_lock(self):
        def _boom() -> None:
            raise RuntimeError("no homeserver")

        assert jobs.start(message="Working...", target=_boom, error_label="Job error") is True
        assert jobs.wait_for_idle(5.0) is True
        assert jobs.status()["status"] == "error"
        assert "Job error: no homeserver" == jobs.status()["message"]
        # The lock was released, so the next job can start.
        assert jobs.start(message="Next...", target=lambda: None, error_label="Job error") is True
        assert jobs.wait_for_idle(5.0) is True

    def test_a_target_that_forgets_to_clear_running_is_corrected(self):
        assert jobs.start(message="Working...", target=lambda: None, error_label="Job error") is True
        assert jobs.wait_for_idle(5.0) is True
        assert jobs.running() is False


class TestProgress:
    def test_progress_replaces_the_message(self):
        jobs.progress("Scoring 3 papers...")
        assert jobs.status()["message"] == "Scoring 3 papers..."
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_gui_jobs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bmnews.gui.jobs'`

- [ ] **Step 3: Create `bmnews/gui/jobs.py`**

```python
"""The single background job the GUI runs, and the status bar reporting it.

The pipeline routes and the watches pane both run work in a daemon thread
against the same database, and they must not overlap: a notification delivery
racing a scoring run would page through a queue that run is still changing.
There is therefore one lock and one status, owned here rather than by whichever
blueprint happened to need them first.

Rendering the status fragment lives here too. It is the job's presentation, and
two blueprints returning it means one definition or two that drift.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from flask import render_template

logger = logging.getLogger(__name__)

# Guards against two background jobs racing on the same database. Acquired in
# the request thread and released by the worker thread's finally block, which
# a plain (non-reentrant) Lock permits.
_lock = threading.Lock()
_status: dict[str, Any] = {
    "running": False,
    "message": "Ready",
    "status": "idle",
    "refresh_list": False,  # Signal the next status poll to reload #paper-list
}
_thread: threading.Thread | None = None


def status() -> dict[str, Any]:
    """The live status dict the status bar renders from.

    Returned rather than copied: callers publish a terminal status by updating
    it in place, which is what the worker threads do when they finish.
    """
    return _status


def running() -> bool:
    """Whether a background job is in flight."""
    return bool(_status["running"])


def progress(message: str) -> None:
    """Record the latest progress line for the status poller."""
    _status["message"] = message


def start(*, message: str, target: Callable[[], None], error_label: str) -> bool:
    """Run *target* in a daemon thread, unless another job holds the lock.

    Args:
        message: Busy message published while the job runs.
        target: The work. It pushes its own app context and publishes its own
            terminal success message; a failure is handled here.
        error_label: Prefix for the message published if *target* raises, e.g.
            ``"Pipeline error"``.

    Returns:
        True if the job started. False if one was already running, or if the
        thread could not be spawned at all. The caller does not have to tell
        those apart: in both cases :func:`status` already holds the message to
        show — the running job's own progress line, or the spawn failure.
    """
    global _thread

    if not _lock.acquire(blocking=False):
        # Deliberately no status update: the running job's progress line is
        # what the user needs to see, and overwriting it with "already running"
        # would replace live information with a truism.
        logger.debug("A background job is already running — refusing another")
        return False

    _status.update(running=True, message=message, status="busy")

    def _run() -> None:
        try:
            target()
        except Exception as exc:
            logger.exception("%s", error_label)
            _status.update(message=f"{error_label}: {exc}", status="error")
        finally:
            # A target that returns without publishing a terminal status would
            # otherwise leave the status bar spinning forever over no job.
            _status["running"] = False
            _lock.release()

    try:
        _thread = threading.Thread(target=_run, daemon=True)
        _thread.start()
    except RuntimeError as exc:
        # The worker never ran, so its finally block will not release the lock.
        logger.exception("Could not start a background job")
        _status.update(running=False, message=f"{error_label}: {exc}", status="error")
        _lock.release()
        return False

    return True


def render_status_bar() -> str:
    """Render the status-bar fragment from the current job status."""
    return render_template(
        "fragments/status_bar.html",
        message=_status["message"],
        status=_status["status"],
        running=_status["running"],
    )


def wait_for_idle(timeout: float = 5.0) -> bool:
    """Block until the running job finishes.

    Exists so tests can assert on a background job without sleep-polling for
    it, which is the only way a caller outside this module can know a daemon
    thread has finished.

    Args:
        timeout: Seconds to wait for the thread to end.

    Returns:
        True if no job is running when this returns.
    """
    thread = _thread
    if thread is not None:
        thread.join(timeout)
    return not running()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_gui_jobs.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Rewrite `bmnews/gui/routes/pipeline.py` onto `jobs`**

Replace the whole file with:

```python
"""Pipeline execution routes."""

from __future__ import annotations

import logging
from collections import deque

from flask import Blueprint, Flask, current_app, render_template

from bmnews.config import AppConfig
from bmnews.constants import DEFAULT_PAGE_SIZE
from bmnews.db.operations import count_unscored_papers, get_paper_with_score
from bmnews.gui import jobs

pipeline_bp = Blueprint("pipeline", __name__)
logger = logging.getLogger(__name__)

# Paper IDs scored since last status poll, consumed on each poll. deque's
# append/popleft are atomic, so no extra locking is needed here.
_scored_paper_ids: deque[int] = deque()


@pipeline_bp.route("/pipeline/run", methods=["POST"])
def run() -> str:
    """Start a full fetch → store → score → digest run in the background.

    Returns:
        The ``status_bar`` HTMX fragment — of the run just started, or of
        whatever job was already in flight.
    """
    from bmnews.pipeline import run_pipeline

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app: Flask = current_app._get_current_object()

    def _run() -> None:
        """Run the pipeline, then publish a terminal status."""

        def _progress_with_refresh(message: str) -> None:
            """Record progress and flag the list for reload once scoring starts."""
            jobs.progress(message)
            # After storing completes, the paper list needs a full reload
            if "Scoring" in message and not jobs.status().get("refresh_list"):
                jobs.status()["refresh_list"] = True

        with app.app_context():
            run_pipeline(
                config,
                on_progress=_progress_with_refresh,
                on_scored=_scored_paper_ids.append,
            )
        jobs.status().update(
            running=False,
            message="Pipeline complete — papers fetched, scored, and digested.",
            status="success",
            refresh_list=True,
        )

    jobs.start(message="Starting pipeline...", target=_run, error_label="Pipeline error")
    return jobs.render_status_bar()


@pipeline_bp.route("/pipeline/resume", methods=["POST"])
def resume() -> str:
    """Resume scoring papers left unscored by a previous session.

    Called on app startup. Does nothing when everything is already scored or
    a run is in flight.

    Returns:
        The ``status_bar`` HTMX fragment.
    """
    from bmnews.pipeline import run_score

    conn = current_app.config["BMNEWS_DB"]
    count = count_unscored_papers(conn)

    if count == 0 or jobs.running():
        return jobs.render_status_bar()

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app: Flask = current_app._get_current_object()

    def _run() -> None:
        """Score the outstanding papers, then publish a terminal status."""
        with app.app_context():
            scored = run_score(
                config,
                on_progress=jobs.progress,
                on_scored=_scored_paper_ids.append,
            )
        jobs.status().update(
            running=False,
            message=f"Resumed scoring complete — {scored} papers scored.",
            status="success",
        )

    jobs.start(
        message=f"Resuming scoring of {count} papers...",
        target=_run,
        error_label="Scoring error",
    )
    return jobs.render_status_bar()


@pipeline_bp.route("/pipeline/status")
def status() -> str:
    """Report pipeline progress, with out-of-band updates for scored papers.

    Polled by the status bar. Each poll drains the queue of newly scored paper
    ids and emits an OOB swap for each affected card, or a single full-list
    refresh when the pipeline signalled that new papers were stored.

    Returns:
        The ``status_bar`` fragment, optionally followed by OOB swap markup.
    """
    conn = current_app.config["BMNEWS_DB"]

    # If the pipeline stored new papers, reload the full paper list via OOB
    needs_list_refresh = jobs.status().get("refresh_list", False)
    if needs_list_refresh:
        jobs.status()["refresh_list"] = False

    # Drain any paper IDs scored since last poll and render OOB card updates
    oob_cards: list[str] = []
    if not needs_list_refresh:
        # Only do per-card OOB updates when we're NOT doing a full list refresh
        # (a full refresh already includes the latest card state)
        while _scored_paper_ids:
            pid = _scored_paper_ids.popleft()
            paper = get_paper_with_score(conn, pid)
            if paper:
                card_html = render_template("fragments/paper_card.html", paper=paper)
                card_html = card_html.replace(
                    f'id="paper-card-{pid}"',
                    f'id="paper-card-{pid}" hx-swap-oob="outerHTML"',
                    1,
                )
                oob_cards.append(card_html)
    else:
        _scored_paper_ids.clear()

    html = jobs.render_status_bar()

    if needs_list_refresh:
        # Trigger a full paper list reload via OOB swap
        from bmnews.db.operations import get_papers_filtered

        papers, total = get_papers_filtered(
            conn,
            sort="date",
            limit=DEFAULT_PAGE_SIZE,
            offset=0,
            with_total=True,
        )
        list_html = render_template(
            "fragments/paper_list.html",
            papers=papers,
            total=total,
            offset=0,
            limit=DEFAULT_PAGE_SIZE,
            sort="date",
            source="",
            tier="",
            design="",
            search="",
        )
        html += f'<div id="paper-list" hx-swap-oob="innerHTML">{list_html}</div>'
    elif oob_cards:
        html += "\n".join(oob_cards)

    return html
```

Two deliberate behaviour changes, both improvements over what this replaced:

1. A refused start now returns the *running* job's status instead of the fixed string `"Pipeline already running..."`, so the user sees live progress rather than a truism.
2. A thread that cannot be spawned now renders an error status instead of re-raising into a 500. The lock is still released.

- [ ] **Step 6: Replace the sleep-poll in the existing GUI test**

In `tests/test_gui_app.py`, replace `TestPipelineRoute.test_run_pipeline_returns_status` (currently at lines 166-178) with:

```python
    def test_run_pipeline_returns_status(self, client):
        from bmnews.gui import jobs

        with patch("bmnews.pipeline.run_pipeline") as mock_run:
            resp = client.post("/pipeline/run")
            assert resp.status_code == 200
            assert b"pipeline" in resp.data.lower()
            assert jobs.wait_for_idle(5.0) is True
            mock_run.assert_called_once()
```

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: PASS, 404 passed (399 before + 5 new), 116 skipped

- [ ] **Step 8: Lint**

Run: `uv run ruff check bmnews/ tests/ && uv run ruff format --check bmnews/ tests/`
Expected: no findings

- [ ] **Step 9: Commit**

```bash
git add bmnews/gui/jobs.py bmnews/gui/routes/pipeline.py tests/test_gui_jobs.py tests/test_gui_app.py
git commit -m "$(cat <<'EOF'
refactor(gui): extract the shared background job into gui/jobs.py

The watches pane needs the same lock the pipeline routes hold — a
delivery must not page through a queue a scoring run is still changing —
so the lock, the status and the thread launch move somewhere both
blueprints can reach without importing each other's privates.

Both pipeline routes duplicated the same acquire / set-busy / spawn /
release sequence, including the spawn-failure path that has to release a
lock no worker will. That now exists once. A refused start returns the
running job's own progress line rather than a fixed "already running",
and a thread that cannot be spawned renders an error instead of a 500.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: The pane — `GET /watches`

**Files:**
- Create: `bmnews/gui/routes/watches.py`
- Create: `bmnews/gui/templates/fragments/watches_view.html`
- Create: `bmnews/gui/templates/fragments/watch_list.html`
- Create: `bmnews/gui/templates/fragments/watch_poller.html`
- Modify: `bmnews/gui/app.py` (register the blueprint)
- Modify: `bmnews/gui/templates/base.html:13-14` (a third tab)
- Modify: `bmnews/gui/static/css/app.css` (append the watch styles)
- Test: `tests/test_gui_notify.py` (create)

**Interfaces:**
- Consumes: `bmnews.gui.jobs.running` (Task 1).
- Produces, in `bmnews.gui.routes.watches`:
  - `watches_bp: Blueprint`
  - `ChannelRow(name: str, delivered: int, matching: int, remaining: int)` — frozen dataclass
  - `WatchRow(name: str, enabled: bool, criteria: str, max_per_run: int, channels: tuple[ChannelRow, ...], remaining: int, deliverable: bool)` — frozen dataclass
  - `_build_rows(config: AppConfig) -> tuple[list[WatchRow], list[str]]` — rows, and the names of watches that failed to parse

- [ ] **Step 1: Write the failing test**

Create `tests/test_gui_notify.py`:

```python
"""Tests for the GUI watches pane."""

from __future__ import annotations

import pytest
from bmlib.db import connect_sqlite

from bmnews.config import AppConfig
from bmnews.db.schema import init_db
from bmnews.gui import jobs
from bmnews.notify.service import DeliveryReport


@pytest.fixture(autouse=True)
def idle_jobs():
    """Leave the module-level job state clean around every test."""
    jobs.wait_for_idle(5.0)
    jobs.status().update(running=False, message="Ready", status="idle", refresh_list=False)
    yield
    jobs.wait_for_idle(5.0)
    jobs.status().update(running=False, message="Ready", status="idle", refresh_list=False)


@pytest.fixture
def config(tmp_path):
    """A config with one watch on one channel.

    ``sqlite_path`` is pointed at a scratch file so that a route reaching the
    database despite the patches below cannot touch the developer's own.
    """
    config = AppConfig()
    config.database.sqlite_path = str(tmp_path / "test.db")
    config.notifications.enabled = True
    config.notifications.channels = {
        "mailbox": {"kind": "email", "to_address": "reader@example.com"},
    }
    config.notifications.watches = {
        "melanoma": {
            "min_relevance": 0.7,
            "tags": ["melanoma"],
            "channels": ["mailbox"],
            "max_per_run": 5,
        },
    }
    return config


@pytest.fixture
def client(config):
    from bmnews.gui.app import create_app

    conn = connect_sqlite(":memory:")
    init_db(conn)
    app = create_app(config, conn)
    app.config["TESTING"] = True
    return app.test_client()


def report(watch="melanoma", channel="mailbox", **kwargs):
    """A pending_counts-shaped report, with the fields that function fills."""
    defaults = {"enabled": True, "sent_total": 3, "matching": 12, "remaining": 9}
    return DeliveryReport(watch=watch, channel=channel, **{**defaults, **kwargs})


class TestPane:
    def test_renders_a_row_per_watch_and_channel(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        resp = client.get("/watches")

        assert resp.status_code == 200
        body = resp.data.decode()
        assert "melanoma" in body
        assert "mailbox" in body
        assert ">3<" in body  # delivered
        assert ">12<" in body  # matching
        assert ">9<" in body  # remaining

    def test_shows_the_criteria_summary(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        body = client.get("/watches").data.decode()

        assert "relevance ≥ 0.7" in body
        assert "tags: melanoma" in body

    def test_a_watch_whose_channels_resolve_to_nothing_is_still_listed(
        self, client, config, monkeypatch
    ):
        # resolve_channels() skips an unknown channel name, so pending_counts
        # returns nothing at all for this watch. It must not vanish from the
        # pane — nothing will ever be delivered for it and that is worth saying.
        config.notifications.watches["orphan"] = {"channels": ["nowhere"]}
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        body = client.get("/watches").data.decode()

        assert "orphan" in body
        assert "no configured channel" in body

    def test_an_unparseable_watch_is_named(self, client, config, monkeypatch):
        # parse_watches() skips this one with an ERROR log; without the diff
        # against the raw config it would be invisible in the GUI.
        config.notifications.watches["broken"] = {"min_relevance": "very"}
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        body = client.get("/watches").data.decode()

        assert "broken" in body
        assert "could not be read" in body

    def test_a_disabled_watch_shows_counts_but_no_buttons(self, client, config, monkeypatch):
        config.notifications.watches["melanoma"]["enabled"] = False
        monkeypatch.setattr(
            "bmnews.notify.service.pending_counts", lambda config: [report(enabled=False)]
        )

        body = client.get("/watches").data.decode()

        assert "disabled" in body
        assert ">9<" in body
        assert "/watches/melanoma/notify" not in body

    def test_globally_disabled_notifications_show_a_notice_and_no_buttons(
        self, client, config, monkeypatch
    ):
        config.notifications.enabled = False
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])

        body = client.get("/watches").data.decode()

        assert "switched off" in body
        assert "/watches/melanoma/notify" not in body

    def test_an_exhausted_watch_has_no_buttons(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.pending_counts",
            lambda config: [report(remaining=0, exhausted=True)],
        )

        body = client.get("/watches").data.decode()

        assert "melanoma" in body
        assert "/watches/melanoma/notify" not in body

    def test_no_watches_configured(self, client, config, monkeypatch):
        config.notifications.watches = {}
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [])

        body = client.get("/watches").data.decode()

        assert "No watches configured" in body

    def test_the_tab_is_in_the_shell(self, client):
        body = client.get("/").data.decode()
        assert 'hx-get="/watches"' in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_gui_notify.py -q`
Expected: FAIL — every request 404s, because no `/watches` route is registered

- [ ] **Step 3: Create `bmnews/gui/routes/watches.py`**

```python
"""The watches pane: what each watch has delivered, and what it still has queued.

Monitoring and delivery only. Creating and editing watches stays in
``config.toml`` — see ``docs/plans/2026-07-29-gui-watches-pane-design.md``.

The rows are built from :func:`~bmnews.notify.watches.parse_watches` with the
counts joined **onto** them, rather than from the counts alone. Three
configurations produce no counts at all and would otherwise render an empty or
misleading pane: a watch naming no configured channel, a watch that fails to
parse, and a watch that is switched off. Each of those is a case where the user
believes they are being alerted and are not, which is the whole thing this pane
exists to make visible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from flask import Blueprint, current_app, render_template

from bmnews.config import AppConfig

watches_bp = Blueprint("watches", __name__)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelRow:
    """One watch's queue on one channel.

    Attributes:
        name: The channel's config table name.
        delivered: Papers ever sent for this pair. Failed attempts are not
            counted here — they stay queued and are counted in ``remaining``.
        matching: Papers the watch matches in total, delivered ones included.
        remaining: Papers still pending.
    """

    name: str
    delivered: int
    matching: int
    remaining: int


@dataclass(frozen=True)
class WatchRow:
    """One watch as the pane renders it.

    Attributes:
        name: The watch's config table name.
        enabled: Whether the watch is evaluated at all.
        criteria: One-line summary of what it matches.
        max_per_run: How many papers one batch delivers — the ``N`` in
            "Notify N more".
        channels: Its queues, one per resolved channel. Empty when none of its
            channel names matches a configured channel.
        remaining: Pending papers summed across channels.
        deliverable: Whether the delivery buttons are offered. False whenever
            pressing one would do nothing: notifications switched off, the
            watch disabled, no channel resolved, or nothing left to send.
    """

    name: str
    enabled: bool
    criteria: str
    max_per_run: int
    channels: tuple[ChannelRow, ...]
    remaining: int
    deliverable: bool


@watches_bp.route("/watches")
def watches_page() -> str:
    """Render the watches pane.

    Returns:
        The ``watches_view`` HTMX fragment.
    """
    from bmnews.gui import jobs

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    rows, unreadable = _build_rows(config)
    return render_template(
        "fragments/watches_view.html",
        rows=rows,
        unreadable=unreadable,
        notifications_enabled=config.notifications.enabled,
        # Opening the tab during a run picks up the completion refresh too.
        polling=jobs.running(),
    )


# --- Internals --------------------------------------------------------------


def _build_rows(config: AppConfig) -> tuple[list[WatchRow], list[str]]:
    """Join the pending counts onto the parsed watches.

    Args:
        config: Application config.

    Returns:
        The rows in config order, and the names of watches ``parse_watches``
        could not read — which it logs and skips, so the raw config keys are
        the only place they still exist.
    """
    from bmnews.notify.service import pending_counts
    from bmnews.notify.watches import parse_watches

    configured = config.notifications.watches or {}
    watches = parse_watches(configured)
    unreadable = sorted(set(configured) - set(watches))

    counts: dict[str, list] = {}
    if watches:
        for report in pending_counts(config):
            counts.setdefault(report.watch, []).append(report)

    rows = []
    for watch in watches.values():
        channels = tuple(
            ChannelRow(
                name=report.channel,
                delivered=report.sent_total,
                matching=report.matching,
                remaining=report.remaining,
            )
            for report in counts.get(watch.name, ())
        )
        remaining = sum(channel.remaining for channel in channels)
        rows.append(
            WatchRow(
                name=watch.name,
                enabled=watch.enabled,
                criteria=_describe(watch),
                max_per_run=watch.max_per_run,
                channels=channels,
                remaining=remaining,
                deliverable=(
                    config.notifications.enabled
                    and watch.enabled
                    and bool(channels)
                    and remaining > 0
                ),
            )
        )
    return rows, unreadable


def _describe(watch) -> str:
    """Summarise a watch's criteria in one line, in the config's vocabulary.

    A watch constraining nothing is a real configuration — it matches every
    scored paper — so it says so rather than rendering an empty line that
    reads like a display bug.
    """
    parts: list[str] = []
    if watch.min_relevance:
        parts.append(f"relevance ≥ {watch.min_relevance:g}")
    if watch.min_combined:
        parts.append(f"combined ≥ {watch.min_combined:g}")
    if watch.min_quality_tier:
        parts.append(f"tier ≥ {watch.min_quality_tier}")
    for label, values in (
        ("tags", watch.tags),
        ("keywords", watch.keywords),
        ("sources", watch.sources),
        ("journals", watch.journals),
        ("designs", watch.study_designs),
    ):
        if values:
            parts.append(f"{label}: {', '.join(values)}")
    return " · ".join(parts) or "no criteria — matches every scored paper"
```

- [ ] **Step 4: Create the three fragments**

`bmnews/gui/templates/fragments/watches_view.html`:

```html
<div class="watches-page">
    <h1>Watches</h1>

    {% if not notifications_enabled %}
    <p class="watch-notice error">
        Notifications are switched off. Set <code>enabled = true</code> under
        <code>[notifications]</code> in <code>~/.bmnews/config.toml</code> to deliver them.
    </p>
    {% endif %}

    {% if unreadable %}
    <p class="watch-notice error">
        {{ unreadable | length }} watch(es) could not be read and are not being evaluated:
        {{ unreadable | join(', ') }}. The log says what is wrong with each.
    </p>
    {% endif %}

    <div id="watch-list">
        {% include "fragments/watch_list.html" %}
    </div>

    <div id="watch-poller">
        {% if polling %}{% include "fragments/watch_poller.html" %}{% endif %}
    </div>

    <button class="btn btn-secondary"
            hx-get="/watches" hx-target="#main-content" hx-swap="innerHTML">
        Refresh
    </button>
</div>
```

`bmnews/gui/templates/fragments/watch_list.html`:

```html
{% if not rows %}
<p class="empty-state">
    No watches configured. Add one under <code>[notifications.watches]</code>
    in <code>~/.bmnews/config.toml</code>.
</p>
{% else %}
{% for row in rows %}
<section class="watch-card">
    <header class="watch-header">
        <span class="watch-name">{{ row.name }}</span>
        {% if not row.enabled %}<span class="watch-badge">disabled</span>{% endif %}
    </header>

    <p class="watch-criteria">{{ row.criteria }}</p>

    {% if row.channels %}
    <table class="watch-channels">
        <thead>
            <tr><th>Channel</th><th>Delivered</th><th>Matching</th><th>Remaining</th></tr>
        </thead>
        <tbody>
            {% for channel in row.channels %}
            <tr>
                <td>{{ channel.name }}</td>
                <td>{{ channel.delivered }}</td>
                <td>{{ channel.matching }}</td>
                <td>{{ channel.remaining }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    {% else %}
    <p class="watch-notice error">
        This watch names no configured channel, so nothing will ever be delivered for it.
    </p>
    {% endif %}

    {% if row.deliverable %}
    <div class="watch-actions">
        <button class="btn btn-primary"
                hx-post="/watches/{{ row.name | urlencode }}/notify"
                hx-target="#status-right" hx-swap="innerHTML">
            Notify {{ row.max_per_run }} more
        </button>
        <button class="btn btn-secondary"
                hx-post="/watches/{{ row.name | urlencode }}/notify-all"
                hx-target="#status-right" hx-swap="innerHTML">
            Notify all {{ row.remaining }} remaining
        </button>
    </div>
    {% endif %}
</section>
{% endfor %}
{% endif %}
```

`bmnews/gui/templates/fragments/watch_poller.html`:

```html
<div hx-get="/watches/rows" hx-trigger="every 2s"
     hx-target="#watch-list" hx-swap="innerHTML"></div>
```

- [ ] **Step 5: Register the blueprint**

In `bmnews/gui/app.py`, extend the import block and the registrations (currently lines 47-53):

```python
    from bmnews.gui.routes.papers import papers_bp
    from bmnews.gui.routes.pipeline import pipeline_bp
    from bmnews.gui.routes.settings import settings_bp
    from bmnews.gui.routes.watches import watches_bp

    app.register_blueprint(papers_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(pipeline_bp)
    app.register_blueprint(watches_bp)
```

- [ ] **Step 6: Add the tab**

In `bmnews/gui/templates/base.html`, insert between the Papers and Settings tabs (after line 13):

```html
        <a href="/watches" class="tab" hx-get="/watches" hx-target="#main-content" hx-swap="innerHTML" hx-push-url="true">Watches</a>
```

- [ ] **Step 7: Add the styles**

Append to `bmnews/gui/static/css/app.css`:

```css
/* Watches pane */
.watches-page { padding: 1.5rem; height: 100%; overflow-y: auto; }
.watches-page h1 { margin-bottom: 1.5rem; }
.watch-card {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1rem;
    margin-bottom: 1rem;
}
.watch-header { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem; }
.watch-name { font-weight: 600; }
.watch-badge {
    font-size: 0.75rem;
    padding: 0.1rem 0.4rem;
    border-radius: 10px;
    background: var(--bg-alt);
    color: var(--text-muted);
}
.watch-criteria { color: var(--text-muted); font-size: 0.85rem; margin-bottom: 0.75rem; }
.watch-channels { border-collapse: collapse; font-size: 0.85rem; margin-bottom: 0.75rem; }
.watch-channels th, .watch-channels td {
    text-align: left;
    padding: 0.25rem 1rem 0.25rem 0;
}
.watch-channels th { color: var(--text-muted); font-weight: 500; }
.watch-actions { display: flex; gap: 0.5rem; }
.watch-notice { font-size: 0.85rem; margin-bottom: 0.75rem; }
.watch-notice.error { color: var(--danger); }
```

`#main-content` is `overflow: hidden`, which is why `.watches-page` carries its
own `height: 100%; overflow-y: auto` rather than relying on the page to scroll.

- [ ] **Step 8: Run the test to verify it passes**

Run: `uv run pytest tests/test_gui_notify.py -q`
Expected: PASS (9 tests)

- [ ] **Step 9: Run the full suite and lint**

Run: `uv run pytest tests/ -q && uv run ruff check bmnews/ tests/ && uv run ruff format --check bmnews/ tests/`
Expected: 413 passed, 116 skipped; no lint findings

- [ ] **Step 10: Commit**

```bash
git add bmnews/gui/routes/watches.py bmnews/gui/templates/fragments/watches_view.html \
        bmnews/gui/templates/fragments/watch_list.html \
        bmnews/gui/templates/fragments/watch_poller.html \
        bmnews/gui/app.py bmnews/gui/templates/base.html \
        bmnews/gui/static/css/app.css tests/test_gui_notify.py
git commit -m "$(cat <<'EOF'
feat(gui): a Watches tab showing what each watch has queued

max_per_run caps a run at five papers and leaves the rest queued rather
than dropping them. Without a surface showing the remaining count, they
were dropped as far as a GUI-only user could tell.

The rows are built from parse_watches() with the counts joined onto
them, not from pending_counts() alone. A watch naming no configured
channel produces no reports at all and would otherwise vanish from the
pane; a watch that fails to parse is skipped with a log line nobody
reads; a disabled watch has counts worth seeing and no button worth
offering. All three now say what they are.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Delivery — `POST /watches/<name>/notify` and `notify-all`

**Files:**
- Modify: `bmnews/gui/routes/watches.py`
- Test: `tests/test_gui_notify.py`

**Interfaces:**
- Consumes: `jobs.start`, `jobs.progress`, `jobs.render_status_bar`, `jobs.wait_for_idle` (Task 1); `_build_rows` (Task 2).
- Produces: routes `watches.notify` and `watches.notify_all`; `_terminal(name, reports) -> dict[str, str]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gui_notify.py`:

```python
class TestDelivery:
    def test_notify_starts_a_batch_run(self, client, monkeypatch):
        calls = []

        def _run_notify(config, **kwargs):
            calls.append(kwargs)
            return [DeliveryReport(watch="melanoma", channel="mailbox", delivered=5, remaining=4)]

        monkeypatch.setattr("bmnews.notify.service.run_notify", _run_notify)

        resp = client.post("/watches/melanoma/notify")

        assert resp.status_code == 200
        assert jobs.wait_for_idle(5.0) is True
        assert calls == [{"watch": "melanoma", "drain": False, "on_progress": jobs.progress}]
        assert jobs.status()["status"] == "success"
        assert "5 paper(s) notified" in jobs.status()["message"]

    def test_notify_all_drains(self, client, monkeypatch):
        calls = []

        def _run_notify(config, **kwargs):
            calls.append(kwargs["drain"])
            return [DeliveryReport(watch="melanoma", channel="mailbox", delivered=9)]

        monkeypatch.setattr("bmnews.notify.service.run_notify", _run_notify)

        client.post("/watches/melanoma/notify-all")

        assert jobs.wait_for_idle(5.0) is True
        assert calls == [True]

    def test_a_failed_delivery_reports_as_an_error(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.run_notify",
            lambda config, **kwargs: [
                DeliveryReport(watch="melanoma", channel="mailbox", failed=5, remaining=9)
            ],
        )

        client.post("/watches/melanoma/notify")

        assert jobs.wait_for_idle(5.0) is True
        assert jobs.status()["status"] == "error"
        assert "stay queued" in jobs.status()["message"]

    def test_a_partial_failure_reports_both(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.run_notify",
            lambda config, **kwargs: [
                DeliveryReport(watch="melanoma", channel="mailbox", delivered=5),
                DeliveryReport(watch="melanoma", channel="chatroom", failed=5),
            ],
        )

        client.post("/watches/melanoma/notify")

        assert jobs.wait_for_idle(5.0) is True
        assert jobs.status()["status"] == "error"
        assert "5 paper(s) notified" in jobs.status()["message"]
        assert "5 failed" in jobs.status()["message"]

    def test_a_raising_run_notify_reports_and_frees_the_lock(self, client, monkeypatch):
        def _boom(config, **kwargs):
            raise RuntimeError("smtp down")

        monkeypatch.setattr("bmnews.notify.service.run_notify", _boom)

        client.post("/watches/melanoma/notify")

        assert jobs.wait_for_idle(5.0) is True
        assert jobs.status()["status"] == "error"
        assert "smtp down" in jobs.status()["message"]
        assert jobs.running() is False

    def test_a_second_delivery_is_refused_while_one_runs(self, client, monkeypatch):
        import threading

        release = threading.Event()
        started = threading.Event()
        runs = []

        def _slow(config, **kwargs):
            runs.append(kwargs["watch"])
            started.set()
            release.wait(5.0)
            return []

        monkeypatch.setattr("bmnews.notify.service.run_notify", _slow)

        client.post("/watches/melanoma/notify")
        started.wait(5.0)
        resp = client.post("/watches/melanoma/notify")

        assert resp.status_code == 200
        release.set()
        assert jobs.wait_for_idle(5.0) is True
        assert runs == ["melanoma"]

    def test_an_unknown_watch_is_a_404(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.run_notify",
            lambda config, **kwargs: pytest.fail("must not be called"),
        )

        assert client.post("/watches/nosuchwatch/notify").status_code == 404
        assert client.post("/watches/nosuchwatch/notify-all").status_code == 404

    def test_the_response_attaches_the_completion_poller(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.run_notify", lambda config, **kwargs: [])

        body = client.post("/watches/melanoma/notify").data.decode()

        assert jobs.wait_for_idle(5.0) is True
        assert 'id="watch-poller"' in body
        assert 'hx-swap-oob="innerHTML"' in body
        assert 'hx-get="/watches/rows"' in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_gui_notify.py::TestDelivery -q`
Expected: FAIL — the POST routes 404

- [ ] **Step 3: Add the routes**

Append to `bmnews/gui/routes/watches.py`, after `watches_page()`:

```python
@watches_bp.route("/watches/<path:name>/notify", methods=["POST"])
def notify(name: str) -> str:
    """Deliver one batch — the watch's ``max_per_run`` — in the background.

    Args:
        name: The watch's config table name.

    Returns:
        The ``status_bar`` fragment, plus an OOB swap attaching the completion
        poller.
    """
    return _start_delivery(name, drain=False)


@watches_bp.route("/watches/<path:name>/notify-all", methods=["POST"])
def notify_all(name: str) -> str:
    """Deliver every pending match for a watch, in the background.

    Args:
        name: The watch's config table name.

    Returns:
        The ``status_bar`` fragment, plus an OOB swap attaching the completion
        poller.
    """
    return _start_delivery(name, drain=True)
```

and, in the internals section:

```python
def _start_delivery(name: str, *, drain: bool) -> str:
    """Start one watch's delivery in the background and report it.

    A ``path`` converter carries *name* because a watch is named by its config
    table heading, which may contain a slash; ``string`` would 404 on one and
    the button would look broken for a reason nobody could see.
    """
    from flask import Flask, abort

    from bmnews.gui import jobs
    from bmnews.notify.service import run_notify
    from bmnews.notify.watches import parse_watches

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app: Flask = current_app._get_current_object()

    if name not in parse_watches(config.notifications.watches or {}):
        abort(404)

    def _run() -> None:
        with app.app_context():
            reports = run_notify(config, watch=name, drain=drain, on_progress=jobs.progress)
        jobs.status().update(running=False, **_terminal(name, reports))

    jobs.start(
        message=f"Notifying {name}...",
        target=_run,
        error_label=f"Notification error for {name}",
    )
    return jobs.render_status_bar() + _oob_poller()


def _terminal(name: str, reports: list) -> dict[str, str]:
    """Turn a run's reports into the status line it ends on.

    A run whose deliveries all failed has done nothing that was asked for, so
    it reports as an error rather than as a quiet success — the distinction
    ``bmnews notify`` already makes on the command line. Failed papers stay in
    the derived queue and retry, which is worth saying in the same breath.
    """
    delivered = sum(report.delivered for report in reports)
    failed = sum(report.failed for report in reports)

    if failed and not delivered:
        return {
            "message": f"{name}: delivery failed — {failed} paper(s) stay queued",
            "status": "error",
        }
    if failed:
        return {
            "message": (
                f"{name}: {delivered} paper(s) notified, "
                f"{failed} failed and stay queued"
            ),
            "status": "error",
        }
    if delivered:
        return {"message": f"{name}: {delivered} paper(s) notified", "status": "success"}
    return {"message": f"{name}: nothing to notify", "status": "success"}


def _oob_poller() -> str:
    """An OOB swap putting the completion poller into its slot.

    The poller lives in ``#watch-poller`` rather than inside ``#watch-list``
    so that starting it costs nothing: re-rendering the rows here would mean a
    full scan per (watch, channel) pair at the very moment the delivery job
    starts changing the numbers it would report.
    """
    poller = render_template("fragments/watch_poller.html")
    return f'<div id="watch-poller" hx-swap-oob="innerHTML">{poller}</div>'
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_gui_notify.py -q`
Expected: PASS (17 tests)

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest tests/ -q && uv run ruff check bmnews/ tests/ && uv run ruff format --check bmnews/ tests/`
Expected: 421 passed, 116 skipped; no lint findings

- [ ] **Step 6: Commit**

```bash
git add bmnews/gui/routes/watches.py tests/test_gui_notify.py
git commit -m "$(cat <<'EOF'
feat(gui): deliver a batch or drain a watch from the pane

Both buttons post to the watch, not to a (watch, channel) pair: the
queues are per channel and run_notify already does the right thing per
channel, so a watch with email pending and Matrix exhausted delivers
email only, and a retry after a failed email retries just the email.

A run whose deliveries all failed reports as an error rather than a
quiet success, matching what `bmnews notify` says on the command line —
the papers stay queued and retry, and a status bar that cannot tell the
difference is one that never reports it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Refresh — `GET /watches/rows`

**Files:**
- Modify: `bmnews/gui/routes/watches.py`
- Test: `tests/test_gui_notify.py`

**Interfaces:**
- Consumes: `jobs.running` (Task 1), `_build_rows` (Task 2), `_oob_poller` (Task 3).
- Produces: route `watches.rows`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gui_notify.py`:

```python
class TestRefresh:
    def test_204_while_a_job_runs_and_no_scan_is_performed(self, client, monkeypatch):
        scans = []

        def _counts(config):
            scans.append(1)
            return [report()]

        monkeypatch.setattr("bmnews.notify.service.pending_counts", _counts)
        jobs.status()["running"] = True

        resp = client.get("/watches/rows")

        assert resp.status_code == 204
        assert resp.data == b""
        # The scan a refresh costs is the thing the 204 exists to avoid.
        assert scans == []

    def test_idle_returns_rows_and_retires_the_poller(self, client, monkeypatch):
        monkeypatch.setattr(
            "bmnews.notify.service.pending_counts", lambda config: [report(remaining=4)]
        )

        resp = client.get("/watches/rows")
        body = resp.data.decode()

        assert resp.status_code == 200
        assert "melanoma" in body
        assert ">4<" in body
        # The poller's slot is emptied, which is what stops the polling.
        assert '<div id="watch-poller" hx-swap-oob="innerHTML"></div>' in body
        assert 'hx-get="/watches/rows"' not in body

    def test_the_pane_carries_the_poller_when_opened_during_a_run(self, client, monkeypatch):
        monkeypatch.setattr("bmnews.notify.service.pending_counts", lambda config: [report()])
        jobs.status()["running"] = True

        body = client.get("/watches").data.decode()

        assert 'hx-get="/watches/rows"' in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_gui_notify.py::TestRefresh -q`
Expected: FAIL — `/watches/rows` 404s (the third test may pass already; the first two must fail)

- [ ] **Step 3: Add the route**

Append to `bmnews/gui/routes/watches.py`, after `notify_all()`:

```python
@watches_bp.route("/watches/rows")
def rows() -> Response | str:
    """Re-render the rows once the running job has finished.

    Returns **204 No Content** while a job is in flight. htmx does not swap on
    a 204, so the poller that asked survives to ask again and no scan is
    performed. That matters: a refresh is a full pass over every candidate for
    every ``(watch, channel)`` pair, which is worth paying once when the counts
    have settled and not every two seconds while they are still moving.

    ``jobs.running()`` is global rather than per-job, so a pipeline run started
    while the poller is alive holds it here until that finishes too. That is
    wanted — the pipeline's own NOTIFY stage delivers, and scoring moves papers
    into and out of the queue.

    Returns:
        204 while a job runs; otherwise the ``watch_list`` fragment followed by
        an OOB swap emptying the poller's slot, which stops the polling.
    """
    from bmnews.gui import jobs

    if jobs.running():
        return Response(status=204)

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    watch_rows, _ = _build_rows(config)
    return (
        render_template("fragments/watch_list.html", rows=watch_rows)
        + '<div id="watch-poller" hx-swap-oob="innerHTML"></div>'
    )
```

Extend the module's Flask import to bring in `Response`:

```python
from flask import Blueprint, Response, current_app, render_template
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_gui_notify.py -q`
Expected: PASS (20 tests)

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest tests/ -q && uv run ruff check bmnews/ tests/ && uv run ruff format --check bmnews/ tests/`
Expected: 424 passed, 116 skipped; no lint findings

- [ ] **Step 6: Commit**

```bash
git add bmnews/gui/routes/watches.py tests/test_gui_notify.py
git commit -m "$(cat <<'EOF'
feat(gui): refresh the watch counts once, when delivery finishes

A refresh is a full collect_matches() scan per (watch, channel) pair, so
polling it live would run several full scans every two seconds against
the database the delivery job is writing to. The poller instead gets a
204 while a job runs — htmx does not swap on a 204, so it survives to
ask again at no cost — and the first idle answer both refreshes the
counts and empties the poller's slot, which stops the polling.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Documentation

**Files:**
- Modify: `bmnews/gui/CLAUDE.md`, `CLAUDE.md`, `docs/user/usage.md`, `docs/dev/architecture.md`, `HANDOVER.md`
- Modify: `docs/plans/2026-07-29-gui-watches-pane-design.md` (status line)

No test step: documentation only.

- [ ] **Step 1: `bmnews/gui/CLAUDE.md`**

Add to the routes table, after the `/pipeline/status` row:

```markdown
| GET | `/watches` | Watches pane — per-channel delivered/matching/remaining |
| POST | `/watches/<name>/notify` | Deliver one batch (the watch's `max_per_run`) |
| POST | `/watches/<name>/notify-all` | Drain a watch's queue |
| GET | `/watches/rows` | Refresh the counts (204 while a job runs) |
```

Add to **Key features**:

```markdown
- **Watches pane** — read-only view of each configured watch with its per-channel
  `delivered / matching / remaining`, plus buttons to deliver one batch or drain the
  queue. Rows are built from `parse_watches()` with the counts joined onto them, so a
  watch that names no configured channel, or that fails to parse, is still shown rather
  than silently absent. Creating and editing watches stays in `config.toml`
- **One background job** — `gui/jobs.py` owns the lock, status and daemon thread that
  the pipeline routes and the watches pane share, so a delivery cannot race a scoring run
```

- [ ] **Step 2: `CLAUDE.md`**

In the GUI directory tree, add under `gui/`:

```
├── jobs.py          # The one background job: lock, status, thread, status-bar fragment
```

and under `routes/`:

```
│   └── watches.py   # Watches pane: counts, delivery, refresh
```

and under `templates/fragments/`, note the three new fragments in the existing comment.

In the **Notifications** section, replace:

> Surfaces: the `bmnews notify` CLI (`--watch`, `--count`, `--all`, `--dry-run`, `--list`) and the NOTIFY stage of `run_pipeline()`. **Still to come**: the GUI watches pane. Everything else is implemented.

with:

> Surfaces: the `bmnews notify` CLI (`--watch`, `--count`, `--all`, `--dry-run`, `--list`), the NOTIFY stage of `run_pipeline()`, and the GUI watches pane (`/watches`). The pane monitors and delivers; watches are still created and edited in `config.toml`.

Add to the test-file table:

```markdown
| `test_gui_jobs.py` | The shared background job — refusal while one runs without clobbering its progress line, a raising target freeing the lock, a target that forgets to clear `running` |
| `test_gui_notify.py` | The watches pane — the count join, the three configurations that produce no counts (unresolved channel, unparseable watch, disabled watch), delivery and drain, failed-delivery reporting, 404 on an unknown watch, and the 204-while-running refresh |
```

- [ ] **Step 3: `docs/user/usage.md`**

Add a section documenting the pane, next to the existing notification CLI documentation:

```markdown
### The Watches tab

The **Watches** tab lists every watch in your config with, per channel:

| Column | Meaning |
|---|---|
| Delivered | Papers successfully sent for this watch over this channel, ever |
| Matching | Papers the watch matches in total, delivered ones included |
| Remaining | Papers still queued. A failed delivery is counted here — it stays queued and is retried |

**Notify N more** delivers one batch (N is the watch's `max_per_run`); **Notify
all remaining** drains the queue. Delivery runs in the background and reports in
the status bar at the bottom of the window; the counts refresh once it finishes.

Watches are created and edited in `~/.bmnews/config.toml` — the tab does not edit
them. A watch shown as *disabled*, or one reported as naming no configured
channel, is not delivering anything; a watch listed as unreadable failed
validation and is being skipped entirely, with the reason in the log.
```

- [ ] **Step 4: `docs/dev/architecture.md`**

Update the GUI section to mention `gui/jobs.py` as the shared background-job
owner and `routes/watches.py` as the fourth blueprint. Leave the `papers` table
description alone — that drift is [issue #11](https://github.com/hherb/BioMedicalNews/issues/11) and is out of scope here.

- [ ] **Step 5: `HANDOVER.md`**

In the state table, change the notification service row from
"**Done except the GUI watches pane**" to "**Done.**", and rewrite the
"Still to do: the GUI watches pane" paragraph in the notification section into a
short record of what shipped, keeping the three invariants that follow it. Note
the new largest open item is `bmlib.transparency`. Keep the file under 500 lines.

- [ ] **Step 6: Design doc status**

In `docs/plans/2026-07-29-gui-watches-pane-design.md`, change the status line to:

```markdown
**Status:** implemented.
```

- [ ] **Step 7: Verify and commit**

Run: `uv run pytest tests/ -q && uv run ruff check bmnews/ tests/ && uv run ruff format --check bmnews/ tests/`
Expected: 424 passed, 116 skipped; no lint findings

```bash
git add CLAUDE.md bmnews/gui/CLAUDE.md docs/ HANDOVER.md
git commit -m "$(cat <<'EOF'
docs: the watches pane, and the shared GUI background job

Records the pane as the notification service's last surface, and
gui/jobs.py as the single lock the pipeline routes and the pane share.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage.** Every section of the design has a task: `gui/jobs.py` extraction (Task 1); the pane, the three must-not-hide cases and the tab (Task 2); the two delivery routes and the `<path:>` converter (Task 3); the 204 refresh and the poller slot (Task 4); the documentation list (Task 5). The design's "what the numbers mean" section is realised as the `ChannelRow` docstring and the usage.md table.

**Poller placement.** The poller lives in its own `#watch-poller` slot rather
than among the rows, so the delivery POST does not have to re-render them — that
would have meant a full scan per pair at the moment the job starts changing the
numbers. The design document was corrected to match before this plan was
committed; the two agree.

**Placeholders.** None: every step carries the code it needs.

**Type consistency.** `ChannelRow.delivered` is fed from `DeliveryReport.sent_total`
(the report's field for "ever delivered"), not from `DeliveryReport.delivered`
(which is "delivered by this run" and is always 0 from `pending_counts`). The
`report()` test helper sets only the fields `pending_counts` fills.
`jobs.status()` is used consistently as a mutable dict in every task.
