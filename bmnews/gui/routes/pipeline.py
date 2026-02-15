"""Pipeline execution routes."""

from __future__ import annotations

import logging
import threading

from flask import Blueprint, current_app, render_template

from bmlib.db import fetch_scalar
from bmnews.config import AppConfig

pipeline_bp = Blueprint("pipeline", __name__)
logger = logging.getLogger(__name__)

_pipeline_lock = threading.Lock()
_pipeline_status: dict = {
    "running": False,
    "message": "Ready",
    "status": "idle",
    "refresh_list": False,
}


@pipeline_bp.route("/pipeline/run", methods=["POST"])
def run():
    from bmnews.pipeline import run_pipeline

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app = current_app._get_current_object()

    if not _pipeline_lock.acquire(blocking=False):
        return render_template("fragments/status_bar.html",
                               message="Pipeline already running...", status="busy",
                               running=True)

    _pipeline_status.update(
        running=True, message="Starting pipeline...", status="busy",
        refresh_list=False,
    )

    def _on_progress(message: str) -> None:
        prev = _pipeline_status["message"]
        _pipeline_status["message"] = message
        # Flag a list refresh when papers become available or get updated:
        # - Storing phase completes (next message after "Storing" means store is done)
        # - Each scored paper: scores are incrementally saved
        if "Storing" in prev and "Storing" not in message:
            _pipeline_status["refresh_list"] = True
        elif "Scoring paper" in message:
            _pipeline_status["refresh_list"] = True

    def _run():
        try:
            with app.app_context():
                run_pipeline(config, on_progress=_on_progress)
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
                refresh_list=False,
            )
        finally:
            _pipeline_lock.release()

    threading.Thread(target=_run, daemon=True).start()

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
        refresh_list=False,
    )

    def _on_progress(message: str) -> None:
        prev = _pipeline_status["message"]
        _pipeline_status["message"] = message
        if "Scoring paper" in message:
            _pipeline_status["refresh_list"] = True

    def _run():
        try:
            with app.app_context():
                scored = run_score(config, on_progress=_on_progress)
            msg = f"Resumed scoring complete — {scored} papers scored."
            _pipeline_status.update(
                running=False, message=msg, status="success",
                refresh_list=True,
            )
        except Exception as e:
            logger.exception("Resume scoring error")
            _pipeline_status.update(
                running=False, message=f"Scoring error: {e}", status="error",
                refresh_list=False,
            )
        finally:
            _pipeline_lock.release()

    threading.Thread(target=_run, daemon=True).start()

    return render_template("fragments/status_bar.html",
                           message=f"Resuming scoring of {count} papers...",
                           status="busy", running=True)


@pipeline_bp.route("/pipeline/status")
def status():
    refresh = _pipeline_status["refresh_list"]
    if refresh:
        _pipeline_status["refresh_list"] = False
    return render_template("fragments/status_bar.html",
                           message=_pipeline_status["message"],
                           status=_pipeline_status["status"],
                           running=_pipeline_status["running"],
                           refresh_list=refresh)
