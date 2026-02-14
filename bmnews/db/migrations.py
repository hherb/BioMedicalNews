"""Application-specific database migrations for bmnews.

Each migration is a function that receives a DB-API connection and
applies DDL.  Migrations use ``CREATE TABLE/INDEX IF NOT EXISTS`` so
they are safe to run against databases that already have the tables.

The migration runner (``bmlib.db.migrations``) tracks which versions
have been applied in a ``schema_version`` table.
"""

from __future__ import annotations

from typing import Any

from bmlib.db import Migration, create_tables


def _is_sqlite(conn: Any) -> bool:
    return "sqlite3" in type(conn).__module__


# ---------------------------------------------------------------------------
# Migration 1: initial schema
# ---------------------------------------------------------------------------

_M001_SQLITE = """\
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

_M001_POSTGRESQL = """\
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


def _m001_initial_schema(conn: Any) -> None:
    """Create the base tables: papers, scores, digests, digest_papers."""
    create_tables(conn, _M001_SQLITE if _is_sqlite(conn) else _M001_POSTGRESQL)


# ---------------------------------------------------------------------------
# Migration 2: paper_tags table
# ---------------------------------------------------------------------------

_M002_SQLITE = """\
CREATE TABLE IF NOT EXISTS paper_tags (
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    tag TEXT NOT NULL,
    PRIMARY KEY (paper_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_paper_tags_tag ON paper_tags (tag);
"""

_M002_POSTGRESQL = """\
CREATE TABLE IF NOT EXISTS paper_tags (
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    tag TEXT NOT NULL,
    PRIMARY KEY (paper_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_paper_tags_tag ON paper_tags (tag);
"""


def _m002_add_paper_tags(conn: Any) -> None:
    """Create the paper_tags table for per-paper interest tagging."""
    create_tables(conn, _M002_SQLITE if _is_sqlite(conn) else _M002_POSTGRESQL)


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

MIGRATIONS: list[Migration] = [
    Migration(1, "initial_schema", _m001_initial_schema),
    Migration(2, "add_paper_tags", _m002_add_paper_tags),
]
