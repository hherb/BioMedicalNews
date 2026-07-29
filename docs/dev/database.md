# Database Schema & Operations

## Overview

The database is shared between two owners. **bmlib owns the paper record**: `publications`, `fulltext_sources` and `download_days` are created by `bmlib.publications.ensure_schema()` and written by `bmlib.publications.sync()`. **bmnews owns everything about scoring and delivery**: `scores`, `paper_tags`, `digests`, `digest_papers`, `paper_extras` and `notifications`.

bmnews used to have a `papers` table of its own. Migration 4 (`migrate_to_publications`) replayed every row through bmlib's storage and dropped it — see [Migrations](#migrations) below. Anything still referring to a `papers` table is describing a schema that no longer exists.

All bmnews SQL lives in `bmnews/db/` — DDL in `migrations.py` (there is no standalone schema DDL; `schema.py` only opens connections and runs migrations), queries in `operations.py`. Execution goes through `bmlib.db`, and both SQLite (default, zero-config) and PostgreSQL are supported.

## What a "paper" is

There is no single table holding one. `operations.py` assembles a paper from a three-way join:

```
publications  (bmlib: identity and bibliographic metadata)
   LEFT JOIN paper_extras  (bmnews: source extras, cached full text)
   LEFT JOIN scores        (bmnews: relevance, quality, summary) — when scored
```

`_PAPER_COLUMNS` / `_PAPER_FROM` hold that SELECT list and FROM clause once so the queries cannot drift apart, and **`_row_to_paper()` is the only place a row becomes a paper dict**: it decodes the JSON array columns and derives the outbound `url` exactly once, so no caller re-parses JSON. `paper_id` in the bmnews tables references `publications(id)`; the column keeps its name because "paper" is bmnews's noun for the thing and the GUI routes are `/papers/<id>`.

## Schema — bmlib-owned

### `publications`

One row per paper. Identity is a normalised DOI, then a PMID: a paper arriving from a second source merges into the existing row instead of duplicating it, and a record with only a PMID is stored rather than dropped.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER / SERIAL | Primary key |
| `doi` | TEXT | Normalised DOI, `NULL` when unknown |
| `pmid` | TEXT | PubMed id, `NULL` when unknown |
| `pmcid` | TEXT | PubMed Central id |
| `title` | TEXT NOT NULL | Paper title |
| `abstract` | TEXT | Abstract |
| `authors` | TEXT | **JSON array** of author names |
| `journal` | TEXT | Journal name |
| `publication_date` | TEXT | ISO date string |
| `publication_types` | TEXT | **JSON array**; feeds bmlib's free Tier-1 quality classification |
| `keywords` | TEXT | **JSON array** of subject/category terms |
| `is_open_access` | INTEGER / BOOLEAN | As reported by a source |
| `license` | TEXT | License string |
| `sources` | TEXT NOT NULL | **JSON array** of registry source names |
| `first_seen_source` | TEXT NOT NULL | Which source stored it first |
| `created_at` / `updated_at` | TEXT | Timestamps |

**Indexes:** unique partial on `doi` and on `pmid` (both `WHERE … IS NOT NULL`), plus `publication_date`.

### `fulltext_sources`

Full-text URLs a fetcher reported for a publication — *where* the full text lives, keyed `UNIQUE(publication_id, url)` with a `source`, `format` and `version`. It does **not** hold the fetched body; the GUI's cached text is in `paper_extras`.

### `download_days`

Per-source, per-day fetch status, `UNIQUE(source, date)`, carrying `status` and `record_count`. This is what makes sync resumable: a day recorded complete is skipped on the next run, and a failed day is re-fetched instead of the whole window.

## Schema — bmnews-owned

### `scores`

One score per paper, enforced by `UNIQUE(paper_id)`.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER / SERIAL | Primary key |
| `paper_id` | INTEGER → `publications(id)` | Cascades on delete |
| `relevance_score` | REAL | LLM relevance, 0.0–1.0 |
| `quality_score` | REAL | Quality assessment, 0.0–1.0 |
| `combined_score` | REAL | `0.6 * relevance + 0.4 * quality` |
| `summary` | TEXT | LLM-generated summary |
| `study_design` | TEXT | `StudyDesign` **value** spelling, e.g. `"rct"` |
| `quality_tier` | TEXT | `QualityTier` **name**, e.g. `"TIER_4_EXPERIMENTAL"` |
| `assessment_json` | TEXT | Full relevance + quality detail |
| `scored_at` | TEXT / TIMESTAMP | When it was scored |

