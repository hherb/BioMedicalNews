"""Pipeline execution routes."""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from flask import Blueprint, Flask, current_app, render_template

from bmnews.config import AppConfig
from bmnews.constants import DEFAULT_PAGE_SIZE
from bmnews.db.operations import count_unscored_papers, get_paper_with_score

pipeline_bp = Blueprint("pipeline", __name__)
logger = logging.getLogger(__name__)

# Guards against two pipeline runs racing on the same database. Acquired in
# the request thread and released by the worker thread's finally block, which
# a plain (non-reentrant) Lock permits.
_pipeline_lock = threading.Lock()
_pipeline_status: dict[str, Any] = {
    "running": False,
    "message": "Ready",
    "status": "idle",
    "refresh_list": False,  # Signal the next status poll to reload #paper-list
}
# Paper IDs scored since last status poll, consumed on each poll. deque's
# append/popleft are atomic, so no extra locking is needed here.
_scored_paper_ids: deque[int] = deque()


def _on_progress(message: str) -> None:
    """Record the latest pipeline progress message for the status poller."""
    _pipeline_status["message"] = message


def _start_pipeline_thread(target_fn: Callable[[], None]) -> None:
    """Launch *target_fn* in a daemon thread.

    Args:
        target_fn: Callable that pushes its own app context and is responsible
            for releasing :data:`_pipeline_lock` when it finishes.
    """
    threading.Thread(target=target_fn, daemon=True).start()


@pipeline_bp.route("/pipeline/run", methods=["POST"])
def run() -> str:
    """Start a full fetch → store → score → digest run in the background.

    Returns:
        The ``status_bar`` HTMX fragment. If a run is already in flight the
        request is a no-op and the busy status is returned instead.
    """
    from bmnews.pipeline import run_pipeline

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app: Flask = current_app._get_current_object()

    if not _pipeline_lock.acquire(blocking=False):
        return render_template("fragments/status_bar.html",
                               message="Pipeline already running...", status="busy",
                               running=True)

    _pipeline_status.update(running=True, message="Starting pipeline...", status="busy")

    def _run() -> None:
        """Run the pipeline, then publish a terminal status."""

        def _progress_with_refresh(message: str) -> None:
            """Record progress and flag the list for reload once scoring starts."""
            _on_progress(message)
            # After storing completes, the paper list needs a full reload
            if "Scoring" in message and not _pipeline_status.get("refresh_list"):
                _pipeline_status["refresh_list"] = True

        try:
            with app.app_context():
                run_pipeline(
                    config,
                    on_progress=_progress_with_refresh,
                    on_scored=_scored_paper_ids.append,
                )
            _pipeline_status.update(
                running=False,
                message="Pipeline complete — papers fetched, scored, and digested.",
                status="success",
                refresh_list=True,
            )
        except Exception as e:
            logger.exception("Pipeline error")
            _pipeline_status.update(
                running=False, message=f"Pipeline error: {e}", status="error",
            )
        finally:
            _pipeline_lock.release()

    try:
        _start_pipeline_thread(_run)
    except RuntimeError as e:
        # The worker never ran, so its finally block will not release the lock.
        _pipeline_status.update(
            running=False, message=f"Could not start pipeline: {e}", status="error",
        )
        _pipeline_lock.release()
        raise

    return render_template("fragments/status_bar.html",
                           message="Starting pipeline...", status="busy",
                           running=True)


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

    if count == 0 or _pipeline_status["running"]:
        return render_template("fragments/status_bar.html",
                               message=_pipeline_status["message"],
                               status=_pipeline_status["status"],
                               running=_pipeline_status["running"])

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app: Flask = current_app._get_current_object()

    if not _pipeline_lock.acquire(blocking=False):
        return render_template("fragments/status_bar.html",
                               message="Pipeline already running...", status="busy",
                               running=True)

    _pipeline_status.update(
        running=True,
        message=f"Resuming scoring of {count} papers...",
        status="busy",
    )

    def _run() -> None:
        """Score the outstanding papers, then publish a terminal status."""
        try:
            with app.app_context():
                scored = run_score(
                    config,
                    on_progress=_on_progress,
                    on_scored=_scored_paper_ids.append,
                )
            msg = f"Resumed scoring complete — {scored} papers scored."
            _pipeline_status.update(running=False, message=msg, status="success")
        except Exception as e:
            logger.exception("Resume scoring error")
            _pipeline_status.update(
                running=False, message=f"Scoring error: {e}", status="error",
            )
        finally:
            _pipeline_lock.release()

    try:
        _start_pipeline_thread(_run)
    except RuntimeError as e:
        _pipeline_status.update(
            running=False, message=f"Could not start scoring: {e}", status="error",
        )
        _pipeline_lock.release()
        raise

    return render_template("fragments/status_bar.html",
                           message=f"Resuming scoring of {count} papers...",
                           status="busy", running=True)


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
    needs_list_refresh = _pipeline_status.get("refresh_list", False)
    if needs_list_refresh:
        _pipeline_status["refresh_list"] = False

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

    html = render_template(
        "fragments/status_bar.html",
        message=_pipeline_status["message"],
        status=_pipeline_status["status"],
        running=_pipeline_status["running"],
    )

    if needs_list_refresh:
        # Trigger a full paper list reload via OOB swap
        from bmnews.db.operations import get_papers_filtered
        papers, total = get_papers_filtered(
            conn, sort="date", limit=DEFAULT_PAGE_SIZE, offset=0, with_total=True,
        )
        list_html = render_template(
            "fragments/paper_list.html",
            papers=papers, total=total, offset=0, limit=DEFAULT_PAGE_SIZE,
            sort="date", source="", tier="", design="", search="",
        )
        html += f'<div id="paper-list" hx-swap-oob="innerHTML">{list_html}</div>'
    elif oob_cards:
        html += "\n".join(oob_cards)

    return html
