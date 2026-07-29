"""Flask application factory for the BioMedicalNews GUI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask

from bmnews import __version__
from bmnews.config import AppConfig
from bmnews.constants import DEFAULT_CONTACT_EMAIL
from bmnews.gui.helpers import format_abstract_html

logger = logging.getLogger(__name__)

GUI_DIR = Path(__file__).parent
TEMPLATES_DIR = GUI_DIR / "templates"
STATIC_DIR = GUI_DIR / "static"


def create_app(config: AppConfig, conn: Any) -> Flask:
    """Build the GUI Flask application.

    Args:
        config: Application configuration, exposed to routes as
            ``app.config["BMNEWS_CONFIG"]``.
        conn: An open database connection shared by all requests. Because the
            embedded server is threaded, this must tolerate use from multiple
            threads (sqlite3 connections need ``check_same_thread=False``).

    Returns:
        The configured Flask app with all blueprints registered.
    """
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=str(STATIC_DIR),
    )
    app.config["BMNEWS_CONFIG"] = config
    app.config["BMNEWS_DB"] = conn
    app.config["BMNEWS_EMAIL"] = getattr(config.user, "email", "") or DEFAULT_CONTACT_EMAIL
    app.jinja_env.filters["format_abstract"] = format_abstract_html
    app.jinja_env.globals["app_version"] = __version__

    from bmnews.gui.routes.papers import papers_bp
    from bmnews.gui.routes.pipeline import pipeline_bp
    from bmnews.gui.routes.settings import settings_bp
    from bmnews.gui.routes.watches import watches_bp

    app.register_blueprint(papers_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(pipeline_bp)
    app.register_blueprint(watches_bp)

    @app.route("/")
    def index() -> str:
        """Serve the full shell, or just the papers view for HTMX requests."""
        from flask import render_template, request

        if request.headers.get("HX-Request"):
            return render_template("fragments/papers_view.html")
        return render_template("base.html")

    return app
