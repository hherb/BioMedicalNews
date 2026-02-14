"""Database schema initialization and migration.

Delegates DDL to the versioned migration system in ``bmnews.db.migrations``.
The ``init_db`` function is called on every connection open and is safe to
call multiple times (idempotent).
"""

from __future__ import annotations

import logging
from typing import Any

from bmlib.db import connect_sqlite, connect_postgresql, run_migrations

from bmnews.db.migrations import MIGRATIONS

logger = logging.getLogger(__name__)


def init_db(conn: Any) -> None:
    """Create or migrate the database schema.

    Applies any pending migrations from ``MIGRATIONS``. Safe to call
    repeatedly — already-applied migrations are skipped.
    """
    applied = run_migrations(conn, MIGRATIONS)
    if applied:
        logger.info("Database schema initialized (%d migration(s) applied)", applied)


def open_db(config) -> Any:
    """Open a database connection from an AppConfig's database section.

    Returns a DB-API connection (sqlite3 or psycopg2).
    """
    from bmnews.config import DatabaseConfig

    db: DatabaseConfig = config.database if hasattr(config, "database") else config

    if db.backend == "postgresql":
        if db.pg_dsn:
            conn = connect_postgresql(dsn=db.pg_dsn)
        else:
            conn = connect_postgresql(
                host=db.pg_host,
                port=db.pg_port,
                database=db.pg_database,
                user=db.pg_user,
                password=db.pg_password,
            )
    else:
        conn = connect_sqlite(db.sqlite_path)

    return conn
