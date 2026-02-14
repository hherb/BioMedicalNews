"""Pipeline execution routes."""

from __future__ import annotations

import logging
import threading

from flask import Blueprint, current_app, render_template

from bmnews.config import AppConfig

pipeline_bp = Blueprint("pipeline", __name__)
logger = logging.getLogger(__name__)

_pipeline_lock = threading.Lock()


@pipeline_bp.route("/pipeline/run", methods=["POST"])
def run():
    from bmnews.pipeline import run_pipeline

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]

    if not _pipeline_lock.acquire(blocking=False):
        return render_template("fragments/status_bar.html",
                               message="Pipeline already running...", status="busy")

    try:
        run_pipeline(config)
        message = "Pipeline complete — papers fetched, scored, and digested."
        status = "success"
    except Exception as e:
        logger.exception("Pipeline error")
        message = f"Pipeline error: {e}"
        status = "error"
    finally:
        _pipeline_lock.release()

    return render_template("fragments/status_bar.html", message=message, status=status)
