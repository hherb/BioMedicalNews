"""Pipeline execution routes."""

from __future__ import annotations

import logging
import threading
from collections import deque

from flask import Blueprint, current_app, render_template

from bmlib.db import fetch_scalar
from bmnews.config import AppConfig
from bmnews.db.operations import get_paper_with_score

pipeline_bp = Blueprint("pipeline", __name__)
logger = logging.getLogger(__name__)

_pipeline_lock = threading.Lock()
_pipeline_status: dict = {
    "running": False,
    "message": "Ready",
    "status": "idle",
}
# Paper IDs scored since last status poll, consumed on each poll.
_scored_paper_ids: deque[int] = deque()


def _on_progress(message: str) -> None:
    _pipeline_status["message"] = message


def _start_pipeline_thread(app, target_fn):
    """Launch *target_fn* in a daemon thread with app context."""
    threading.Thread(target=target_fn, daemon=True).start()


@pipeline_bp.route("/pipeline/run", methods=["POST"])
def run():
    from bmnews.pipeline import run_pipeline

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app = current_app._get_current_object()

    if not _pipeline_lock.acquire(blocking=False):
        return render_template("fragments/status_bar.html",
                               message="Pipeline already running...", status="busy",
                               running=True)

    _pipeline_status.update(running=True, message="Starting pipeline...", status="busy")

    def _run():
        try:
            with app.app_context():
                run_pipeline(
                    config,
                    on_progress=_on_progress,
                    on_scored=_scored_paper_ids.append,
                )
            _pipeline_status.update(
                running=False,
                message="Pipeline complete — papers fetched, scored, and digested.",
                status="success",
            )
        except Exception as e:
            logger.exception("Pipeline error")
            _pipeline_status.update(
                running=False, message=f"Pipeline error: {e}", status="error",
            )
        finally:
            _pipeline_lock.release()

    _start_pipeline_thread(app, _run)

    return render_template("fragments/status_bar.html",
                           message="Starting pipeline...", status="busy",
                           running=True)


@pipeline_bp.route("/pipeline/resume", methods=["POST"])
def resume():
    """Auto-resume scoring for papers left unscored from a previous session."""
    from bmnews.pipeline import run_score

    conn = current_app.config["BMNEWS_DB"]
    count = fetch_scalar(
        conn,
        "SELECT COUNT(*) FROM papers p LEFT JOIN scores s ON s.paper_id = p.id "
        "WHERE s.id IS NULL",
    ) or 0

    if count == 0 or _pipeline_status["running"]:
        return render_template("fragments/status_bar.html",
                               message=_pipeline_status["message"],
                               status=_pipeline_status["status"],
                               running=_pipeline_status["running"])

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app = current_app._get_current_object()

    if not _pipeline_lock.acquire(blocking=False):
        return render_template("fragments/status_bar.html",
                               message="Pipeline already running...", status="busy",
                               running=True)

    _pipeline_status.update(
        running=True,
        message=f"Resuming scoring of {count} papers...",
        status="busy",
    )

    def _run():
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

    _start_pipeline_thread(app, _run)

    return render_template("fragments/status_bar.html",
                           message=f"Resuming scoring of {count} papers...",
                           status="busy", running=True)


@pipeline_bp.route("/pipeline/status")
def status():
    conn = current_app.config["BMNEWS_DB"]

    # Drain any paper IDs scored since last poll and render OOB card updates
    oob_cards: list[str] = []
    while _scored_paper_ids:
        pid = _scored_paper_ids.popleft()
        paper = get_paper_with_score(conn, pid)
        if paper:
            card_html = render_template("fragments/paper_card.html", paper=paper)
            # Inject hx-swap-oob so HTMX replaces the existing card in-place
            card_html = card_html.replace(
                f'id="paper-card-{pid}"',
                f'id="paper-card-{pid}" hx-swap-oob="outerHTML"',
                1,
            )
            oob_cards.append(card_html)

    html = render_template(
        "fragments/status_bar.html",
        message=_pipeline_status["message"],
        status=_pipeline_status["status"],
        running=_pipeline_status["running"],
    )

    if oob_cards:
        html += "\n".join(oob_cards)

    return html