**Indexes:** `combined_score`.

### `paper_extras`

The leftovers bmlib has no column for, one row per publication (`publication_id` is the primary key, cascading on delete).

| Column | Type | Description |
|--------|------|-------------|
| `publication_id` | INTEGER → `publications(id)` | Primary key |
| `metadata_json` | TEXT NOT NULL | Source `extras` blob (e.g. `cited_by`) |
| `fulltext_html` | TEXT | The GUI's cached full text |
| `fulltext_source` | TEXT NOT NULL | Where that text came from |
| `fulltext_pdf_url` | TEXT | The PDF the text was extracted from |

The PDF URL is kept *beside* the HTML rather than instead of it: extraction loses figures, tables and layout, so the reading pane offers both.

One publication can be fed by several sources, so `save_paper_metadata()` **merges key by key** rather than replacing the blob — a later value wins, and a key the new metadata says nothing about survives.

### `paper_tags`

Interest tags the scorer matched, primary key `(paper_id, tag)`, indexed on `tag`. The notification matcher reads `paper["tags"]`, which `publications` has no column for, so `get_notification_candidates()` attaches them per chunk from here.

### `digests` / `digest_papers`

`digests` records each delivery event (`sent_at`, `paper_count`, `delivery_method` — `"email"`, `"stdout"`, `"file"`, `"email_failed"` — and `status`). `digest_papers` is the many-to-many junction, primary key `(digest_id, paper_id)`.

It serves two purposes: papers already linked to a digest are excluded from the next one, and a past digest can be re-rendered via `get_cached_digest_papers()`.

### `notifications`

One row per `(watch, paper_id, channel)` triple, which the table is unique on. The row holds the **latest** delivery attempt for that triple, `sent` or `failed` — a retry updates it in place and increments `attempts` rather than adding a second row.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER / SERIAL | Primary key |
| `watch` | TEXT | Watch name from `[notifications.watches.<name>]` |
| `paper_id` | INTEGER → `publications(id)` | Cascades on delete |
| `channel` | TEXT | Channel name — one watch can deliver to several |
| `status` | TEXT | `sent` or `failed` |
| `attempts` | INTEGER | Incremented on each retry of the same row |
| `error` | TEXT | Failure detail, empty on success |
| `sent_at` | TEXT / TIMESTAMP | When the attempt was recorded |

**Indexes:** `(watch, channel, status)`.

Two properties this table depends on:

- **The pending queue is not stored.** It is derived per run as "papers this watch matches now, minus those with a `sent` row for that channel". That is what makes paging idempotent, and it means editing a watch cannot leave orphaned queue rows behind. A `failed` row stays in the derived queue, so it retries.
- **It must stay separate from `digest_papers`.** `get_papers_for_digest()` excludes papers present in `digest_papers` and nothing else, so recording a notification there would silently suppress that paper's digest entry.

The unique key includes `channel` because one watch can deliver to both email and Matrix and one can succeed while the other fails — retry state is per-channel or it is wrong.

## Migrations

`db/schema.py` has no DDL of its own: `init_db(conn)` calls `bmlib.db.run_migrations(conn, MIGRATIONS)`, which applies whatever is pending and is safe to call on every connection open. `MIGRATIONS` lives in `db/migrations.py`, each entry carrying a version, a description and a function, with a pair of DDL strings per backend.

| # | Name | What it does |
|---|------|--------------|
| 1 | `initial_schema` | The original `papers`, `scores`, `digests`, `digest_papers` |
| 2 | `add_paper_tags` | `paper_tags` |
| 3 | `add_fulltext_columns` | `pmid`, `pmcid`, `fulltext_html`, `fulltext_source` on `papers`; backfills identifiers from `metadata_json` |
| 4 | `migrate_to_publications` | Moves storage onto bmlib — see below |
| 5 | `add_notifications` | `notifications` |
| 6 | `add_fulltext_pdf_url` | `paper_extras.fulltext_pdf_url`, and clears stale preprint-server full text |

