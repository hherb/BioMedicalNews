# Handover

## Where things stand

| Area | State |
|---|---|
| Storage on `bmlib.publications` | **Done.** No `papers` table; fetch+store is one `sync()` call. Decisions worth keeping are recorded below. |
| Multi-provider LLM + model selector | **Done.** Six providers via bmlib (`list_providers()` is the authority), settings UI datalist cached in `~/.bmnews/model_cache.json`. Design/plan: `docs/plans/2026-02-15-llm-providers-model-selector-*.md`. |
| Notification service | **Done except the GUI watches pane** — see below. |
| `bmlib.transparency` | **Unused.** Config section and packaging extra declared, analyzer never called. This is now the largest open item. |
| `docs/dev/` drift | `architecture.md` and `database.md` still describe the removed `papers` table — [issue #11](https://github.com/hherb/BioMedicalNews/issues/11). |

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

## The notification service

Design: `docs/plans/2026-07-26-notification-service-design.md` (decisions and
rationale). Plan and the four deviations from it:
`docs/plans/2026-07-28-notification-service-plan.md`.

A **watch** is named criteria that alert on a matching paper as it is scored,
separately from the periodic digest. A notified paper is still included in the
next digest.

Shipped: the `notifications` table, `NotificationsConfig`, `notify/watches.py`
and `notify/matcher.py` (all of which predate this session), plus
`notify/channels/` (email + Matrix), `notify/renderer.py`, `notify/service.py`,
the four `notify_*` templates, the `bmnews notify` CLI and the NOTIFY stage in
`run_pipeline()`.

**Still to do: the GUI watches pane.** The design sketches it as a pane
listing each watch with `delivered / matching`, plus "Notify N more" and
"Notify all remaining" buttons posting to `/notify/<watch>`, delivery running
in a daemon thread behind the existing `_pipeline_lock` and reporting through
the same status-bar fragment as the pipeline routes. `service.pending_counts()`
already returns exactly what the pane needs to render, per `(watch, channel)`.
Nothing else depends on it.

Three invariants to preserve if you touch any of this. Each of them is the
whole reason some piece is shaped the way it is:

1. **The pending queue is derived, never stored** — "papers this watch matches
   now, minus those already sent over this channel". That is what makes paging
   idempotent and stops an edited watch from leaving orphaned queue rows.
2. **A bare `LIMIT N` in SQL is wrong.** SQL narrows (score floors, tier
   exclusion, the not-already-sent anti-join, ordering); Python applies the
   rest (keywords, tags, sources, journal, study design). Limiting before the
   Python filter under-delivers silently. `collect_matches()` scans in
   `NOTIFY_SCAN_CHUNK`-sized chunks until one comes back short and returns
   *every* pending match; `_deliver()` slices the batch off that. The chunk is
   a scan window, never a delivery cap. The scan runs to the end because
   `remaining` has to be exact — affordable only because
   `get_notification_candidates()` selects `_NOTIFY_PAPER_COLUMNS` rather than
   `_PAPER_COLUMNS`, whose `p.*` would drag the GUI's cached full text through
   a query that materialises every candidate.
3. **`notifications` must stay separate from `digest_papers`.**
   `get_papers_for_digest()` excludes papers in `digest_papers` and nothing
   else, so recording a notification there would silently suppress that paper's
   digest entry.

Three smaller ones worth not rediscovering. An adapter raises `ChannelError`
rather than returning a boolean, because `send_email` returns `False` on
failure and a `False` read as success marks papers sent and drops them out of
the queue forever — and `ChannelError` is the *only* exception `run_notify()`
reads as "this delivery did not happen", so a transport failure has to be
converted into one (`MatrixChannel._request()` does that; without it an `httpx`
connect error abandons every watch left in the run). Matrix's `txnId` is
derived from `(watch, channel, sorted paper_ids)`, never randomly, because the
homeserver treats a repeat as a retransmission — which is the only thing
closing the "message sent, row not yet written, crash" window; the batch is
recorded in one transaction for the same reason, since half a recorded batch
resends the rest under a different `txnId`. And the four `notify_*` templates
escape their own interpolations, because bmlib's `TemplateEngine` runs with
`autoescape=False`.

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
