"""Pure-function database operations for bmnews.

All SQL lives here. Uses bmlib.db for execution and bmlib.publications for
paper storage — bmnews owns the scoring, tagging and digest tables, bmlib owns
the publication records those tables point at.

Papers therefore come back as a join of three things: bmlib's ``publications``
row, bmnews's ``paper_extras`` row (source metadata bmlib has no column for,
plus the cached full text), and the ``scores`` row when there is one.
:func:`_row_to_paper` is the single place that shape is assembled, so the JSON
columns are decoded exactly once and every caller sees real lists.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from bmlib.db import execute, fetch_all, fetch_one, fetch_scalar, is_sqlite, placeholder
from bmlib.db import transaction as _transaction
from bmlib.fulltext.models import FullTextSourceEntry
from bmlib.publications import store_publication
from bmlib.publications.models import Publication
from bmlib.publications.storage import get_publication_by_doi, get_publication_by_pmid

from bmnews.constants import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_QUERY_LIMIT,
    UNSCORED_BATCH_SIZE,
)
from bmnews.metadata import parse_metadata

logger = logging.getLogger(__name__)

_is_sqlite = is_sqlite
_placeholder = placeholder

# Every paper query selects the same three-way join, so the SELECT list and the
# FROM clause live here rather than being retyped (and drifting) per query.
_PAPER_COLUMNS = """
    p.*, e.metadata_json, e.fulltext_html, e.fulltext_source
"""

_SCORE_COLUMNS = """
    s.relevance_score, s.quality_score, s.combined_score,
    s.summary, s.study_design, s.quality_tier
"""

_PAPER_FROM = """
    FROM publications p
    LEFT JOIN paper_extras e ON e.publication_id = p.id
