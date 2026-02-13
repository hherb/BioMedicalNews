"""Database schema definitions and initialization.

All SQL DDL lives here. Backend-aware: uses ``?`` for SQLite params.
"""

from __future__ import annotations

import logging
from typing import Any

from bmlib.db import connect_sqlite, connect_postgresql, create_tables, table_exists

logger = logging.getLogger(__name__)

SCHEMA_SQLITE = """\
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doi TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '',
    abstract TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    published_date TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers (doi);
CREATE INDEX IF NOT EXISTS idx_papers_published ON papers (published_date);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers (source);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    relevance_score REAL NOT NULL DEFAULT 0.0,
    quality_score REAL NOT NULL DEFAULT 0.0,
    combined_score REAL NOT NULL DEFAULT 0.0,
    summary TEXT NOT NULL DEFAULT '',
    study_design TEXT NOT NULL DEFAULT '',
    quality_tier TEXT NOT NULL DEFAULT '',
    assessment_json TEXT NOT NULL DEFAULT '{}',
    scored_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(paper_id)
);

CREATE INDEX IF NOT EXISTS idx_scores_combined ON scores (combined_score);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    paper_count INTEGER NOT NULL DEFAULT 0,
    delivery_method TEXT NOT NULL DEFAULT 'stdout',
    status TEXT NOT NULL DEFAULT 'sent'
);

CREATE TABLE IF NOT EXISTS digest_papers (
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    PRIMARY KEY (digest_id, paper_id)
);
"""

SCHEMA_POSTGRESQL = """\
CREATE TABLE IF NOT EXISTS papers (
    id SERIAL PRIMARY KEY,
    doi TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '',
    abstract TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    published_date TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers (doi);
CREATE INDEX IF NOT EXISTS idx_papers_published ON papers (published_date);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers (source);

CREATE TABLE IF NOT EXISTS scores (
    id SERIAL PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    relevance_score REAL NOT NULL DEFAULT 0.0,
    quality_score REAL NOT NULL DEFAULT 0.0,
    combined_score REAL NOT NULL DEFAULT 0.0,
    summary TEXT NOT NULL DEFAULT '',
    study_design TEXT NOT NULL DEFAULT '',
    quality_tier TEXT NOT NULL DEFAULT '',
    assessment_json TEXT NOT NULL DEFAULT '{}',
    scored_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(paper_id)
);

CREATE INDEX IF NOT EXISTS idx_scores_combined ON scores (combined_score);

CREATE TABLE IF NOT EXISTS digests (
    id SERIAL PRIMARY KEY,
    sent_at TIMESTAMP NOT NULL DEFAULT NOW(),
    paper_count INTEGER NOT NULL DEFAULT 0,
    delivery_method TEXT NOT NULL DEFAULT 'stdout',
    status TEXT NOT NULL DEFAULT 'sent'
);

CREATE TABLE IF NOT EXISTS digest_papers (
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    PRIMARY KEY (digest_id, paper_id)
);
"""


def init_db(conn: Any) -> None:
    """Create all tables if they don't already exist."""
    module_name = type(conn).__module__
    if "sqlite3" in module_name:
        create_tables(conn, SCHEMA_SQLITE)
    else:
        create_tables(conn, SCHEMA_POSTGRESQL)
    logger.info("Database schema initialized")


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
