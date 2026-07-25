"""Backend detection for the two databases bmlib can hand us.

``bmlib.db`` deliberately stays transparent: it executes whatever SQL it is
given, so the caller writes backend-appropriate DDL and placeholders.  These
two helpers are that caller-side detail, kept in one place so the schema and
the queries cannot disagree about which backend they are talking to.
"""

from __future__ import annotations

from typing import Any

__all__ = ["is_sqlite", "placeholder"]


def is_sqlite(conn: Any) -> bool:
    """Return True if *conn* is a sqlite3 connection (rather than psycopg2)."""
    return "sqlite3" in type(conn).__module__


def placeholder(conn: Any) -> str:
    """Return the parameter placeholder this connection's driver expects."""
    return "?" if is_sqlite(conn) else "%s"
