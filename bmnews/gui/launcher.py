"""Desktop GUI launcher — opens pywebview window with Flask backend."""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import Any

from bmnews import __version__
from bmnews.config import AppConfig, DEFAULT_CONFIG_DIR
from bmnews.db.schema import init_db, open_db

logger = logging.getLogger(__name__)

_WINDOW_STATE_PATH = DEFAULT_CONFIG_DIR / "window_state.json"
_DEFAULT_GEOMETRY = {"x": None, "y": None, "width": 1200, "height": 800}


def _load_window_state() -> dict:
    """Load saved window geometry, falling back to defaults."""
    try:
        if _WINDOW_STATE_PATH.exists():
            data = json.loads(_WINDOW_STATE_PATH.read_text(encoding="utf-8"))
            return {**_DEFAULT_GEOMETRY, **data}
    except Exception:
        logger.debug("Could not load window state, using defaults")
    return dict(_DEFAULT_GEOMETRY)


def _save_window_state(window: Any) -> None:
    """Persist current window geometry to disk."""
    try:
        state = {"x": window.x, "y": window.y,
                 "width": window.width, "height": window.height}
        _WINDOW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WINDOW_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        logger.debug("Could not save window state")


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

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

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

    # Open the native window with saved geometry
    geo = _load_window_state()
    kwargs: dict[str, Any] = {
        "width": geo["width"],
        "height": geo["height"],
        "min_size": (600, 400),
    }
    if geo["x"] is not None and geo["y"] is not None:
        kwargs["x"] = geo["x"]
        kwargs["y"] = geo["y"]

    window = webview.create_window(
        f"Bio-Medical News - Version {__version__}",
        f"http://127.0.0.1:{port}",
        **kwargs,
    )

    def _on_closing():
        _save_window_state(window)
        return True

    window.events.closing += _on_closing
    webview.start()

    # Cleanup
    conn.close()
    logger.info("GUI closed")
