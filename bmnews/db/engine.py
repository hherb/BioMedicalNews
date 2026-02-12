"""Database engine factory and session management."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from bmnews.config import AppConfig

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def _enable_sqlite_wal(dbapi_conn, _connection_record):
    """Enable WAL mode for SQLite for better concurrent access."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_engine(config: AppConfig) -> Engine:
    """Create and return the SQLAlchemy engine based on configuration."""
    global _engine, _session_factory

    url = config.database_url
    kwargs: dict = {}

    if config.database_backend == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10

    _engine = create_engine(url, echo=False, **kwargs)

    if config.database_backend == "sqlite":
        event.listen(_engine, "connect", _enable_sqlite_wal)

    if config.database_backend == "postgresql":
        try:
            from sqlalchemy import text

            with _engine.connect() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.commit()
                logger.info("pgvector extension enabled")
        except Exception:
            logger.warning("Could not enable pgvector extension — semantic search on PostgreSQL requires it")

    _session_factory = sessionmaker(bind=_engine)
    logger.info("Database engine initialised (%s)", config.database_backend)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database engine not initialised — call init_engine() first")
    return _engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Provide a transactional database session."""
    if _session_factory is None:
        raise RuntimeError("Database engine not initialised — call init_engine() first")
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
