"""Desktop GUI launcher — opens pywebview window with Flask backend."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any

from bmnews import __version__
from bmnews.config import DEFAULT_CONFIG_DIR, AppConfig
from bmnews.constants import (
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    MIN_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    SERVER_POLL_INTERVAL_SECONDS,
    SERVER_START_TIMEOUT_SECONDS,
)
from bmnews.db.schema import init_db, open_db

logger = logging.getLogger(__name__)

_WINDOW_STATE_PATH = DEFAULT_CONFIG_DIR / "window_state.json"
_DEFAULT_GEOMETRY: dict[str, int | None] = {
    "x": None,
    "y": None,
    "width": DEFAULT_WINDOW_WIDTH,
    "height": DEFAULT_WINDOW_HEIGHT,
}


def _position_on_screen(x: int | None, y: int | None) -> bool:
    """Check whether (x, y) falls within any connected display."""
    if x is None or y is None:
        return False
    try:
        from AppKit import NSScreen  # type: ignore[import-untyped]

        for screen in NSScreen.screens():
            f = screen.frame()
            if (
                f.origin.x <= x <= f.origin.x + f.size.width
                and f.origin.y <= y <= f.origin.y + f.size.height
            ):
                return True
    except Exception:
        # AppKit unavailable — skip validation and trust the values
        return True
    return False


def _load_window_state() -> dict:
    """Load saved window geometry, falling back to defaults."""
    try:
        if _WINDOW_STATE_PATH.exists():
            data = json.loads(_WINDOW_STATE_PATH.read_text(encoding="utf-8"))
            geo = {**_DEFAULT_GEOMETRY, **data}
            # Discard position if it would land off-screen (e.g. detached monitor)
            if not _position_on_screen(geo.get("x"), geo.get("y")):
                geo["x"] = None
                geo["y"] = None
            return geo
    except Exception:
        logger.debug("Could not load window state, using defaults")
    return dict(_DEFAULT_GEOMETRY)


def _save_window_state(window: Any) -> None:
    """Persist current window geometry to disk."""
    try:
        state = {"x": window.x, "y": window.y, "width": window.width, "height": window.height}
        _WINDOW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WINDOW_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        logger.debug("Could not save window state")


def _find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float) -> bool:
    """Block until the local HTTP server accepts a connection.

    Args:
        port: Port the Flask server was told to listen on.
        timeout: Maximum time to wait, in seconds.

    Returns:
        True if the server became reachable within *timeout*, else False.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(SERVER_POLL_INTERVAL_SECONDS)
    return False


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

    def run_server() -> None:
        """Serve the Flask app on the chosen port (blocks until shutdown)."""
        app.run(host="127.0.0.1", port=port, use_reloader=False, threaded=True)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Probe the socket rather than signalling before app.run(): setting an
    # Event on the first line of the thread returns immediately and proves
    # nothing about whether the server is actually listening yet.
    if not _wait_for_server(port, SERVER_START_TIMEOUT_SECONDS):
        logger.warning(
            "Flask server not reachable on port %d after %.1fs — opening the window anyway",
            port,
            SERVER_START_TIMEOUT_SECONDS,
        )

    # Open the native window with saved geometry
    geo = _load_window_state()
    kwargs: dict[str, Any] = {
        "width": geo["width"],
        "height": geo["height"],
        "min_size": (MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT),
    }
    if geo["x"] is not None and geo["y"] is not None:
        kwargs["x"] = geo["x"]
        kwargs["y"] = geo["y"]

    window = webview.create_window(
        f"Bio-Medical News - Version {__version__}",
        f"http://127.0.0.1:{port}",
        **kwargs,
    )

    def _on_closing() -> bool:
        """Persist window geometry, then allow the close to proceed."""
        _save_window_state(window)
        return True

    window.events.closing += _on_closing
    try:
        webview.start()
    finally:
        # Always release the DB handle, even if the webview loop raises.
        conn.close()
        logger.info("GUI closed")
