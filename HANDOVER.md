# Handover

## Where things stand

| Area | State |
|---|---|
| Storage on `bmlib.publications` | **Done.** No `papers` table; fetch+store is one `sync()` call. Decisions worth keeping are recorded below. |
| Multi-provider LLM + model selector | **Done.** Six providers via bmlib (`list_providers()` is the authority), settings UI datalist cached in `~/.bmnews/model_cache.json`. Design/plan: `docs/plans/2026-02-15-llm-providers-model-selector-*.md`. |
| Notification service | **Done.** CLI, pipeline stage, and the GUI watches pane — see below. |
| `docs/dev/` drift | **Done.** All six files rewritten against the current code ([issue #11](https://github.com/hherb/BioMedicalNews/issues/11)). |
| `bmlib.transparency` | **Done.** Wired up as a fifth pipeline stage, informs only — see below. |
| `docs/dev/` drift detection | **Open.** The rewrite above was verified by hand, and nothing in CI fails when a rename rots it again — [issue #16](https://github.com/hherb/BioMedicalNews/issues/16). |
| bmlib pin → 0.6.0 | **Done — on this machine, which turned out to be the only place a pin exists.** The local lock moved to 0.6.0 (`ec6683a9`) on 2026-08-01, suite green, no code change. `uv.lock` is gitignored, so there was no repo-level pin to move and CI has been resolving bmlib *main* all along — [issue #25](https://github.com/hherb/BioMedicalNews/issues/25) tracks whether that should change. |
| Digest templates don't escape metadata | **Fixed, PR open.** `digest_email.html` escapes every interpolation and carries the notify templates' explanatory comment ([PR #23](https://github.com/hherb/BioMedicalNews/pull/23), closes [#17](https://github.com/hherb/BioMedicalNews/issues/17)). `digest_text.txt` deliberately stays raw: it is a text/plain MIME part, matching `notify_email.txt`/`notify_matrix.txt` — the issue's premise that all four notify templates escape was wrong, only the HTML ones do. A test pins each half. |
| Reading pane shows literal `None` for a missing date | **Fixed, PR open.** Both `reading_pane.html` *and* `paper_card.html` (identical defect, found while fixing) now guard the date with `{% if %}`, as the `journal` line beside it already did ([PR #24](https://github.com/hherb/BioMedicalNews/pull/24), closes [#18](https://github.com/hherb/BioMedicalNews/issues/18)). The issue's option 2, deliberately: `_row_to_paper()` keeps leaving a date-semantic NULL as `None` for Python readers. |
| `uv.lock` untracked ↔ CI tests bmlib main | **Open, needs a decision.** Track the lock (`uv sync --locked` in CI), pin in `pyproject.toml`, or keep CI-as-canary and live with per-machine pins — [issue #25](https://github.com/hherb/BioMedicalNews/issues/25). Looks like a deliberate tandem-development choice, so it was lodged rather than changed. |

## Environment gotcha

`uv.lock` pins bmlib **by commit**, and `uv run` re-syncs to that pin — so
installing a newer bmlib by hand is silently undone on the next `uv run`.
When bmnews starts using a bmlib symbol that does not exist yet in the pin,
the whole suite fails at import. The fix is to move the pin:

```bash
uv lock --upgrade-package bmlib
```

**The pin is per-machine, not the repo's.** `.gitignore` ignores `uv.lock`
(line 141), and CI installs with `uv pip install -e`, resolving
`bmlib @ git+…` at whatever bmlib main is that day — so everything above
describes only the checkout you are sitting in, and a bmlib-main breakage
surfaces on whichever CI run happens next, not on a pin bump. That came to
light on 2026-08-01 while executing the tracked "bump the pin to 0.6.0"
follow-up, which therefore turned out to be a purely local operation:
this machine now runs 0.6.0 (`ec6683a9`), suite green, nothing to commit.
[Issue #25](https://github.com/hherb/BioMedicalNews/issues/25) holds the
track-the-lock / pin-in-pyproject / status-quo decision.

With 0.6.0, bmlib's `TransparencyResult.to_dict()` includes
`unknown_reason`, so it starts appearing inside `transparency.result_json`
on newly analysed papers — stored with the rest of the blob, read by
nothing. The transparency stage still avoids `TransparencyUnknownReason`
itself, so it keeps working on either side of the bump.

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

**The GUI watches pane shipped**, completing this service's last surface.
Design: `docs/plans/2026-07-29-gui-watches-pane-design.md`. `/watches` lists
every watch with, per channel, `delivered / matching / remaining`; **Notify N
more** posts to `/watches/<name>/notify` for one batch (the watch's
`max_per_run`), **Notify all remaining** to `/watches/<name>/notify-all` to
drain it. Rows are built from `parse_watches()` with `service.pending_counts()`
joined **onto** them, not from the counts alone — a watch naming no configured
channel produces no reports and would otherwise vanish, and one that fails to
parse is skipped by `parse_watches()` with only a log line, so the pane diffs
the configured names against the parsed ones to still list it. A channel list
that resolves only *partly* is the same failure at smaller scale —
`resolve_channels()` drops the unknown name and returns the rest, leaving a
healthy-looking table — so the row also diffs the names the watch asked for
against the reports that came back, and names what was dropped. A disabled
watch is different again: `pending_counts()` reports it deliberately (knowing
what it would send is the point of being able to look), so its counts render,
but it earns no delivery buttons. Delivery runs through `gui/jobs.py`,
extracted from the pipeline routes so both share one lock, one status dict
and one daemon thread — a delivery must not race a scoring run on the same
database. Counts refresh once, when the job finishes: the poller gets a 204
while one is running rather than re-scanning every candidate every two
seconds against a database that job is still writing to, and opening the tab
mid-run skips the scan for the same reason and says so in place of the table.
Which channels a watch resolves to is settled from `parse_channels()` rather
than from the reports that came back, which is what lets every one of those
config notices render on a page that gathered no counts at all. Nothing else
depends on the notification service now; it is fully shipped.

A channel name repeated in one watch (`channels = ["mail", "mail"]`) is now
dropped at parse time with a warning ([issue #14](https://github.com/hherb/BioMedicalNews/issues/14)).
It resolved to two identical `Channel` objects, and both `run_notify()` and
`pending_counts()` iterate that list — so the watch delivered twice in one run
(the second pass re-derives the queue, so it sent the *next* batch, silently
doubling `max_per_run`) and the pane rendered two identical rows. The fix is in
`Watch.from_config()`, so every caller sees the corrected list without having to
remember to de-duplicate, and again in `resolve_channels()` — silently, since
the parse has already warned. The second one is not redundant: `Watch` is
exported and directly constructible, so without it the guarantee rests on every
instance having come through the config parse, which nothing enforces.

Only an exact repeat of a channel *name* is caught. Two differently named
channels pointing at one address remain two deliveries, deliberately —
`notifications` keys retry state on the channel name, so they are separate
queues, and one failing says nothing about the other.

One thing to keep straight if you touch the pane's numbers: a report is per
`(watch, channel)`, so anything summed across channels counts **notifications**
— one paper on one channel — and not papers. Five papers going to email and
Matrix is ten notifications, and calling that "10 papers" is simply false. The
per-channel table columns are the only place a paper count is safe to render,
which is why the drain button carries no total and the terminal status line
says "N notification(s)".

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

## The transparency stage

Design: `docs/plans/2026-07-30-transparency-analysis-design.md`. Plan:
`docs/plans/2026-07-30-transparency-analysis-plan.md`. `[transparency]` had
sat in `config.toml` since the first release with nothing calling an
analyzer — `enabled = true` was a silent no-op. It is now a fifth pipeline
stage, `SYNC → SCORE → TRANSPARENCY → NOTIFY → DIGEST`, wired end to end:
config, the `transparency` table (migration 7), the read path, the stage
itself, the CLI, and all four display surfaces (GUI reading pane, digest,
notify templates, `bmnews transparency --list`).

**Placement mirrors NOTIFY, and for the same reasons.** Neither stage is
gated on `scored > 0` — a run with nothing newly scored can still have a
paper that just crossed `min_combined_score` on a re-score, exactly as NOTIFY
can still have a watch just loosened. Both are wrapped so a failure cannot
take the run down: sync and scoring have already done the expensive work by
the time either runs, and TRANSPARENCY specifically depends on five external
APIs, any of which can be down without that costing the digest.

**The one invariant not to undo: retries are bounded by an attempt count, not
by the reason a result came back UNKNOWN.** bmlib's analyzer sets its
"reachable" flag only on an HTTP 200, so it reports `UNREACHABLE` both for a
network outage *and* for a paper indexed in none of the five APIs — the two
are indistinguishable from the result alone. "Retry every `UNKNOWN`" would
therefore mean re-querying every unindexed preprint, four to eight requests
each, on every single run, forever. The `transparency.attempts` column stops
that at `TRANSPARENCY_MAX_ATTEMPTS` (3); `bmnews transparency --refresh`
resets it to 1, because an explicit re-analysis has to restore the automatic
retries too, or a single forced attempt would exhaust the whole budget on one
try. A determinate result (`low`/`medium`/`high`) is never re-selected
regardless of `attempts` — `get_transparency_candidates()`'s `risk_level`
test fails before `attempts` is even considered.

**The second selection invariant: a refresh run is ordered by staleness, not
by score.** The normal queue narrows itself — a paper drops out of it the
moment it holds a determinate result — so ordering that queue best-score-first
means every run starts on papers the last one never saw. A refresh run has no
such predicate; it selects everything above the gate. Ordered by score it
therefore returned the *identical* top-`limit` papers on every run, re-spending
four to eight requests per paper while the rest of the corpus was never reached
at all — so a corpus larger than one batch could not be refreshed by any number
of `--refresh` runs. `get_transparency_candidates()` switches to
`t.analyzed_at ASC NULLS FIRST` when `refresh` is set, which sorts the batch
just refreshed to the back and lets successive runs walk the corpus. **Keep
`NULLS FIRST` explicit**: SQLite sorts NULLs first in `ASC` and PostgreSQL
sorts them last, so dropping it passes the SQLite suite and strands
never-analysed papers at the back of the queue on PostgreSQL only.
`tests/test_db.py::TestTransparency::test_refresh_puts_a_never_analysed_paper_first`
runs on both backends and is what catches that.

**The config gate was renamed.** `min_score_threshold` → `min_combined_score`
— it sat one field away from bmlib's own `score_threshold`, which means
something else on a different scale (0.0–1.0 combined score vs. bmlib's
0–100 transparency score). Safe to rename because the feature had never run,
so no stored value needed migrating. `TransparencyConfig` carries the old
name forward via `_DEPRECATED_KEYS` with a log warning rather than silently
reverting a customised value to the default; `save_config` writes the new
name on the next save.

**It informs only, on purpose, and that is not a placeholder for later.**
Nothing filters or reranks on a transparency result — no selection query
reads `transparency_score`, the notify matcher is untouched, and bmlib's
`tier_downgrade_applied` is stored in `result_json` and never read back into
a `combined_score`. A value derived from five external APIs must not be able
to move a score the user has already acted on. Filtering and the tier
downgrade are both plausible additive follow-ups, not oversights — do not
"finish" this by wiring either one in without a fresh design conversation.

**The design avoids `TransparencyUnknownReason` deliberately** — it only
exists from bmlib 0.6.0, and the stage was shaped so the feature would
never force the pin to move. The local pin has since moved to 0.6.0 anyway;
see "Environment gotcha" above for what that does and does not mean.

## The developer docs

`docs/dev/` had drifted a long way behind the `publications` migration — it
still documented a `papers` table, `upsert_paper()`, `SCHEMA_SQLITE` in
`schema.py`, a `FetchedPaper` dataclass and a `fetchers/base.py`, none of which
exist. All six files are now written against the code as it stands:

- **`database.md`** — rewritten around the two owners (bmlib: `publications`,
  `fulltext_sources`, `download_days`; bmnews: `scores`, `paper_tags`,
  `digests`/`digest_papers`, `paper_extras`, `notifications`), the three-way
  join a "paper" actually is, the migration list (grown since this rewrite —
  see current count in the migration table itself rather than trusting a
  number here), the real operations reference, and the per-backend test
  setup.
- **`architecture.md`** — data-flow diagram and module graph redrawn; the
  notify path and the GUI blueprints added; backend-aware SQL corrected to
  `placeholder()`/`is_sqlite()` and per-migration DDL pairs.
- **`codebase.md`** — the missing packages (`notify/`, `gui/`, `constants.py`,
  `metadata.py`, `templating.py`) documented; the removed fetcher modules
  dropped.
- **`bmlib-integration.md`** — `bmlib.publications` and `bmlib.fulltext`
  sections added, the quality section corrected to `QualityManager`, and the
  transparency section corrected from "available when enabled" to "declared but
  nothing calls it".
- **`testing.md`** — the per-backend `new_db()` pattern, the autouse GUI-jobs
  fixture, and the real test-file list.
- **`contributing.md`** — the fetcher guide rewritten around bmlib's registry
  convention (the old one described a dispatch path that no longer exists).

Two things to know if you extend them: the docs are checked by reading, not by
a test, so a symbol renamed in code will not fail CI here; and `docs/user/`
still says `pip install` throughout, which is at odds with the project's
uv-only rule but is not wrong for an end user — worth a decision rather than a
silent edit.

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
