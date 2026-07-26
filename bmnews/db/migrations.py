"""Application-specific database migrations for bmnews.

Each migration is a function that receives a DB-API connection and
applies DDL.  Migrations use ``CREATE TABLE/INDEX IF NOT EXISTS`` so
they are safe to run against databases that already have the tables.

The migration runner (``bmlib.db.migrations``) tracks which versions
have been applied in a ``schema_version`` table.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from bmlib.db import Migration, create_tables, execute, fetch_all, is_sqlite, placeholder
from bmlib.publications import ensure_schema, store_publication
from bmlib.publications.models import FullTextSource, Publication

from bmnews.constants import STRANDED_PAPERS_LOG_LIMIT, STRANDED_PAPERS_PATH
from bmnews.db.operations import publication_id
from bmnews.metadata import parse_metadata

logger = logging.getLogger(__name__)

# ``is_sqlite``/``placeholder`` come from bmlib now — bmnews used to keep its
# own copies in ``bmnews/db/backend.py``.
_is_sqlite = is_sqlite

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
# Migration 3: fulltext columns on papers
# ---------------------------------------------------------------------------


def _m003_add_fulltext_columns(conn: Any) -> None:
    """Add pmid, pmcid, fulltext_html, fulltext_source columns to papers."""
    is_sqlite = _is_sqlite(conn)

    if is_sqlite:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        for col_def in [
            "pmid TEXT",
            "pmcid TEXT",
            "fulltext_html TEXT",
            "fulltext_source TEXT NOT NULL DEFAULT ''",
        ]:
            col_name = col_def.split()[0]
            if col_name not in existing:
                conn.execute(f"ALTER TABLE papers ADD COLUMN {col_def}")
        conn.commit()
    else:
        create_tables(
            conn,
            """\
ALTER TABLE papers ADD COLUMN IF NOT EXISTS pmid TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS pmcid TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS fulltext_html TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS fulltext_source TEXT NOT NULL DEFAULT '';
""",
        )

    # Backfill pmid/pmcid from metadata_json for existing europepmc papers
    if is_sqlite:
        conn.execute("""
            UPDATE papers SET
                pmid = json_extract(metadata_json, '$.pmid'),
                pmcid = json_extract(metadata_json, '$.pmcid')
            WHERE source = 'europepmc'
              AND metadata_json != '{}'
              AND pmid IS NULL
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# Migration 4: move storage onto bmlib's publications table
# ---------------------------------------------------------------------------

# bmnews-owned per-publication data that bmlib's schema has no column for.
# ``metadata_json`` keeps whatever a source sent in ``FetchedRecord.extras``
# (Europe PMC's ``cited_by``, for instance); the two full-text columns are the
# GUI's read-through cache, which bmlib's ``fulltext_sources`` table does not
# replace — that table records *where* full text lives, not the fetched body.
_M004_EXTRAS_SQLITE = """\
CREATE TABLE IF NOT EXISTS paper_extras (
    publication_id INTEGER PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    fulltext_html TEXT,
    fulltext_source TEXT NOT NULL DEFAULT ''
);
"""

_M004_EXTRAS_POSTGRESQL = _M004_EXTRAS_SQLITE

# The bmnews-owned tables that reference a paper, rebuilt so their foreign key
# points at ``publications``. (``digests`` itself carries no paper reference —
# only ``digest_papers`` links the two — so it is left alone.) ``paper_id``
# keeps its name: "paper" is bmnews's own noun for the thing (the GUI routes
# are /papers/<id>), and renaming the column would ripple through the scorer's
# result dicts for no behavioural gain.
#
# ON DELETE CASCADE throughout: bmlib owns the ``publications`` row, so if it
# ever removes one, the bmnews rows hanging off it should go with it rather
# than linger pointing at nothing.
_M004_REPOINTED_SQLITE = """\
CREATE TABLE scores_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
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

CREATE TABLE paper_tags_new (
    paper_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (paper_id, tag)
);

CREATE TABLE digest_papers_new (
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    paper_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    PRIMARY KEY (digest_id, paper_id)
);
"""

_M004_REPOINTED_POSTGRESQL = """\
CREATE TABLE scores_new (
    id SERIAL PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
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

CREATE TABLE paper_tags_new (
    paper_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (paper_id, tag)
);

CREATE TABLE digest_papers_new (
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    paper_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    PRIMARY KEY (digest_id, paper_id)
);
"""

