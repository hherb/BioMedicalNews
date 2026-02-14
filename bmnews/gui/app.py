"""Flask application factory for the BioMedicalNews GUI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask

from bmnews.config import AppConfig

logger = logging.getLogger(__name__)

GUI_DIR = Path(__file__).parent
TEMPLATES_DIR = GUI_DIR / "templates"
STATIC_DIR = GUI_DIR / "static"


def create_app(config: AppConfig, conn: Any) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=str(STATIC_DIR),
    )
    app.config["BMNEWS_CONFIG"] = config
    app.config["BMNEWS_DB"] = conn

    from bmnews.gui.routes.papers import papers_bp
    from bmnews.gui.routes.settings import settings_bp
    from bmnews.gui.routes.pipeline import pipeline_bp

    app.register_blueprint(papers_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(pipeline_bp)

    @app.route("/")
    def index():
        from flask import render_template
        return render_template("base.html")

    return app