"""


# --- Papers ---


def store_paper(
    conn: Any,
    *,
    doi: str | None = None,
    title: str,
    authors: Sequence[str] = (),
    abstract: str = "",
    source: str = "",
    published_date: str = "",
    keywords: Sequence[str] = (),
    pmid: str | None = None,
    pmcid: str | None = None,
    journal: str | None = None,
    publication_types: Sequence[str] = (),
    is_open_access: bool = False,
    license: str | None = None,  # noqa: A002 — matches bmlib's field name
    metadata: dict | None = None,
) -> int:
    """Store one paper and return its ``publications`` row id.

    Identity is bmlib's: the record is deduplicated on its normalised DOI and
    then its PMID, so re-storing a paper updates it, and the same work arriving
    from a second source merges into the existing row instead of duplicating
    it. Unlike the old ``papers`` table, a paper with only a PMID is stored
    rather than dropped.

    The pipeline itself no longer calls this — :func:`bmlib.publications.sync`
    does the storing during a fetch. It remains the supported way to put a
    single known paper into the database from a script or a test, which is
    what it is exercised as.

    Args:
        conn: DB-API connection.
        doi: Paper DOI. Optional — a PMID identifies a paper just as well.
        title: Paper title.
        authors: Author names.
        abstract: Abstract text.
        source: Registry name of the source it came from.
        published_date: ISO publication date string.
        keywords: Subject/category terms.
        pmid: PubMed id.
        pmcid: PubMed Central id.
        journal: Journal name.
        publication_types: Publication types, which feed bmlib's free
            Tier-1 quality classification.
        is_open_access: Whether a source reported it as open access.
        license: License string.
        metadata: Source-specific extras with no column of their own.

    Returns:
        The id of the stored (or merged-into) publication.

    Raises:
        ValueError: If neither *doi* nor *pmid* is given — bmlib has nothing
            to key the record on.
    """
    if not doi and not pmid:
        raise ValueError("a paper needs at least one of doi or pmid")

    pub = Publication(
        title=title,
        sources=[source] if source else [],
        first_seen_source=source or "unknown",
        doi=doi or None,
        pmid=pmid or None,
        pmcid=pmcid or None,
        abstract=abstract or None,
        authors=list(authors),
        journal=journal or None,
        publication_date=published_date or None,
        publication_types=list(publication_types),
        keywords=list(keywords),
        is_open_access=is_open_access,
        license=license or None,
    )

    with _transaction(conn):
        store_publication(conn, pub)
        # store_publication reports "added"/"merged", not an id, and normalises
        # the identifiers on ``pub`` as it goes — so the row is looked back up
        # by whichever canonical identifier it now carries.
        paper_id = publication_id(conn, doi=pub.doi, pmid=pub.pmid)
        if paper_id is None:  # pragma: no cover — defensive
            raise RuntimeError(f"stored publication could not be found again: {title[:80]}")
        if metadata:
            save_paper_metadata(conn, paper_id=paper_id, metadata=metadata)

    return paper_id


def publication_id(conn: Any, *, doi: str | None = None, pmid: str | None = None) -> int | None:
    """Return the publication id for a DOI or PMID, or None if unknown.

    Both identifiers are normalised the way bmlib stores them, so a lookup
    using any case or prefix variant matches.
    """
    if doi:
        found = get_publication_by_doi(conn, doi)
        if found is not None:
            return found.id
    if pmid:
        found = get_publication_by_pmid(conn, pmid)
        if found is not None:
            return found.id
    return None


def paper_exists(conn: Any, doi: str) -> bool:
    """Check whether a paper with this DOI is already stored."""
    return get_publication_by_doi(conn, doi) is not None


def get_paper_by_doi(conn: Any, doi: str) -> dict | None:
    """Fetch a single paper by DOI. Returns dict or None."""
    found = get_publication_by_doi(conn, doi)
    if found is None:
        return None
    return get_paper(conn, found.id)


def get_paper(conn: Any, paper_id: int) -> dict | None:
    """Fetch a single paper by id, without its score. Returns dict or None."""
    ph = _placeholder(conn)
    row = fetch_one(
        conn,
        f"SELECT {_PAPER_COLUMNS} {_PAPER_FROM} WHERE p.id = {ph}",
        (paper_id,),
    )
    return _row_to_paper(row) if row else None


def get_paper_with_score(conn: Any, paper_id: int) -> dict | None:
    """Fetch a single paper with its score data joined. Returns dict or None."""
    ph = _placeholder(conn)
    row = fetch_one(
        conn,
        f"""
        SELECT {_PAPER_COLUMNS}, {_SCORE_COLUMNS}, s.assessment_json
        {_PAPER_FROM}
        LEFT JOIN scores s ON s.paper_id = p.id
        WHERE p.id = {ph}
        """,
        (paper_id,),
    )
    return _row_to_paper(row) if row else None


def get_unscored_papers(
    conn: Any, limit: int = UNSCORED_BATCH_SIZE,
) -> list[dict]:
    """Get papers that have no row in ``scores`` yet, newest first.

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
        SELECT {_PAPER_COLUMNS}
        {_PAPER_FROM}
        LEFT JOIN scores s ON s.paper_id = p.id
        WHERE s.id IS NULL
        ORDER BY p.created_at DESC
        LIMIT {ph}
        """,
        (limit,),
    )
    return [_row_to_paper(r) for r in rows]


def count_unscored_papers(conn: Any) -> int:
    """Return how many stored papers have no row in ``scores`` yet."""
    return fetch_scalar(
        conn,
        "SELECT COUNT(*) FROM publications p "
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
    """Insert or update a score for a paper.

    Args:
        conn: DB-API connection.
        paper_id: The ``publications`` row this score is for.
        relevance_score: LLM relevance score, 0.0–1.0.
        quality_score: Quality score derived from the assessed tier.
        combined_score: Weighted combination of the two.
        summary: One-paragraph LLM summary.
        study_design: Classified study design.
        quality_tier: Assessed quality tier.
        assessment_json: Full quality assessment, encoded as JSON.
    """
    ph = _placeholder(conn)
    sqlite = _is_sqlite(conn)
    now = "datetime('now')" if sqlite else "NOW()"
    excluded = "excluded" if sqlite else "EXCLUDED"

    sql = f"""
        INSERT INTO scores (paper_id, relevance_score, quality_score,
                           combined_score, summary, study_design,
                           quality_tier, assessment_json)
        VALUES ({', '.join([ph] * 8)})
        ON CONFLICT(paper_id) DO UPDATE SET
            relevance_score = {excluded}.relevance_score,
            quality_score = {excluded}.quality_score,
            combined_score = {excluded}.combined_score,
            summary = {excluded}.summary,
            study_design = {excluded}.study_design,
            quality_tier = {excluded}.quality_tier,
            assessment_json = {excluded}.assessment_json,
            scored_at = {now}
    """

    params = (paper_id, relevance_score, quality_score, combined_score,
              summary, study_design, quality_tier, assessment_json)

    with _transaction(conn):
        execute(conn, sql, params)


def get_scored_papers(
    conn: Any, min_combined: float = 0.0, limit: int = DEFAULT_QUERY_LIMIT,
) -> list[dict]:
    """Get papers with scores above threshold, ordered by score."""
    ph = _placeholder(conn)
    rows = fetch_all(
        conn,
        f"""
        SELECT {_PAPER_COLUMNS}, {_SCORE_COLUMNS}, s.assessment_json
        {_PAPER_FROM}
        JOIN scores s ON s.paper_id = p.id
        WHERE s.combined_score >= {ph}
        ORDER BY s.combined_score DESC
        LIMIT {ph}
        """,
        (min_combined, limit),
    )
    return [_row_to_paper(r) for r in rows]


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
        SELECT {_PAPER_COLUMNS}, {_SCORE_COLUMNS}
        {_PAPER_FROM}
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
    return [_row_to_paper(r) for r in rows]


def _source_filter(conn: Any) -> str:
    """Return the SQL testing whether a paper carries a given source.

    ``publications.sources`` is a JSON array — a paper seen on both medRxiv
    and PubMed lists both — so matching a source means looking inside the
    array, not comparing the column. Each backend unnests JSON its own way.
    """
    ph = _placeholder(conn)
    if _is_sqlite(conn):
        return f"EXISTS (SELECT 1 FROM json_each(p.sources) WHERE json_each.value = {ph})"
    return (
        "EXISTS (SELECT 1 FROM json_array_elements_text(p.sources::json)"
        f" AS source_name WHERE source_name = {ph})"
    )


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
        source: Restrict to papers carrying this source, or ``""`` for all.
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
        conditions.append(_source_filter(conn))
        params.append(source)
    if quality_tier:
        conditions.append(f"s.quality_tier = {ph}")
        params.append(quality_tier)
    if study_design:
        conditions.append(f"s.study_design = {ph}")
        params.append(study_design)
    if search:
        # SQLite's LIKE already ignores ASCII case; PostgreSQL's does not and
        # has ILIKE for exactly this, so the operator differs per backend.
        like = f"LIKE {ph}" if _is_sqlite(conn) else f"ILIKE {ph}"
        conditions.append(f"(p.title {like} OR p.abstract {like})")
        params.extend([f"%{search}%", f"%{search}%"])

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # NULLS LAST throughout: a paper with no score (or no date) sorts to the
    # end rather than jumping to the top of a descending sort on PostgreSQL.
    sort_map = {
        "combined": "s.combined_score DESC NULLS LAST",
        "relevance": "s.relevance_score DESC NULLS LAST",
        "quality": "s.quality_score DESC NULLS LAST",
        "date": "p.publication_date DESC NULLS LAST",
    }
    order_by = sort_map.get(sort, "s.combined_score DESC NULLS LAST")

    base_query = f"""
        {_PAPER_FROM}
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
        SELECT {_PAPER_COLUMNS}, {_SCORE_COLUMNS}
        {base_query}
        ORDER BY {order_by}
        LIMIT {ph} OFFSET {ph}
        """,
        tuple(params + [limit, offset]),
    )

    results = [_row_to_paper(r) for r in rows]
    if with_total:
        return results, total
    return results


def get_cached_digest_papers(conn: Any, days: int | None = None) -> list[dict]:
    """Get papers that were included in previous digests.

    Args:
        conn: DB-API connection.
        days: If provided, only return papers published within the last N days.

    Returns:
        List of paper dicts with scoring data, same format as
        get_papers_for_digest.
    """
    ph = _placeholder(conn)

    if days is not None:
        if _is_sqlite(conn):
            date_filter = f"AND p.publication_date >= date('now', '-' || {ph} || ' days')"
        else:
            date_filter = (
                f"AND p.publication_date >= (CURRENT_DATE - ({ph} || ' days')::interval)::text"
            )
        params: tuple = (days,)
    else:
        date_filter = ""
        params = ()

    # EXISTS rather than a JOIN + DISTINCT: a paper carried by several digests
    # would otherwise appear once per link, and de-duplicating that with
    # DISTINCT means sorting every selected column — including the abstract.
    rows = fetch_all(
        conn,
        f"""
        SELECT {_PAPER_COLUMNS}, {_SCORE_COLUMNS}
        {_PAPER_FROM}
        JOIN scores s ON s.paper_id = p.id
        WHERE EXISTS (SELECT 1 FROM digest_papers dp WHERE dp.paper_id = p.id)
        {date_filter}
        ORDER BY s.combined_score DESC
        """,
        params,
    )
    return [_row_to_paper(r) for r in rows]


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
    sqlite = _is_sqlite(conn)

    # A plain INSERT never takes a conflict path, so lastrowid is reliable
    # here; PostgreSQL uses RETURNING rather than currval() so the result
    # does not depend on the sequence being named ``digests_id_seq``.
    insert_sql = f"""
        INSERT INTO digests (paper_count, delivery_method)
        VALUES ({ph}, {ph})
    """
    if not sqlite:
        insert_sql += " RETURNING id"

    with _transaction(conn):
        cur = execute(conn, insert_sql, (len(paper_ids), delivery_method))
        if sqlite:
            digest_id = cur.lastrowid
        else:
            row = cur.fetchone()
            digest_id = row["id"] if isinstance(row, dict) else row[0]

        for pid in paper_ids:
            execute(
                conn,
                f"INSERT INTO digest_papers (digest_id, paper_id) VALUES ({ph}, {ph})",
                (digest_id, pid),
            )

    return digest_id


# --- Paper extras (bmnews-only per-publication data) ---


def _upsert_extras(conn: Any, *, paper_id: int, columns: dict[str, Any]) -> None:
    """Insert or update the ``paper_extras`` row for a publication."""
    ph = _placeholder(conn)
    sqlite = _is_sqlite(conn)
    excluded = "excluded" if sqlite else "EXCLUDED"

    names = list(columns)
    assignments = ", ".join(f"{name} = {excluded}.{name}" for name in names)
    sql = (
        f"INSERT INTO paper_extras (publication_id, {', '.join(names)})"
        f" VALUES ({', '.join([ph] * (len(names) + 1))})"
        f" ON CONFLICT(publication_id) DO UPDATE SET {assignments}"
    )

    with _transaction(conn):
        execute(conn, sql, (paper_id, *(columns[name] for name in names)))


def save_fulltext(
    conn: Any, *, paper_id: int, html: str, source: str,
) -> None:
    """Store full-text HTML (or a link) and its source for a paper."""
    _upsert_extras(
        conn,
        paper_id=paper_id,
        columns={"fulltext_html": html, "fulltext_source": source},
    )


def get_fulltext_sources(conn: Any, paper_id: int) -> list[FullTextSourceEntry]:
    """Return the full-text locations a fetcher reported for this paper.

    These come from bmlib's ``fulltext_sources`` table, populated during sync,
    and are handed to :class:`bmlib.fulltext.FullTextService` as its first,
    cheapest tier — a URL the source already told us about beats rediscovering
    one through Europe PMC or Unpaywall.
    """
    ph = _placeholder(conn)
    rows = fetch_all(
        conn,
        "SELECT source, url, format, version FROM fulltext_sources"
        f" WHERE publication_id = {ph} ORDER BY id",
        (paper_id,),
    )
    return [
        FullTextSourceEntry(
            url=row["url"],
            format=row["format"],
            source=row["source"],
            version=row["version"],
        )
        for row in rows
    ]


def save_paper_metadata(conn: Any, *, paper_id: int, metadata: dict) -> None:
    """Merge source-specific extras into what is already stored for a paper.

    These are the fields bmlib's ``publications`` table has no column for —
    Europe PMC's ``cited_by``, for instance.

    One publication can be fed by several sources, so this merges key by key
    rather than replacing the blob: a key *metadata* carries wins (a citation
    count that has gone up should not be pinned to its first reading), and a
    key it says nothing about is left alone rather than dropped along with
    whatever source contributed it.

    Args:
        conn: DB-API connection.
        paper_id: The ``publications`` row these extras belong to.
        metadata: Extras to merge in. An empty dict is a no-op.
    """
    if not metadata:
        return

    with _transaction(conn):
        merged = get_paper_metadata(conn, paper_id)
        merged.update(metadata)
        _upsert_extras(
            conn,
            paper_id=paper_id,
            columns={"metadata_json": json.dumps(merged)},
        )


def get_paper_metadata(conn: Any, paper_id: int) -> dict:
    """Return the stored source extras for a paper, or ``{}`` if it has none."""
    ph = _placeholder(conn)
    row = fetch_one(
        conn,
        f"SELECT metadata_json FROM paper_extras WHERE publication_id = {ph}",
        (paper_id,),
    )
    return parse_metadata(row["metadata_json"]) if row else {}


# --- Paper Tags ---


def save_paper_tags(conn: Any, *, paper_id: int, tags: list[str]) -> None:
    """Replace all tags for a paper with the given list."""
    ph = _placeholder(conn)
    with _transaction(conn):
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
        SELECT {_PAPER_COLUMNS}, {_SCORE_COLUMNS}
        {_PAPER_FROM}
        JOIN scores s ON s.paper_id = p.id
        JOIN paper_tags pt ON pt.paper_id = p.id
        WHERE pt.tag = {ph}
        ORDER BY s.combined_score DESC
        """,
        (tag,),
    )
    return [_row_to_paper(r) for r in rows]