### Migration 4 in detail

It replays every `papers` row through `store_publication()` so bmlib's dedupe decides identity, repoints the three bmnews tables that reference a paper (`scores`, `paper_tags`, `digest_papers` — `digests` itself carries no paper reference), creates `paper_extras`, and drops `papers`.

Where two rows collapse into one publication, `scores` is `UNIQUE(paper_id)`, so the surviving score is the **highest `combined_score`** — the one the digest showed and the user acted on. Tags and digest links are unioned; metadata merges key by key with the later row winning.

**This migration is destructive and one-way.** A row that can be keyed on neither DOI nor PMID cannot be represented, so it is logged at ERROR and written to `~/.bmnews/stranded-papers.json` (`constants.STRANDED_PAPERS_PATH`) before `papers` is dropped.

### Migration 6 in detail

It clears full text stored under a preprint server's own name (`_STALE_FULLTEXT_SOURCES`) — those rows hold an abstract-only rendering of a body-less JATS document — **and deletes the matching file from bmlib's disk cache**. Both halves are needed: bmlib consults its cache before the database, so clearing the row alone would have the next request served the same file and stored again under the `cached` source name, out of reach of any filter keyed on the server's name. A cache that cannot be opened is logged and skipped rather than failing the migration.

## Operations reference

All functions in `db/operations.py` take a DB-API connection as the first argument. Writes are keyword-only.

### Paper operations

| Function | Description |
|----------|-------------|
| `store_paper(conn, *, title, doi=None, pmid=None, …) → int` | Store one paper via `bmlib.publications.store_publication()` and return its `publications` id. Raises `ValueError` with neither DOI nor PMID. The pipeline no longer calls this — `sync()` stores during a fetch — but it is the supported way to insert a known paper from a script or test |
| `publication_id(conn, *, doi=None, pmid=None) → int \| None` | Look a publication up by either identifier |
| `paper_exists(conn, doi) → bool` | Whether a paper with this DOI is stored |
| `get_paper_by_doi(conn, doi) → dict \| None` | One paper by DOI |
| `get_paper(conn, paper_id) → dict \| None` | One paper by id |
| `get_paper_with_score(conn, paper_id) → dict \| None` | The same, with its score joined |
| `get_unscored_papers(conn, limit=500) → list[dict]` | Papers with no `scores` row |
| `count_unscored_papers(conn) → int` | How many are waiting |
| `paper_url(paper) → str` | The outbound URL, derived rather than stored |

### Score, digest and query operations

| Function | Description |
|----------|-------------|
| `save_score(conn, *, paper_id, …) → None` | Insert or update a score |
| `get_scored_papers(conn, min_combined=0.0, limit=100) → list[dict]` | Scored papers above a floor |
| `get_papers_for_digest(conn, min_combined, max_papers, min_relevance, exclude_tiers) → list[dict]` | Top papers **not yet in any digest** |
| `get_papers_filtered(conn, *, sort, source, quality_tier, study_design, search, limit, offset, with_total) → …` | The GUI's list query |
| `get_cached_digest_papers(conn, days=None) → list[dict]` | Papers from previous digests, optionally by date |
| `record_digest(conn, paper_ids, delivery_method) → int` | Record a digest and link its papers |

### Notification operations

| Function | Description |
|----------|-------------|
| `get_notification_candidates(conn, *, watch, channel, min_relevance, min_combined, exclude_tiers, limit, offset) → list[dict]` | One SQL-narrowed chunk of the derived queue, tags attached |
| `record_notification(conn, *, watch, paper_id, channel, status, error='') → None` | Record one attempt |
| `record_notifications(conn, *, watch, paper_ids, channel, status, error='') → None` | Record a whole batch **in one transaction** |
| `count_notifications(conn, *, watch, channel='', status='sent') → int` | How many have been delivered |

