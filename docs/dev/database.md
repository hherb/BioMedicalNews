# Database Schema & Operations

## Overview

bmnews uses a 4-table relational schema to store papers, scores, digest history, and paper-digest relationships. All SQL lives in `bmnews/db/` — schema DDL in `schema.py`, operations in `operations.py`.

The database layer uses `bmlib.db` for execution and supports both SQLite (default, zero-config) and PostgreSQL (production/multi-user).

## Schema

### `papers`

Stores fetched paper metadata. One row per unique DOI.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Auto-increment ID |
| `doi` | TEXT UNIQUE NOT NULL | DOI or identifier (e.g., `"pmid:12345"`) |
| `title` | TEXT NOT NULL | Paper title |
| `authors` | TEXT | Semicolon-separated author names |
| `abstract` | TEXT | Paper abstract |
| `url` | TEXT | DOI link or direct URL |
| `source` | TEXT | Source: `"medrxiv"`, `"biorxiv"`, `"europepmc"` |
| `published_date` | TEXT | ISO date string (YYYY-MM-DD) |
| `categories` | TEXT | Semicolon-separated categories |
| `metadata_json` | TEXT | JSON blob with source-specific metadata |
| `fetched_at` | TIMESTAMP | When the paper was first fetched |
| `created_at` | TIMESTAMP | Row creation timestamp |

**Indexes:** `doi`, `published_date`, `source`

### `scores`

Stores scoring results. One score per paper (enforced by unique constraint).

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Auto-increment ID |
| `paper_id` | INTEGER FK → papers(id) | Foreign key to paper |
| `relevance_score` | REAL | LLM relevance score (0.0–1.0) |
| `quality_score` | REAL | Quality assessment score (0.0–1.0) |
| `combined_score` | REAL | Weighted: `0.6 * relevance + 0.4 * quality` |
| `summary` | TEXT | LLM-generated summary |
| `study_design` | TEXT | Study design classification (e.g., `"rct"`, `"cohort_prospective"`) |
| `quality_tier` | TEXT | Quality tier name (e.g., `"TIER_4_EXPERIMENTAL"`) |
| `assessment_json` | TEXT | Full JSON with relevance + quality details |
| `scored_at` | TIMESTAMP | When the paper was scored |

**Indexes:** `combined_score`
**Constraints:** `UNIQUE(paper_id)` — one score per paper

### `digests`

Records each digest delivery event.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PRIMARY KEY | Auto-increment ID |
| `sent_at` | TIMESTAMP | When the digest was sent |
| `paper_count` | INTEGER | Number of papers in this digest |
| `delivery_method` | TEXT | `"email"`, `"stdout"`, `"file"`, `"email_failed"` |
| `status` | TEXT | `"sent"` (always, currently) |

### `digest_papers`

Junction table linking digests to papers (many-to-many).

| Column | Type | Description |
|--------|------|-------------|
| `digest_id` | INTEGER FK → digests(id) | Digest reference |
| `paper_id` | INTEGER FK → papers(id) | Paper reference |

**Primary key:** `(digest_id, paper_id)`

This table serves two purposes:
1. Track which papers have been included in digests (so they aren't repeated)
2. Allow re-rendering of past digests via `get_cached_digest_papers()`

## Operations reference

All functions in `db/operations.py` take a DB-API connection as the first argument.

### Paper operations

| Function | Signature | Description |
|----------|-----------|-------------|
| `paper_exists` | `(conn, doi) → bool` | Check if a paper with this DOI exists |
| `upsert_paper` | `(conn, *, doi, title, ...) → int` | Insert or update paper, returns paper ID |
| `get_paper_by_doi` | `(conn, doi) → dict \| None` | Fetch single paper by DOI |
| `get_unscored_papers` | `(conn, limit=100) → list[dict]` | Papers without score entries |

### Score operations

| Function | Signature | Description |
|----------|-----------|-------------|
| `save_score` | `(conn, *, paper_id, relevance_score, ...) → None` | Insert or update score |
| `get_scored_papers` | `(conn, min_combined, limit) → list[dict]` | Papers with scores above threshold |
| `get_papers_for_digest` | `(conn, min_combined, max_papers) → list[dict]` | Top papers not yet in any digest |
| `get_cached_digest_papers` | `(conn, days=None) → list[dict]` | Papers from previous digests, optionally filtered by date |

### Digest operations

| Function | Signature | Description |
|----------|-----------|-------------|
| `record_digest` | `(conn, paper_ids, delivery_method) → int` | Record digest + link papers, returns digest ID |

## Backend differences

The `_placeholder(conn)` helper returns `"?"` for SQLite and `"%s"` for PostgreSQL by inspecting `type(conn).__module__`.

Key SQL differences handled:

| Feature | SQLite | PostgreSQL |
|---------|--------|------------|
| Placeholder | `?` | `%s` |
| Auto-increment | `AUTOINCREMENT` | `SERIAL` |
| Current timestamp | `datetime('now')` | `NOW()` |
| Upsert returning | `cursor.lastrowid` | `RETURNING id` |
| Date arithmetic | `date('now', '-N days')` | `(CURRENT_DATE - (N \|\| ' days')::interval)` |
| Sequence value | N/A | `currval('digests_id_seq')` |

## Adding a new table

1. Add `CREATE TABLE` to both `SCHEMA_SQLITE` and `SCHEMA_POSTGRESQL` in `schema.py`
2. Add indexes as needed
3. Add operation functions in `operations.py` following the existing pattern
4. Add tests in `tests/test_db.py`

Example:

```python
# In operations.py
def get_recent_papers(conn, days: int = 7) -> list[dict]:
    """Get papers published in the last N days."""
    ph = _placeholder(conn)
    is_sqlite = "sqlite3" in type(conn).__module__
    if is_sqlite:
        sql = f"""
            SELECT * FROM papers
            WHERE published_date >= date('now', '-' || {ph} || ' days')
            ORDER BY published_date DESC
        """
    else:
        sql = f"""
            SELECT * FROM papers
            WHERE published_date >= (CURRENT_DATE - ({ph} || ' days')::interval)::text
            ORDER BY published_date DESC
        """
    rows = fetch_all(conn, sql, (days,))
    return [_row_to_dict(r) for r in rows]
```

## Testing database operations

All database tests use in-memory SQLite:

```python
from bmlib.db import connect_sqlite
from bmnews.db.schema import init_db

def _db():
    conn = connect_sqlite(":memory:")
    init_db(conn)
    return conn

def test_upsert_and_retrieve():
    conn = _db()
    pid = upsert_paper(conn, doi="10.1101/test", title="Test")
    assert pid > 0
    paper = get_paper_by_doi(conn, "10.1101/test")
    assert paper["title"] == "Test"
```

This pattern avoids file I/O and ensures each test starts with a clean database.