# --- Helpers ---

_JSON_LIST_COLUMNS = ("authors", "keywords", "publication_types", "sources")


def paper_url(paper: dict) -> str:
    """Build the canonical outbound URL for a paper.

    ``publications`` stores identifiers, not links, so the URL is derived —
    preferring the DOI, then PubMed, then PMC. Returns ``""`` when the paper
    carries no identifier that resolves to a page.
    """
    if paper.get("doi"):
        return f"https://doi.org/{paper['doi']}"
    if paper.get("pmid"):
        return f"https://pubmed.ncbi.nlm.nih.gov/{paper['pmid']}/"
    if paper.get("pmcid"):
        return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{paper['pmcid']}/"
    return ""


def _decode_json_list(raw: Any) -> list[str]:
    """Decode one of the JSON array columns, degrading to [] on bad data."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _row_to_paper(row: Any) -> dict:
    """Convert a joined DB row into the paper dict the rest of bmnews uses.

    The JSON array columns become real lists, the source-specific extras blob
    becomes a ``metadata`` dict, and the outbound ``url`` is derived from the
    identifiers.
    """
    if row is None:
        return {}
    # dict() covers both backends: sqlite3.Row exposes the mapping protocol,
    # and psycopg2's RealDictRow is already a dict subclass.
    paper = dict(row)

    for column in _JSON_LIST_COLUMNS:
        if column in paper:
            paper[column] = _decode_json_list(paper[column])

    paper["metadata"] = parse_metadata(paper.get("metadata_json"))
    paper["is_open_access"] = bool(paper.get("is_open_access"))
    paper["url"] = paper_url(paper)
    return paper
