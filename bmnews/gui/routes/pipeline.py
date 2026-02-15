"""Pipeline execution routes."""

from __future__ import annotations

import logging
import threading

from flask import Blueprint, current_app, render_template

from bmnews.config import AppConfig

pipeline_bp = Blueprint("pipeline", __name__)
logger = logging.getLogger(__name__)

_pipeline_lock = threading.Lock()
_pipeline_status: dict = {"running": False, "message": "Ready", "status": "idle"}


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

    def _on_progress(message: str) -> None:
        _pipeline_status["message"] = message

    def _run():
        try:
            with app.app_context():
                run_pipeline(config, on_progress=_on_progress)
            _pipeline_status.update(
                running=False,
                message="Pipeline complete — papers fetched, scored, and digested.",
                status="success",
            )
        except Exception as e:
            logger.exception("Pipeline error")
            _pipeline_status.update(
                running=False, message=f"Pipeline error: {e}", status="error"
            )
        finally:
            _pipeline_lock.release()

    threading.Thread(target=_run, daemon=True).start()

    return render_template("fragments/status_bar.html",
                           message="Starting pipeline...", status="busy",
                           running=True)


@pipeline_bp.route("/pipeline/status")
def status():
    return render_template("fragments/status_bar.html",
                           message=_pipeline_status["message"],
                           status=_pipeline_status["status"],
                           running=_pipeline_status["running"])
