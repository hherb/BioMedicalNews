# Handover — migrating storage onto `bmlib.publications`

**Status: done.** bmnews no longer has a `papers` table. Fetching and storing
are one `bmlib.publications.sync()` call, papers live in bmlib's
`publications` table, and the bmnews-owned tables (`scores`, `paper_tags`,
`digest_papers`, plus `digests` behind them) point at it.

What follows records how the plan was resolved, because the decisions are not
recoverable from the diff. The one section still open — `bmlib.transparency` —
is at the bottom; it was always independent of this migration.

## The blocker: `bmlib.publications` was SQLite-only

`storage.py` hardcoded `?` placeholders, `schema.py` used `INTEGER PRIMARY KEY
AUTOINCREMENT`, and bmnews supports PostgreSQL. Of the three options, the
preferred one was taken: **backend-aware SQL was upstreamed into bmlib**
(hherb/bmlib#28) rather than dropping PostgreSQL from bmnews or abandoning the
migration.

Testing PostgreSQL properly for the first time turned up three bugs in bmlib
that this migration would otherwise have inherited:

- `fetch_scalar()` always returned `None` there — psycopg2's `RealDictRow` is
  keyed by column name, so `row[0]` raised `KeyError` into a silent fallback.
- `transaction()` did not nest, so `sync()`'s one-commit-per-day batching
  degraded to one commit per record and a failed day could not roll back.
- `create_tables()` committed mid-migration, so a migration that failed
  part-way left its DDL applied.

The nesting fix has a trap worth remembering: nesting is counted by bmlib, not
read from psycopg2's transaction status. psycopg2 opens a transaction on the
first statement of *any* kind — a bare `SELECT` leaves the connection
`INTRANS` — so status-based detection would classify ordinary blocks as nested
and silently stop committing every write.

**bmnews requires the bmlib change.** `pyproject.toml` tracks bmlib's `main`,
so bmlib#28 has to land before this branch works from a clean install.

## What the migration bought

- **`download_days` resume tracking** — a day that failed is recorded and
  re-fetched next run. bmnews used to refetch the whole lookback window every
  time and silently lose a failed day.
- **Identity merging** — `store_publication()` normalises DOIs and PMIDs,
  dedupes across sources, and consolidates a split identity (the same work
  arriving with a DOI from one source and a PMID from another).
- **PMID-only records** — bmnews's `papers.doi` was `NOT NULL UNIQUE`, so
  `run_store()` *dropped* any paper without a DOI. bmlib keys on either.
- **One transaction per day**, with the write lock held only for the store
  loop rather than across network I/O.

## How the schema was reconciled

| bmnews `papers` | Where it went |
|---|---|
| `authors` (`"A; B"` string) | `publications.authors` (JSON list); templates render `authors\|join('; ')` |
| `categories` (`"a; b"` string) | `publications.keywords` (JSON list) |
| `metadata_json.pub_type` | `publications.publication_types` — still feeds Tier-1 quality classification |
| `metadata_json.journal` | `publications.journal` |
| `source` (single) | `publications.sources` (JSON list) + `first_seen_source`; the source filter unnests the array |
| `url` | Derived from the identifiers in `operations.paper_url()` |
| `pmcid` | Upstreamed — bmlib's `FetchedRecord.pmc_id` was being dropped on store, so a `pmcid` column was added there rather than kept bmnews-side |
| `fulltext_html` / `fulltext_source` | `paper_extras` (see below). Fetcher-reported URLs go to bmlib's `fulltext_sources` table |
| `metadata_json.cited_by` | `paper_extras.metadata_json` |

`paper_extras` is bmnews's side table for exactly two things bmlib has no
column for: the source `extras` blob, and the GUI's cached full-text body.
bmlib's `fulltext_sources` records *where* full text lives, not the fetched
body, so it does not replace the cache.

Two shapes deliberately did **not** change: `scores.paper_id` and friends keep
that name even though they now reference `publications(id)` — "paper" is
bmnews's noun, and the GUI routes are `/papers/<id>` — and `_row_to_paper()`
is the single place a row becomes a paper dict, so nothing downstream re-parses
JSON.

## Merges, and what happens to a score

`store_publication()` can collapse two `papers` rows into one publication.
`scores` is `UNIQUE(paper_id)`, so migration 4 keeps the **highest
`combined_score`** — that is the one the digest showed and the user acted on.
`paper_tags` and `digest_papers` union. `tests/test_db.py`'s
`TestMigrationToPublications` starts from a populated v3 database and asserts
no score, tag or digest link is orphaned.

A `papers` row with neither a DOI nor a PMID cannot be represented (bmlib keys
on one or the other) and is logged and left behind. `papers.doi` was
`NOT NULL`, so this only catches rows whose DOI was stored blank.

## Also unused

`bmlib.transparency` — `TransparencyAnalyzer` queries CrossRef, Europe PMC,
OpenAlex and ClinicalTrials.gov for funding, COI and trial-registration
signals. bmnews declares a `[transparency]` config section and a
`transparency` packaging extra but never calls it, and
`QualityAssessment` already has `transparency_result` /
`transparency_adjusted` fields waiting for it. That is a feature, independent
of this migration.
