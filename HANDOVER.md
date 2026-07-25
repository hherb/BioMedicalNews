# Handover — migrating storage onto `bmlib.publications`

Status after the bmlib audit (PR #4): every *fetch* path now goes through bmlib's
source registry, but bmnews still stores what it fetches in its own `papers`
table. bmlib ships a complete alternative — `bmlib.publications.sync` /
`storage` / `schema` — that bmnews duplicates in a simpler form
(`pipeline._fetch_via_registry()` + `pipeline.run_store()`).

This is the plan for closing that gap. It is a data migration, not a
refactor, so it is deliberately not part of PR #4.

## What bmlib gives us that bmnews lacks

- **`download_days` resume tracking** — a day that failed is recorded and
  re-fetched next run. bmnews currently refetches the whole lookback window
  every time and silently loses a failed day.
- **Identity merging** — `store_publication()` normalises DOIs and PMIDs,
  dedupes across sources, and consolidates a split identity (the same work
  arriving with a DOI from one source and a PMID from another).
- **PMID-only records** — bmnews's `papers.doi` is `NOT NULL UNIQUE`, so
  `run_store()` *drops* any paper without a DOI. bmlib keys on either.
- **One transaction per day**, with the write lock held only for the store
  loop rather than across network I/O.

## Blocker to resolve first

**`bmlib.publications` is SQLite-only.** `storage.py` hardcodes `?`
placeholders (24 of them) and `schema.py` uses `INTEGER PRIMARY KEY
AUTOINCREMENT` plus partial indexes. bmnews supports PostgreSQL
(`config.database.backend`). Pick one before starting:

1. Upstream backend-aware SQL into bmlib (mirrors what `bmnews/db/backend.py`
   already does) — preferred, keeps both projects honest.
2. Drop the PostgreSQL backend from bmnews.
3. Keep bmnews's own storage layer and close this handover as "won't do".

## Schema reconciliation

`publications` covers most of `papers`, but not all of it:

| bmnews `papers` | bmlib `publications` | Action |
|---|---|---|
| `authors` (`"A; B"` string) | `authors` (JSON list) | Convert on migrate; update templates |
| `categories` (`"a; b"` string) | `keywords` (JSON list) | Convert; `_record_categories()` becomes unnecessary |
| `metadata_json.pub_type` | `publication_types` (JSON list) | Direct move — feeds Tier-1 quality classification |
| `metadata_json.journal` | `journal` | Direct move |
| `source` (single) | `sources` (list) + `first_seen_source` | Filter queries must match inside a list |
| `url` | — | Derive from DOI at render time |
| `pmcid` | — | **No column.** Needed for full-text retrieval — either upstream it or keep a bmnews-side side table |
| `fulltext_html` / `fulltext_source` | `fulltext_sources` table + `bmlib.fulltext.FullTextCache` | Cache parsed HTML on disk instead of in a column |
| `metadata_json.cited_by` | — | Lost unless kept in a side table |

## Steps

1. **Resolve the PostgreSQL blocker above.** Nothing else starts until this
   is decided.
2. **Migration 4** — `ensure_schema(conn)`, then copy every `papers` row
   through `store_publication()` so bmlib's own dedupe decides identity.
   Build a `papers.id → publications.id` map as you go.
3. **Handle merges.** `store_publication()` can collapse two `papers` rows
   into one publication. `scores` is `UNIQUE(paper_id)`, so decide a winner
   (suggestion: highest `combined_score`, it is the one the user acted on).
   `paper_tags` and `digest_papers` union cleanly.
4. **Repoint the bmnews-owned tables** — `scores`, `digests`, `digest_papers`,
   `paper_tags` all FK to `papers(id)`; remap via the id map, then drop
   `papers`. These four tables stay bmnews's own; bmlib has no opinion on
   scoring or digests.
5. **Replace fetch + store with `sync()`.** `pipeline.run_fetch()` and
   `run_store()` collapse into one `sync(conn, sources=..., date_from=...,
   date_to=..., source_configs=..., on_progress=...)` call. Map
   `SyncProgress` onto the existing `on_progress(str)` callback so the GUI
   status bar keeps working. `FetchedPaper` and `_record_to_fetched_paper()`
   can then go.
6. **Rewrite the queries** in `db/operations.py` against `publications`.
   The source filter is the awkward one — `sources` is a JSON list, so
   `p.source = ?` becomes a `json_each` join.
7. **Tests.** `tests/test_db.py` and `tests/test_pipeline.py` carry the
   detail. Add a migration test that starts from a populated v3 database and
   asserts no score, tag or digest link is orphaned.

## Also unused

`bmlib.transparency` — `TransparencyAnalyzer` queries CrossRef, Europe PMC,
OpenAlex and ClinicalTrials.gov for funding, COI and trial-registration
signals. bmnews declares a `[transparency]` config section and a
`transparency` packaging extra but never calls it, and
`QualityAssessment` already has `transparency_result` /
`transparency_adjusted` fields waiting for it. That is a feature, independent
of this migration.