`get_notification_candidates()` deliberately selects `_NOTIFY_PAPER_COLUMNS` rather than `_PAPER_COLUMNS`: the latter's `p.*` would drag the GUI's cached full text through a query that materialises every candidate. Its `limit` is a **scan window, never a delivery cap** — the Python matcher rejects rows afterwards, so capping here would under-deliver silently. See [the notifications section of CLAUDE.md](../../CLAUDE.md) for the full rule.

`record_notifications()` writes the batch in one transaction because half a recorded batch would resend the rest under a different Matrix `txnId`.

### Extras, full text and tags

| Function | Description |
|----------|-------------|
| `save_fulltext(conn, *, paper_id, html, source, pdf_url='') → None` | Cache retrieved full text |
| `get_fulltext_sources(conn, paper_id) → list[FullTextSourceEntry]` | What bmlib recorded about where full text lives |
| `save_paper_metadata(conn, *, paper_id, metadata) → None` | Merge into the extras blob, key by key |
| `get_paper_metadata(conn, paper_id) → dict` | The decoded blob |
| `save_paper_tags(conn, *, paper_id, tags) → None` | Replace a paper's interest tags |
| `get_paper_tags(conn, paper_id) → list[str]` | One paper's tags |
| `get_all_tags(conn) → list[str]` | Every tag in use |
| `get_papers_by_tag(conn, tag) → list[dict]` | Papers carrying a tag |

## Backend differences

`bmlib.db` supplies the two helpers that keep the SQL backend-aware: `placeholder(conn)` returns `?` or `%s`, and `is_sqlite(conn)` selects between DDL or SQL variants. (`operations.py` aliases them as `_placeholder` / `_is_sqlite` for its own use.)

| Feature | SQLite | PostgreSQL |
|---------|--------|------------|
| Placeholder | `?` | `%s` |
| Auto-increment | `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL` |
| Current timestamp | `datetime('now')` | `NOW()` |
| Case-insensitive search | `LIKE` | `ILIKE` |
| Unnesting a JSON array | `json_each(...)` | `json_array_elements_text(...)` |
| Date arithmetic | `date('now', '-N days')` | `(CURRENT_DATE - (N \|\| ' days')::interval)` |

The JSON unnesting is what `_source_filter()` needs: `publications.sources` is a JSON array, so filtering papers by source is backend-specific SQL rather than a plain comparison.

Never rely on `cursor.lastrowid` after an upsert — SQLite leaves it pointing at the last row actually *inserted* when `ON CONFLICT` takes the UPDATE path. Look the row up by its natural key instead, as `store_paper()` does.

## Adding a new table

1. Write the `CREATE TABLE` for both backends as a pair of module-level strings in `migrations.py`
2. Add a `_mNNN_description(conn)` function that applies the right one via `create_tables(conn, …)`
3. Append `Migration(N, "description", _mNNN_description)` to `MIGRATIONS`
4. Add operation functions in `operations.py`, using `placeholder(conn)` and the `bmlib.db` helpers
5. Add tests in `tests/test_db.py` — they run against **both** backends

## Testing database operations

`tests/test_db.py` opts every test into per-backend parameterisation:

```python
pytestmark = pytest.mark.usefixtures("db_backend")
```

Build databases with `tests.backends.new_db()` rather than `connect_sqlite(":memory:")` directly — it returns an unmigrated connection on whichever backend the current run selected, so the caller decides which migrations to apply (which is what lets the migration tests build a database at an older version):

```python
from bmlib.db import execute, placeholder

from bmnews.db.schema import init_db
from tests.backends import new_db


def _db():
    conn = new_db()
    init_db(conn)
    return conn


def test_store_and_retrieve():
    conn = _db()
    pid = store_paper(conn, doi="10.1101/test", title="Test")
    assert get_paper_by_doi(conn, "10.1101/test")["title"] == "Test"
```

SQLite runs in memory. PostgreSQL runs only when `BMNEWS_TEST_PG_DSN` names a live server, each connection isolated in a schema of its own, and is skipped otherwise — so the backend-specific SQL above is untested unless you point it at one:

```bash
BMNEWS_TEST_PG_DSN=postgresql://bmnews:bmnews@localhost:5432/bmnews_test pytest tests/test_db.py
```

Give it a scratch database: the tests create and drop their own schemas.
