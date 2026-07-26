"""Backend selection for the database tests.

bmnews runs on SQLite or PostgreSQL, and the difference is not cosmetic:
``_source_filter`` unnests a JSON array with ``json_each`` on one backend and
``json_array_elements_text`` on the other, keyword search picks ``LIKE`` or
``ILIKE``, timestamps default to ``datetime('now')`` or ``NOW()``, and every
migration carries a pair of DDL strings. Testing only SQLite leaves all of
that unexercised, which is worse than no coverage at all once CI is green:
the checkmark reads as "both backends work".

So the database tests run once per backend. SQLite is always available.
PostgreSQL needs a live server, so it runs only when ``BMNEWS_TEST_PG_DSN``
names one — CI sets it from a ``services: postgres`` container, and a
developer without one gets a skip rather than a failure.

The active backend is process state rather than a parameter because the tests
build databases through a bare ``_db()`` helper called from sixty-odd places;
threading an argument through all of them would be a large diff that changed
no behaviour. The ``db_backend`` fixture in ``conftest.py`` is the only writer,
and it always hands the state back afterwards.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from bmlib.db import connect_postgresql, connect_sqlite

#: Names a running PostgreSQL server to test against, e.g.
#: ``postgresql://bmnews:bmnews@localhost:5432/bmnews_test``. Unset means the
#: PostgreSQL parameterisation is skipped.
PG_DSN_ENV = "BMNEWS_TEST_PG_DSN"

SQLITE = "sqlite"
POSTGRESQL = "postgresql"

# The backend the currently-running test is parameterised on. Only
# :func:`use_backend` writes it.
_active: str = SQLITE

# Every PostgreSQL connection handed out during the current test, paired with
# the schema created for it, so both can be torn down together.
_pg_open: list[tuple[Any, str]] = []
_pg_schema_counter = 0


def backend_params() -> list[Any]:
    """Return the pytest params for one run per supported backend.

    PostgreSQL is emitted either way so that a skipped run is visible in the
    test report — a parameterisation that silently disappears when a server is
    missing looks identical to one that was never written.
    """
    skip = pytest.mark.skip(reason=f"{PG_DSN_ENV} not set")
    marks = () if os.environ.get(PG_DSN_ENV) else (skip,)
    return [
        pytest.param(SQLITE, id=SQLITE),
        pytest.param(POSTGRESQL, id=POSTGRESQL, marks=marks),
    ]


def active_backend() -> str:
    """Return the backend the current test is running against."""
    return _active


@contextmanager
def use_backend(backend: str) -> Iterator[str]:
    """Make *backend* the target of :func:`new_db` for the duration of a test.

    On exit every PostgreSQL database handed out is dropped and its connection
    closed.
    """
    global _active
    previous = _active
    _active = backend
    try:
        yield backend
    finally:
        _drop_pg_schemas()
        _active = previous


def new_db() -> Any:
    """Open a connection to a fresh, empty database on the active backend.

    The result is unmigrated — the caller decides which migrations to apply,
    which is what lets the migration tests build a database at an older
    version.

    Calling this twice in one test yields two independent databases on either
    backend, so a test that needs to compare two databases keeps working.
    """
    if _active == SQLITE:
        return connect_sqlite(":memory:")
    return _new_pg_db()


def _new_pg_db() -> Any:
    """Open a PostgreSQL connection isolated in a schema of its own.

    A PostgreSQL server has no equivalent of ``:memory:``, and one database
    per test would be slow and cannot be created from inside a transaction.
    A schema per connection gives the same isolation cheaply: ``search_path``
    is per-session, so the connection sees only its own tables and two
    connections handed out in the same test cannot collide.
    """
    global _pg_schema_counter

    conn = connect_postgresql(dsn=os.environ[PG_DSN_ENV])
    _pg_schema_counter += 1
    schema = f"bmnews_test_{os.getpid()}_{_pg_schema_counter}"

    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA {schema}")
    cur.execute(f"SET search_path TO {schema}")
    conn.commit()

    _pg_open.append((conn, schema))
    return conn


def _drop_pg_schemas() -> None:
    """Drop every schema created during the test and close its connection.

    Each schema is dropped over the same connection that owns it. Dropping
    from a second session would block behind whatever transaction that
    connection left open — tests do not close their connections — and turn a
    teardown into a hang.
    """
    while _pg_open:
        conn, schema = _pg_open.pop()
        try:
            conn.rollback()
            cur = conn.cursor()
            cur.execute("SET search_path TO pg_catalog")
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
        finally:
            conn.close()
