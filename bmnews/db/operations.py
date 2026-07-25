"""Pure-function database operations for bmnews.

All SQL lives here. Uses bmlib.db for execution.
Backend-aware: detects sqlite3 vs psycopg2 by connection module name.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from bmlib.db import execute, fetch_all, fetch_one, fetch_scalar, transaction

from bmnews.constants import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_QUERY_LIMIT,
    UNSCORED_BATCH_SIZE,
)
from bmnews.db.backend import is_sqlite as _is_sqlite
from bmnews.db.backend import placeholder as _placeholder

logger = logging.getLogger(__name__)


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
    """Insert a paper, or update it in place if its DOI is already stored.

    Args:
        conn: DB-API connection.
        doi: Paper DOI (the natural key — must be non-empty and unique).
        title: Paper title.
        authors: Semicolon-separated author list.
        abstract: Abstract text.
        url: Canonical URL for the paper.
        source: Source identifier the paper was fetched from.
        published_date: ISO publication date string.
        categories: Semicolon-separated category/subject list.
        metadata_json: Source-specific metadata encoded as a JSON object.

    Returns:
        The id of the inserted or updated ``papers`` row.
    """
    ph = _placeholder(conn)
    is_sqlite = _is_sqlite(conn)

    if is_sqlite:
        # RETURNING requires SQLite >= 3.35; fall back to a DOI lookup so the
        # correct id is returned on the conflict path too.  ``cur.lastrowid``
        # is NOT usable here: when ON CONFLICT takes the UPDATE branch SQLite
        # leaves it pointing at the last row actually inserted.
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
        if not is_sqlite:
            row = cur.fetchone()
            return row[0] if row else 0

    paper_id = fetch_scalar(conn, f"SELECT id FROM papers WHERE doi = {ph}", (doi,))
    return int(paper_id) if paper_id is not None else 0


def get_paper_by_doi(conn: Any, doi: str) -> dict | None:
    """Fetch a single paper by DOI. Returns dict or None."""
    ph = _placeholder(conn)
    row = fetch_one(conn, f"SELECT * FROM papers WHERE doi = {ph}", (doi,))
    return _row_to_dict(row) if row else None


def get_paper_with_score(conn: Any, paper_id: int) -> dict | None:
    """Fetch a single paper with its score data joined. Returns dict or None."""
    ph = _placeholder(conn)
    row = fetch_one(
        conn,
        f"""
        SELECT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier, s.assessment_json
        FROM papers p
        LEFT JOIN scores s ON s.paper_id = p.id
        WHERE p.id = {ph}
        """,
        (paper_id,),
    )
    return _row_to_dict(row) if row else None


def get_unscored_papers(
    conn: Any, limit: int = UNSCORED_BATCH_SIZE,
) -> list[dict]:
    """Get papers that have no row in ``scores`` yet, newest fetch first.

    Args:
        conn: DB-API connection.
        limit: Maximum number of papers to return. Callers that need to know
            whether more remain should compare the result length to *limit*.

    Returns:
        List of paper dicts, at most *limit* long.
    """
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


def count_unscored_papers(conn: Any) -> int:
    """Return how many stored papers have no row in ``scores`` yet."""
    return fetch_scalar(
        conn,
        "SELECT COUNT(*) FROM papers p "
        "LEFT JOIN scores s ON s.paper_id = p.id WHERE s.id IS NULL",
    ) or 0


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
    is_sqlite = _is_sqlite(conn)

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
    conn: Any, min_combined: float = 0.0, limit: int = DEFAULT_QUERY_LIMIT,
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
    max_papers: int = DEFAULT_PAGE_SIZE,
    min_relevance: float = 0.0,
    exclude_tiers: Sequence[str] = (),
) -> list[dict]:
    """Get top-scoring papers that haven't been included in a digest yet.

    Args:
        conn: DB-API connection.
        min_combined: Minimum combined score a paper must reach.
        max_papers: Maximum number of papers to return.
        min_relevance: Minimum relevance score a paper must reach.
        exclude_tiers: Quality tier names to leave out entirely.

    Returns:
        Paper dicts with their scoring data, best combined score first.
    """
    ph = _placeholder(conn)
    params: list = [min_combined, min_relevance]
    tier_filter = ""
    if exclude_tiers:
        placeholders = ", ".join(ph for _ in exclude_tiers)
        tier_filter = f"AND s.quality_tier NOT IN ({placeholders})"
        params.extend(exclude_tiers)
    params.append(max_papers)

    rows = fetch_all(
        conn,
        f"""
        SELECT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier
        FROM papers p
        JOIN scores s ON s.paper_id = p.id
        LEFT JOIN digest_papers dp ON dp.paper_id = p.id
        WHERE s.combined_score >= {ph}
          AND s.relevance_score >= {ph}
          AND dp.paper_id IS NULL
          {tier_filter}
        ORDER BY s.combined_score DESC
        LIMIT {ph}
        """,
        tuple(params),
    )
    return [_row_to_dict(r) for r in rows]


def get_papers_filtered(
    conn: Any,
    *,
    sort: str = "combined",
    source: str = "",
    quality_tier: str = "",
    study_design: str = "",
    search: str = "",
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    with_total: bool = False,
) -> list[dict] | tuple[list[dict], int]:
    """Query papers with sorting, filtering, keyword search and pagination.

    Args:
        conn: DB-API connection.
        sort: One of ``combined``, ``relevance``, ``quality`` or ``date``.
            Unknown values fall back to ``combined``.
        source: Restrict to a single source name, or ``""`` for all.
        quality_tier: Restrict to a single quality tier name, or ``""``.
        study_design: Restrict to a single study design, or ``""``.
        search: Case-insensitive substring matched against title and abstract.
        limit: Maximum number of rows to return.
        offset: Number of rows to skip, for pagination.
        with_total: If True, also return the unpaginated match count.

    Returns:
        A list of paper dicts, or ``(papers, total)`` when *with_total* is set.
    """
    ph = _placeholder(conn)
    params: list = []
    conditions: list[str] = []

    if source:
        conditions.append(f"p.source = {ph}")
        params.append(source)
    if quality_tier:
        conditions.append(f"s.quality_tier = {ph}")
        params.append(quality_tier)
    if study_design:
        conditions.append(f"s.study_design = {ph}")
        params.append(study_design)
    if search:
        conditions.append(f"(p.title LIKE {ph} OR p.abstract LIKE {ph})")
        params.extend([f"%{search}%", f"%{search}%"])

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    sort_map = {
        "combined": "s.combined_score DESC NULLS LAST",
        "relevance": "s.relevance_score DESC NULLS LAST",
        "quality": "s.quality_score DESC NULLS LAST",
        "date": "p.published_date DESC",
    }
    order_by = sort_map.get(sort, "s.combined_score DESC NULLS LAST")

    base_query = f"""
        FROM papers p
        LEFT JOIN scores s ON s.paper_id = p.id
        {where}
    """

    total = 0
    if with_total:
        total = fetch_scalar(
            conn,
            f"SELECT COUNT(*) {base_query}",
            tuple(params),
        ) or 0

    rows = fetch_all(
        conn,
        f"""
        SELECT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier
        {base_query}
        ORDER BY {order_by}
        LIMIT {ph} OFFSET {ph}
        """,
        tuple(params + [limit, offset]),
    )

    results = [_row_to_dict(r) for r in rows]
    if with_total:
        return results, total
    return results


def get_cached_digest_papers(conn: Any, days: int | None = None) -> list[dict]:
    """Get papers that were included in previous digests.

    Args:
        conn: DB-API connection.
        days: If provided, only return papers with published_date
              within the last N days.

    Returns:
        List of paper dicts with scoring data, same format as
        get_papers_for_digest.
    """
    ph = _placeholder(conn)
    is_sqlite = _is_sqlite(conn)

    if days is not None:
        if is_sqlite:
            date_filter = f"AND p.published_date >= date('now', '-' || {ph} || ' days')"
        else:
            date_filter = (
                f"AND p.published_date >= (CURRENT_DATE - ({ph} || ' days')::interval)::text"
            )
        params: tuple = (days,)
    else:
        date_filter = ""
        params = ()

    rows = fetch_all(
        conn,
        f"""
        SELECT DISTINCT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier
        FROM papers p
        JOIN scores s ON s.paper_id = p.id
        JOIN digest_papers dp ON dp.paper_id = p.id
        WHERE 1=1 {date_filter}
        ORDER BY s.combined_score DESC
        """,
        params,
    )
    return [_row_to_dict(r) for r in rows]


# --- Digests ---


def record_digest(
    conn: Any,
    paper_ids: list[int],
    delivery_method: str = "stdout",
) -> int:
    """Record that a digest was sent and link it to its papers.

    Args:
        conn: DB-API connection.
        paper_ids: Ids of the papers included in the digest.
        delivery_method: How the digest was delivered (``file``, ``email``,
            ``email_failed`` or ``stdout``).

    Returns:
        The id of the newly created ``digests`` row.
    """
    ph = _placeholder(conn)
    is_sqlite = _is_sqlite(conn)

    # A plain INSERT never takes a conflict path, so lastrowid is reliable
    # here; PostgreSQL uses RETURNING rather than currval() so the result
    # does not depend on the sequence being named ``digests_id_seq``.
    insert_sql = f"""
        INSERT INTO digests (paper_count, delivery_method)
        VALUES ({ph}, {ph})
    """
    if not is_sqlite:
        insert_sql += " RETURNING id"

    with transaction(conn):
        cur = execute(conn, insert_sql, (len(paper_ids), delivery_method))
        if is_sqlite:
            digest_id = cur.lastrowid
        else:
            row = cur.fetchone()
            digest_id = row[0] if row else 0

        for pid in paper_ids:
            execute(
                conn,
                f"INSERT INTO digest_papers (digest_id, paper_id) VALUES ({ph}, {ph})",
                (digest_id, pid),
            )

    return digest_id


# --- Full Text ---


def save_fulltext(
    conn: Any, *, paper_id: int, html: str, source: str,
) -> None:
    """Store full-text HTML and source for a paper."""
    ph = _placeholder(conn)
    with transaction(conn):
        execute(
            conn,
            f"UPDATE papers SET fulltext_html = {ph}, fulltext_source = {ph} WHERE id = {ph}",
            (html, source, paper_id),
        )


def update_paper_identifiers(
    conn: Any, *, paper_id: int, pmid: str | None = None, pmcid: str | None = None,
) -> None:
    """Update pmid and/or pmcid for a paper."""
    ph = _placeholder(conn)
    sets = []
    params: list = []
    if pmid is not None:
        sets.append(f"pmid = {ph}")
        params.append(pmid)
    if pmcid is not None:
        sets.append(f"pmcid = {ph}")
        params.append(pmcid)
    if not sets:
        return
    params.append(paper_id)
    with transaction(conn):
        execute(conn, f"UPDATE papers SET {', '.join(sets)} WHERE id = {ph}", tuple(params))


# --- Paper Tags ---


def save_paper_tags(conn: Any, *, paper_id: int, tags: list[str]) -> None:
    """Replace all tags for a paper with the given list."""
    ph = _placeholder(conn)
    with transaction(conn):
        execute(conn, f"DELETE FROM paper_tags WHERE paper_id = {ph}", (paper_id,))
        for tag in tags:
            execute(
                conn,
                f"INSERT INTO paper_tags (paper_id, tag) VALUES ({ph}, {ph})",
                (paper_id, tag),
            )


def get_paper_tags(conn: Any, paper_id: int) -> list[str]:
    """Get all tags for a paper."""
    ph = _placeholder(conn)
    rows = fetch_all(
        conn,
        f"SELECT tag FROM paper_tags WHERE paper_id = {ph} ORDER BY tag",
        (paper_id,),
    )
    return [r["tag"] if isinstance(r, dict) else r[0] for r in rows]


def get_all_tags(conn: Any) -> list[str]:
    """Get all distinct tags in the database."""
    rows = fetch_all(conn, "SELECT DISTINCT tag FROM paper_tags ORDER BY tag")
    return [r["tag"] if isinstance(r, dict) else r[0] for r in rows]


def get_papers_by_tag(conn: Any, tag: str) -> list[dict]:
    """Get all papers that have a specific tag, joined with scores."""
    ph = _placeholder(conn)
    rows = fetch_all(
        conn,
        f"""
        SELECT p.*, s.relevance_score, s.quality_score, s.combined_score,
               s.summary, s.study_design, s.quality_tier
        FROM papers p
        JOIN scores s ON s.paper_id = p.id
        JOIN paper_tags pt ON pt.paper_id = p.id
        WHERE pt.tag = {ph}
        ORDER BY s.combined_score DESC
        """,
        (tag,),
    )
    return [_row_to_dict(r) for r in rows]


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