_M004_INDEXES = """\
CREATE INDEX IF NOT EXISTS idx_scores_combined ON scores (combined_score);
CREATE INDEX IF NOT EXISTS idx_paper_tags_tag ON paper_tags (tag);
"""


def _split_semicolons(value: str | None) -> list[str]:
    """Split one of the old ``"A; B"`` string columns into a list."""
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def _as_list(value: Any) -> list[str]:
    """Coerce a metadata value that should be a list of strings into one."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _fulltext_sources(meta: dict) -> list[FullTextSource] | None:
    """Rebuild FullTextSource rows from a stored ``fulltext_sources`` blob."""
    entries = meta.get("fulltext_sources")
    if not isinstance(entries, list):
        return None
    sources = [
        FullTextSource(
            publication_id=0,  # store_publication fills this in
            source=entry.get("source", "unknown"),
            url=entry["url"],
            format=entry.get("format", "html"),
            version=entry.get("version"),
        )
        for entry in entries
        if isinstance(entry, dict) and entry.get("url")
    ]
    return sources or None


def _paper_to_publication(row: Any, meta: dict) -> Publication:
    """Build the bmlib :class:`Publication` for one legacy ``papers`` row."""
    source = row["source"] or "unknown"
    return Publication(
        title=row["title"] or "",
        sources=[source],
        first_seen_source=source,
        doi=row["doi"] or None,
        pmid=(row["pmid"] or meta.get("pmid")) or None,
        pmcid=(row["pmcid"] or meta.get("pmcid")) or None,
        abstract=row["abstract"] or None,
        # "A; B" becomes ["A", "B"]; likewise categories become keywords.
        authors=_split_semicolons(row["authors"]),
        journal=meta.get("journal") or None,
        publication_date=row["published_date"] or None,
        publication_types=_as_list(meta.get("pub_type")),
        keywords=_split_semicolons(row["categories"]),
        is_open_access=bool(meta.get("is_open_access")),
        license=meta.get("license") or None,
    )


def _copy_papers(conn: Any) -> tuple[dict[int, int], dict[int, dict]]:
    """Copy every ``papers`` row into ``publications``.

    Each row goes through :func:`store_publication`, so bmlib's own
    deduplication decides identity — two ``papers`` rows for one work (the
    same DOI in different cases, or a DOI row and a PMID row) collapse into a
    single publication.

    Returns:
        The ``papers.id`` → ``publications.id`` map, and the bmnews-only
        extras (metadata blob, cached full text) keyed by publication id,
        already merged across any rows that collapsed.
    """
    id_map: dict[int, int] = {}
    extras: dict[int, dict] = {}
    stranded: list[dict] = []

    for row in fetch_all(conn, "SELECT * FROM papers ORDER BY id"):
        meta = parse_metadata(row["metadata_json"])
        pub = _paper_to_publication(row, meta)

        if not pub.doi and not pub.pmid:
            # Nothing to key on: bmlib identifies a publication by DOI or
            # PMID, so such a row cannot be represented (and could not be
            # looked up again anyway). papers.doi was NOT NULL, so this only
            # catches rows whose DOI was stored blank.
            stranded.append(_stranded_row(row, "no DOI or PMID"))
            continue

        store_publication(conn, pub, fulltext_sources=_fulltext_sources(meta))
        # store_publication normalises the identifiers on ``pub`` in place, so
        # the row is looked back up by whichever canonical form it now holds.
        new_id = publication_id(conn, doi=pub.doi, pmid=pub.pmid)
        if new_id is None:  # pragma: no cover — defensive
            stranded.append(_stranded_row(row, "stored publication could not be found again"))
            continue

        id_map[row["id"]] = new_id
        _merge_extras(extras, new_id, row, meta)

    _report_stranded(stranded)
    return id_map, extras


def _stranded_row(row: Any, reason: str) -> dict:
    """Capture a ``papers`` row that cannot be migrated, for the rescue file."""
    return {"reason": reason, **dict(row)}


def _report_stranded(stranded: list[dict]) -> None:
    """Log and save any rows that could not be carried across.

    ``papers`` is dropped at the end of this migration, so a row left behind
    is gone for good. Writing it out first means a user who hits this can
    still see exactly what was lost — and re-enter it — rather than having
    only a log line that a GUI session never shows them.

    Args:
        stranded: The rows that could not be migrated, with a ``reason``.
    """
    if not stranded:
        return

    logger.error("%d paper(s) could not be migrated and were left behind", len(stranded))
    for entry in stranded[:STRANDED_PAPERS_LOG_LIMIT]:
        logger.error("  paper %s (%s): %.80s", entry.get("id"), entry["reason"], entry.get("title"))
    if len(stranded) > STRANDED_PAPERS_LOG_LIMIT:
        logger.error("  ... and %d more", len(stranded) - STRANDED_PAPERS_LOG_LIMIT)

    rescue_path = Path(STRANDED_PAPERS_PATH).expanduser()
    try:
        rescue_path.parent.mkdir(parents=True, exist_ok=True)
        rescue_path.write_text(json.dumps(stranded, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        # An unwritable home directory must not abort an otherwise good
        # migration — the rows are in the log either way.
        logger.error("Could not write stranded papers to %s: %s", rescue_path, exc)
    else:
        logger.error("Their full contents were saved to %s", rescue_path)


def _merge_extras(extras: dict[int, dict], pub_id: int, row: Any, meta: dict) -> None:
    """Fold one paper's bmnews-only data into the extras for its publication.

    Two papers can collapse into one publication, so this merges rather than
    overwrites. Rows are replayed oldest id first and the later one wins per
    key, matching what :func:`bmnews.db.operations.save_paper_metadata` does
    at runtime: the most recent reading of a value like a citation count is
    the one to keep, and a key the later row says nothing about survives.
    """
    current = extras.setdefault(
        pub_id, {"metadata": {}, "fulltext_html": None, "fulltext_source": ""}
    )
    current["metadata"].update(meta)
    # Same rule for the cached body: a later row is a more recent fetch. An
    # empty one does not blank out what an earlier row already cached.
    if row["fulltext_html"]:
        current["fulltext_html"] = row["fulltext_html"]
        current["fulltext_source"] = row["fulltext_source"] or ""


def _write_extras(conn: Any, extras: dict[int, dict]) -> None:
    """Persist the merged bmnews-only data into ``paper_extras``."""
    ph = placeholder(conn)
    for pub_id, data in extras.items():
        execute(
            conn,
            "INSERT INTO paper_extras"
            f" (publication_id, metadata_json, fulltext_html, fulltext_source)"
            f" VALUES ({ph}, {ph}, {ph}, {ph})",
            (
                pub_id,
                json.dumps(data["metadata"]),
                data["fulltext_html"],
                data["fulltext_source"],
            ),
        )


def _repoint_scores(conn: Any, id_map: dict[int, int]) -> None:
    """Rebuild ``scores`` against ``publications``, one row per publication.

    ``scores`` is UNIQUE(paper_id), so when two papers collapse into one
    publication only one of their scores can survive. The highest combined
    score wins: that is the one the digest would have shown and the user acted
    on.
    """
    ph = placeholder(conn)
    winners: dict[int, Any] = {}
    for row in fetch_all(conn, "SELECT * FROM scores"):
        pub_id = id_map.get(row["paper_id"])
        if pub_id is None:
            continue
        best = winners.get(pub_id)
        if best is None or row["combined_score"] > best["combined_score"]:
            winners[pub_id] = row

    for pub_id, row in winners.items():
        execute(
            conn,
            "INSERT INTO scores_new (paper_id, relevance_score, quality_score,"
            " combined_score, summary, study_design, quality_tier, assessment_json,"
            f" scored_at) VALUES ({', '.join([ph] * 9)})",
            (
                pub_id,
                row["relevance_score"],
                row["quality_score"],
                row["combined_score"],
                row["summary"],
                row["study_design"],
                row["quality_tier"],
                row["assessment_json"],
                row["scored_at"],
            ),
        )


def _repoint_paper_tags(conn: Any, id_map: dict[int, int]) -> None:
    """Rebuild ``paper_tags`` against ``publications``, unioning duplicates."""
    ph = placeholder(conn)
    pairs = {
        (id_map[row["paper_id"]], row["tag"])
        for row in fetch_all(conn, "SELECT paper_id, tag FROM paper_tags")
        if row["paper_id"] in id_map
    }
    for pub_id, tag in sorted(pairs):
        execute(
            conn,
            f"INSERT INTO paper_tags_new (paper_id, tag) VALUES ({ph}, {ph})",
            (pub_id, tag),
        )


def _repoint_digest_papers(conn: Any, id_map: dict[int, int]) -> None:
    """Rebuild ``digest_papers`` against ``publications``, unioning duplicates.

    Two papers that collapsed into one publication and appeared in the same
    digest become a single link rather than a primary-key violation.
    """
    ph = placeholder(conn)
    pairs = {
        (row["digest_id"], id_map[row["paper_id"]])
        for row in fetch_all(conn, "SELECT digest_id, paper_id FROM digest_papers")
        if row["paper_id"] in id_map
    }
    for digest_id, pub_id in sorted(pairs):
        execute(
            conn,
            f"INSERT INTO digest_papers_new (digest_id, paper_id) VALUES ({ph}, {ph})",
            (digest_id, pub_id),
        )


def _swap_in_rebuilt_tables(conn: Any) -> None:
    """Drop the old FK-to-papers tables and promote the rebuilt ones."""
    for table in ("scores", "paper_tags", "digest_papers"):
        execute(conn, f"DROP TABLE {table}")
        execute(conn, f"ALTER TABLE {table}_new RENAME TO {table}")
    execute(conn, "DROP TABLE papers")
    create_tables(conn, _M004_INDEXES)


def _m004_migrate_to_publications(conn: Any) -> None:
    """Move paper storage onto ``bmlib.publications``.

    bmnews kept its own ``papers`` table, duplicating in a simpler form what
    ``bmlib.publications`` already provides — and losing things in the
    process: a paper without a DOI was dropped outright, a day that failed to
    fetch was silently refetched from scratch, and the same work arriving from
    two sources became two rows.

    Every ``papers`` row is replayed through :func:`store_publication` so
    bmlib's deduplication decides identity, then the four bmnews-owned tables
    (``scores``, ``paper_tags``, ``digests``, ``digest_papers``) are repointed
    at the resulting publication ids and ``papers`` is dropped. bmlib has no
    opinion on scoring or digests, so those tables stay bmnews's own.

    The whole migration runs inside the migration runner's transaction: it
    either completes or leaves the v3 database untouched.
    """
    sqlite = _is_sqlite(conn)

    ensure_schema(conn)
    create_tables(conn, _M004_EXTRAS_SQLITE if sqlite else _M004_EXTRAS_POSTGRESQL)
    create_tables(conn, _M004_REPOINTED_SQLITE if sqlite else _M004_REPOINTED_POSTGRESQL)

    id_map, extras = _copy_papers(conn)
    _write_extras(conn, extras)
    _repoint_scores(conn, id_map)
    _repoint_paper_tags(conn, id_map)
    _repoint_digest_papers(conn, id_map)
    _swap_in_rebuilt_tables(conn)

    logger.info("Migrated %d paper(s) onto bmlib.publications", len(id_map))


# ---------------------------------------------------------------------------
# Migration 5: notification delivery records
# ---------------------------------------------------------------------------

# One row per *delivered* notification, not per queued one. The pending queue
# is derived on each run — papers a watch matches now, minus those already
# sent — so editing a watch's criteria cannot leave stale rows queued under
# criteria that no longer match.
#
# The unique key includes ``channel`` because one watch can deliver to both
# email and Matrix and one can succeed while the other fails; retry state is
# per-channel or it is wrong. A ``failed`` row is deliberately still selected
# by the pending query, which is what makes a failed delivery retry.
#
# This table must stay separate from ``digest_papers``: ``get_papers_for_digest``
# excludes papers present there and nothing else, so recording a notification in
# it would silently suppress that paper's digest entry. A notification is "now";
# the digest is the record, and a paper belongs in both.
_M005_SQLITE = """\
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch TEXT NOT NULL,
    paper_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (watch, paper_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_notifications_lookup
    ON notifications (watch, channel, status);
"""

_M005_POSTGRESQL = """\
CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    watch TEXT NOT NULL,
    paper_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    sent_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (watch, paper_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_notifications_lookup
    ON notifications (watch, channel, status);
"""


def _m005_add_notifications(conn: Any) -> None:
    """Create the ``notifications`` table recording watch deliveries."""
    create_tables(conn, _M005_SQLITE if _is_sqlite(conn) else _M005_POSTGRESQL)


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

MIGRATIONS: list[Migration] = [
    Migration(1, "initial_schema", _m001_initial_schema),
    Migration(2, "add_paper_tags", _m002_add_paper_tags),
    Migration(3, "add_fulltext_columns", _m003_add_fulltext_columns),
    Migration(4, "migrate_to_publications", _m004_migrate_to_publications),
    Migration(5, "add_notifications", _m005_add_notifications),
]
