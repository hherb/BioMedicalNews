"""Pure-function database operations for bmnews.

All SQL lives here. Uses bmlib.db for execution.
Backend-aware: detects sqlite3 vs psycopg2 by connection module name.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bmlib.db import execute, fetch_one, fetch_all, fetch_scalar, transaction

logger = logging.getLogger(__name__)


def _placeholder(conn: Any) -> str:
    """Return the correct parameter placeholder for this connection."""
    return "?" if "sqlite3" in type(conn).__module__ else "%s"


# --- Papers ---


def paper_exists(conn: Any, doi: str) -> bool:
    """Check if a paper with this DOI already exists."""
    ph = _placeholder(conn)
    val = fetch_scalar(conn, f"SELECT 1 FROM papers WHERE doi = {ph}", (doi,))
    return val is not None


def upsert_paper(
    conn: Any,
    *,
    doi: str,
    title: str,
    authors: str = "",
    abstract: str = "",
    url: str = "",
    source: str = "",
    published_date: str = "",
    categories: str = "",
    metadata_json: str = "{}",
) -> int:
    """Insert or update a paper. Returns the paper id."""
    ph = _placeholder(conn)
    is_sqlite = "sqlite3" in type(conn).__module__

    if is_sqlite:
        sql = f"""
            INSERT INTO papers (doi, title, authors, abstract, url, source,
                               published_date, categories, metadata_json)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            ON CONFLICT(doi) DO UPDATE SET
                title = excluded.title,
                authors = excluded.authors,
                abstract = excluded.abstract,
                url = excluded.url,
                categories = excluded.categories,
                metadata_json = excluded.metadata_json
        """
    else:
        sql = f"""
            INSERT INTO papers (doi, title, authors, abstract, url, source,
                               published_date, categories, metadata_json)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            ON CONFLICT(doi) DO UPDATE SET
                title = EXCLUDED.title,
                authors = EXCLUDED.authors,
                abstract = EXCLUDED.abstract,
                url = EXCLUDED.url,
                categories = EXCLUDED.categories,
                metadata_json = EXCLUDED.metadata_json
            RETURNING id
        """

    params = (doi, title, authors, abstract, url, source,
              published_date, categories, metadata_json)

    with transaction(conn):
        cur = execute(conn, sql, params)
        if is_sqlite:
            return cur.lastrowid
        row = cur.fetchone()
        return row[0] if row else 0


def get_paper_by_doi(conn: Any, doi: str) -> dict | None:
    """Fetch a single paper by DOI. Returns dict or None."""
    ph = _placeholder(conn)
    row = fetch_one(conn, f"SELECT * FROM papers WHERE doi = {ph}", (doi,))
    return _row_to_dict(row) if row else None


def get_unscored_papers(conn: Any, limit: int = 100) -> list[dict]:
    """Get papers that haven't been scored yet."""
    ph = _placeholder(conn)
    rows = fetch_all(
        conn,
        f"""
        SELECT p.* FROM papers p
        LEFT JOIN scores s ON s.paper_id = p.id
        WHERE s.id IS NULL
        ORDER BY p.fetched_at DESC
        LIMIT {ph}
        """,
        (limit,),
    )
    return [_row_to_dict(r) for r in rows]


# --- Scores ---


def save_score(
    conn: Any,
    *,
    paper_id: int,
    relevance_score: float = 0.0,
    quality_score: float = 0.0,
    combined_score: float = 0.0,
    summary: str = "",
    study_design: str = "",
    quality_tier: str = "",
    assessment_json: str = "{}",
) -> None:
    """Insert or update a score for a paper."""
    ph = _placeholder(conn)
    is_sqlite = "sqlite3" in type(conn).__module__

    if is_sqlite:
        sql = f"""
            INSERT INTO scores (paper_id, relevance_score, quality_score,
                               combined_score, summary, study_design,
                               quality_tier, assessment_json)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            ON CONFLICT(paper_id) DO UPDATE SET
                relevance_score = excluded.relevance_score,
                quality_score = excluded.quality_score,
                combined_score = excluded.combined_score,
                summary = excluded.summary,
                study_design = excluded.study_design,
                quality_tier = excluded.quality_tier,
                assessment_json = excluded.assessment_json,
                scored_at = datetime('now')
        """
    else:
        sql = f"""
            INSERT INTO scores (paper_id, relevance_score, quality_score,
                               combined_score, summary, study_design,
                               quality_tier, assessment_json)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            ON CONFLICT(paper_id) DO UPDATE SET
                relevance_score = EXCLUDED.relevance_score,
                quality_score = EXCLUDED.quality_score,
                combined_score = EXCLUDED.combined_score,
                summary = EXCLUDED.summary,
                study_design = EXCLUDED.study_design,
                quality_tier = EXCLUDED.quality_tier,
                assessment_json = EXCLUDED.assessment_json,
                scored_at = NOW()
        """

    params = (paper_id, relevance_score, quality_score, combined_score,
              summary, study_design, quality_tier, assessment_json)

    with transaction(conn):
        execute(conn, sql, params)


def get_scored_papers(
    conn: Any, min_combined: float = 0.0, limit: int = 100,
) -> list[dict]:
    """Get papers with scores above threshold, ordered by score."""
    ph = _placeholder(conn)
    rows = fetch_all(
        conn,
        f"""
        SELECT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier, s.assessment_json
        FROM papers p
        JOIN scores s ON s.paper_id = p.id
        WHERE s.combined_score >= {ph}
        ORDER BY s.combined_score DESC
        LIMIT {ph}
        """,
        (min_combined, limit),
    )
    return [_row_to_dict(r) for r in rows]


def get_papers_for_digest(
    conn: Any,
    min_combined: float = 0.4,
    max_papers: int = 20,
) -> list[dict]:
    """Get top-scoring papers that haven't been included in a digest yet."""
    ph = _placeholder(conn)
    rows = fetch_all(
        conn,
        f"""
        SELECT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier
        FROM papers p
        JOIN scores s ON s.paper_id = p.id
        LEFT JOIN digest_papers dp ON dp.paper_id = p.id
        WHERE s.combined_score >= {ph}
          AND dp.paper_id IS NULL
        ORDER BY s.combined_score DESC
        LIMIT {ph}
        """,
        (min_combined, max_papers),
    )
    return [_row_to_dict(r) for r in rows]


# --- Digests ---


def record_digest(
    conn: Any,
    paper_ids: list[int],
    delivery_method: str = "stdout",
) -> int:
    """Record that a digest was sent. Returns digest id."""
    ph = _placeholder(conn)
    is_sqlite = "sqlite3" in type(conn).__module__

    with transaction(conn):
        cur = execute(
            conn,
            f"""
            INSERT INTO digests (paper_count, delivery_method)
            VALUES ({ph}, {ph})
            """,
            (len(paper_ids), delivery_method),
        )
        if is_sqlite:
            digest_id = cur.lastrowid
        else:
            digest_id = fetch_scalar(conn, "SELECT currval('digests_id_seq')")

        for pid in paper_ids:
            execute(
                conn,
                f"INSERT INTO digest_papers (digest_id, paper_id) VALUES ({ph}, {ph})",
                (digest_id, pid),
            )

    return digest_id


# --- Helpers ---


def _row_to_dict(row: Any) -> dict:
    """Convert a DB-API row to a plain dict."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    # sqlite3.Row
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)
