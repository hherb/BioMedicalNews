"""Desktop GUI launcher — opens pywebview window with Flask backend."""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from bmnews.config import AppConfig
from bmnews.db.schema import init_db, open_db

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_app(config: AppConfig) -> tuple[Any, Any]:
    """Create the Flask app and database connection."""
    from bmnews.gui.app import create_app

    conn = open_db(config)
    init_db(conn)
    app = create_app(config, conn)
    return app, conn


def launch(config: AppConfig, port: int | None = None) -> None:
    """Launch the desktop GUI.

    Args:
        config: Application configuration.
        port: Fixed port number. If None, a free port is chosen.
    """
    import webview

    if port is None:
        port = _find_free_port()

    app, conn = _build_app(config)

    # Start Flask in a daemon thread
    ready = threading.Event()

    def run_server():
        ready.set()
        app.run(host="127.0.0.1", port=port, use_reloader=False, threaded=True)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    ready.wait(timeout=5)

    # Open the native window
    window = webview.create_window(
        "BioMedical News",
        f"http://127.0.0.1:{port}",
        width=1200,
        height=800,
        min_size=(600, 400),
    )
    webview.start()

    # Cleanup
    conn.close()
    logger.info("GUI closed")
