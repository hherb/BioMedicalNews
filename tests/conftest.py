"""Shared pytest fixtures."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from tests.backends import backend_params, use_backend

logger = logging.getLogger(__name__)


def _reset_jobs() -> None:
    """Wait out any running GUI job, then force the shared state back to idle.

    A wait that times out means the worker is still holding ``jobs._lock``, so
    every later ``jobs.start()`` would return False and one slow test would
    cascade into a run of unrelated-looking failures. Prising the lock open
    from another thread is wrong in production and right here — the state is
    being discarded either way, and a plain ``Lock`` permits it.
    """
    try:
        # Imported here rather than at module scope: this fixture is autouse
        # for the whole suite, and Flask is the optional ``gui`` extra. In an
        # environment installed as ``.[dev]`` alone the GUI test modules fail
        # collection on their own imports and the rest of the suite still
        # runs — which it would not if this raised for every test.
        from bmnews.gui import jobs
    except ImportError:
        return

    if not jobs.wait_for_idle(5.0):
        # ``wait_for_idle()`` is ``not running()``, which also goes False for a
        # test that poked ``jobs.status()["running"] = True`` by hand — no
        # thread, no lock, nothing outlived anything. Only the lock actually
        # being held means a job is still out there.
        if jobs._lock.locked():
            logger.error("A GUI background job outlived its test — forcing the shared state idle")
            try:
                jobs._lock.release()
            except RuntimeError:  # Released between the check and the call.
                pass
    jobs.status().update(running=False, message="Ready", status="idle", refresh_list=False)


@pytest.fixture(autouse=True)
def idle_jobs() -> Iterator[None]:
    """Leave the GUI's module-level job state clean around every test.

    Autouse for the whole suite rather than one module: ``bmnews.gui.jobs``
    owns process state, and any test driving ``POST /pipeline/run`` or a
    watches delivery leaks a thread, a lock and a status line into whatever
    runs next.
    """
    _reset_jobs()
    yield
    _reset_jobs()


@pytest.fixture(params=backend_params())
def db_backend(request: pytest.FixtureRequest) -> Iterator[str]:
    """Run the requesting test once per supported database backend.

    A module opts in with ``pytestmark = pytest.mark.usefixtures("db_backend")``;
    every test in it is then parameterised, and the ``_db()`` helpers build
    their databases on whichever backend the current run selected. See
    ``tests/backends.py`` for why the selection is process state.
    """
    with use_backend(request.param) as backend:
        yield backend
