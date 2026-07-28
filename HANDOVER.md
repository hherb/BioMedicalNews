# Handover

## Where things stand

| Area | State |
|---|---|
| Storage on `bmlib.publications` | **Done.** No `papers` table; fetch+store is one `sync()` call. Decisions worth keeping are recorded below. |
| Multi-provider LLM + model selector | **Done.** Six providers via bmlib (`list_providers()` is the authority), settings UI datalist cached in `~/.bmnews/model_cache.json`. Design/plan: `docs/plans/2026-02-15-llm-providers-model-selector-*.md`. |
| Notification service | **In flight** — see below. |
| `bmlib.transparency` | **Unused.** Config section and packaging extra declared, analyzer never called. |

## Environment gotcha

`uv.lock` pins bmlib **by commit**, and `uv run` re-syncs to that pin — so
installing a newer bmlib by hand is silently undone on the next `uv run`.
When bmnews starts using a bmlib symbol that does not exist yet in the pin,
the whole suite fails at import. The fix is to move the pin:

```bash
uv lock --upgrade-package bmlib
```

That is what unblocked the suite this session: the lock sat at bmlib 0.2.1
(`e227ec14`) while `db/operations.py` had already started importing
`bmlib.db.is_sqlite`, which only exists from 0.5.x.

## Work in flight: the notification service

Design: `docs/plans/2026-07-26-notification-service-design.md` (decisions and
rationale). Plan: `docs/plans/2026-07-28-notification-service-plan.md`
(task-by-task implementation).

A **watch** is named criteria that alert on a matching paper as it is scored,
separately from the periodic digest. A notified paper is still included in the
next digest.

Implemented:

- Migration 5 — the `notifications` table (one row per *delivered* notification,
  unique on `(watch, paper_id, channel)`).
- `NotificationsConfig`, with `channels` and `watches` as dicts-of-dicts —
  forced by `save_config`'s three-level serializer, which silently drops
  anything deeper and stringifies list elements.
- `notify/watches.py` — `Watch` / `Channel` parsed from those dicts, warning on
  unknown keys rather than ignoring them.
- `notify/matcher.py` — pure `(paper, watch) -> bool`, no I/O, no LLM.

Not yet implemented:

- `notify/channels/` — email (wraps `digest/sender.py`) and Matrix (plain
  authenticated HTTP PUT, no SDK).
- `notify/service.py` — `run_notify()`: select, page, dispatch, record.
- The `bmnews notify` CLI command and the `run_pipeline()` wiring.
- `notify_email.*` / `notify_matrix.*` templates.
- The GUI watches pane (deliberately last; nothing else depends on it).

**Nothing calls the matcher yet.**

Two traps the design calls out and the implementation has to honour:

1. **The pending queue is derived, never stored** — "papers this watch matches
   now, minus those already sent". That is what makes paging idempotent and
   stops an edited watch from leaving orphaned queue rows.
2. **A bare `LIMIT N` in SQL is wrong.** SQL narrows (score floors, tier
   exclusion, the not-already-sent anti-join, ordering); Python applies the
   rest (keywords, tags, sources, journal, study design). Limiting before the
   Python filter under-delivers silently. The top-up loop must distinguish
   "N matches collected" from "a chunk came back short" — only the second means
   exhaustion, and a batch filling exactly at a chunk boundary is not it.
3. **`notifications` must stay separate from `digest_papers`.**
   `get_papers_for_digest()` excludes papers in `digest_papers` and nothing
   else, so recording a notification there would silently suppress that paper's
   digest entry.

## Reference: how the `publications` migration was resolved

Kept because the reasoning is not recoverable from the diff.

**The blocker** was that `bmlib.publications` was SQLite-only. Backend-aware
SQL was upstreamed into bmlib (hherb/bmlib#28) rather than dropping PostgreSQL
from bmnews. Testing PostgreSQL properly for the first time turned up three
bmlib bugs this migration would otherwise have inherited: `fetch_scalar()`
always returned `None` (psycopg2's `RealDictRow` is keyed by name, so `row[0]`
raised into a silent fallback); `transaction()` did not nest, degrading
`sync()`'s one-commit-per-day batching to one commit per record; and
`create_tables()` committed mid-migration, leaving DDL applied after a failure.

The nesting fix has a trap worth remembering: nesting is counted by bmlib, not
read from psycopg2's transaction status. psycopg2 opens a transaction on the
first statement of *any* kind — a bare `SELECT` leaves the connection
`INTRANS` — so status-based detection would classify ordinary blocks as nested
and silently stop committing every write.

**What it bought:** `download_days` resume tracking (a failed day is recorded
and re-fetched, where bmnews used to refetch the whole window and lose it);
identity merging across sources; PMID-only records, which bmnews's
`NOT NULL UNIQUE` DOI column used to drop outright; and one transaction per
day, with the write lock held only for the store loop rather than across
network I/O.

**Schema reconciliation:** `authors` and `categories` strings became JSON lists
(`publications.authors` / `keywords`); `metadata_json.pub_type` became
`publication_types` and still feeds Tier-1 quality classification; a single
`source` became the `sources` array plus `first_seen_source`; `url` is derived
in `operations.paper_url()`; `pmcid` was upstreamed into bmlib, which had been
dropping `FetchedRecord.pmc_id` on store. `fulltext_html` / `fulltext_source`
and the `cited_by` extras went to `paper_extras` — bmlib's `fulltext_sources`
records *where* full text lives, not the fetched body, so it does not replace
the cache.

Two shapes deliberately did **not** change: `scores.paper_id` and friends keep
that name though they now reference `publications(id)` — "paper" is bmnews's
noun and the GUI routes are `/papers/<id>` — and `_row_to_paper()` stays the
single place a row becomes a paper dict.

**Merges.** `store_publication()` can collapse two `papers` rows into one
publication. `scores` is `UNIQUE(paper_id)`, so migration 4 keeps the highest
`combined_score` — the one the digest showed and the user acted on. Tags and
digest links union. A row keyed on neither DOI nor PMID cannot be represented;
it is logged at ERROR and written to `~/.bmnews/stranded-papers.json` before
`papers` is dropped. The migration is destructive and one-way.
