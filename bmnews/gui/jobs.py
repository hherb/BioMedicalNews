"""The single background job the GUI runs, and the status bar reporting it.

The pipeline routes and the watches pane both run work in a daemon thread
against the same database, and they must not overlap: a notification delivery
racing a scoring run would page through a queue that run is still changing.
There is therefore one lock and one status, owned here rather than by whichever
blueprint happened to need them first.

Rendering the status fragment lives here too. It is the job's presentation, and
two blueprints returning it means one definition or two that drift.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from flask import render_template

logger = logging.getLogger(__name__)

# Guards against two background jobs racing on the same database. Acquired in
# the request thread and released by the worker thread's finally block, which
# a plain (non-reentrant) Lock permits.
_lock = threading.Lock()
_status: dict[str, Any] = {
    "running": False,
    "message": "Ready",
    "status": "idle",
    "refresh_list": False,  # Signal the next status poll to reload #paper-list
}
_thread: threading.Thread | None = None


def status() -> dict[str, Any]:
    """The live status dict the status bar renders from.

    Returned rather than copied: callers publish a terminal status by updating
    it in place, which is what the worker threads do when they finish.
    """
    return _status


def running() -> bool:
    """Whether a background job is in flight."""
    return bool(_status["running"])


def progress(message: str) -> None:
    """Record the latest progress line for the status poller."""
    _status["message"] = message


def start(*, message: str, target: Callable[[], None], error_label: str) -> bool:
    """Run *target* in a daemon thread, unless another job holds the lock.

    Args:
        message: Busy message published while the job runs.
        target: The work. It pushes its own app context and publishes its own
            terminal success message; a failure is handled here.
        error_label: Prefix for the message published if *target* raises, e.g.
            ``"Pipeline error"``.

    Returns:
        True if the job started. False if one was already running, or if the
        thread could not be spawned at all. The caller does not have to tell
        those apart: in both cases :func:`status` already holds the message to
        show — the running job's own progress line, or the spawn failure.
    """
    global _thread

    if not _lock.acquire(blocking=False):
        # Deliberately no status update: the running job's progress line is
        # what the user needs to see, and overwriting it with "already running"
        # would replace live information with a truism.
        logger.debug("A background job is already running — refusing another")
        return False

    _status.update(running=True, message=message, status="busy")

    def _run() -> None:
        try:
            target()
        except Exception as exc:
            logger.exception("%s", error_label)
            _status.update(message=f"{error_label}: {exc}", status="error")
        finally:
            # A target that returns without publishing a terminal status would
            # otherwise leave the status bar spinning forever over no job.
            _status["running"] = False
            _lock.release()

    try:
        _thread = threading.Thread(target=_run, daemon=True)
        _thread.start()
    except RuntimeError as exc:
        # The worker never ran, so its finally block will not release the lock.
        logger.exception("Could not start a background job")
        _status.update(running=False, message=f"{error_label}: {exc}", status="error")
        _lock.release()
        return False

    return True


def render_status_bar() -> str:
    """Render the status-bar fragment from the current job status."""
    return render_template(
        "fragments/status_bar.html",
        message=_status["message"],
        status=_status["status"],
        running=_status["running"],
    )


def wait_for_idle(timeout: float = 5.0) -> bool:
    """Block until the running job finishes.

    Exists so tests can assert on a background job without sleep-polling for
    it, which is the only way a caller outside this module can know a daemon
    thread has finished.

    Args:
        timeout: Seconds to wait for the thread to end.

    Returns:
        True if no job is running when this returns.
    """
    thread = _thread
    if thread is not None:
        thread.join(timeout)
    return not running()
