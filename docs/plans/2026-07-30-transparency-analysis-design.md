# Transparency Analysis Design

**Date:** 2026-07-30
**Status:** design only — no implementation in this document.

## Problem

`bmnews.config` has carried a `[transparency]` section since the first release
and `pyproject.toml` declares a `transparency` extra, but no bmnews code has
ever called an analyzer. Setting `enabled = true` changes nothing, silently.
That is the worst kind of dead config: it reads as a feature.

Two things have to be fixed together, because the second explains the first.

The config's own docstring says *"Settings for exposing scoring rationale to the
user"*. That describes nothing bmlib does. `bmlib.transparency` assesses
**research integrity** — whether funders are disclosed, whether a conflict-of-
interest statement exists, whether data is available, whether a registered
trial has posted its results — by querying CrossRef, Europe PMC, PubMed,
OpenAlex and ClinicalTrials.gov. It has nothing to do with why the scorer gave
a paper the relevance score it did.

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| What a result *does* | **Informs only** | Display a risk badge and the findings. Nothing changes which papers are selected or how they rank. A network-derived value must earn trust before it gates a digest, and `combined_score` staying independent of network results means a re-analysis can never silently move a score the user already acted on. |
| Quality tier downgrade | **Not honoured** | `TransparencyResult.tier_downgrade_applied` is read and stored, never applied. Applying it would make `quality_score`, `combined_score` and every existing tier floor depend on five external APIs. |
| When analysis runs | **Its own pipeline stage** | The gate needs `combined_score`, so it must follow SCORE; the digest and notify templates display the badge, so it must precede both. A separate stage is independently runnable, resumable, and keeps a rate-limited HTTP call out of a `ThreadPoolExecutor` sized for LLM concurrency. |
| Which papers | **Scored, above `min_combined_score`** | Each analysis costs four to eight external requests. Analysing papers the user will never see is the one cost that buys nothing. |
| Re-analysis | **Never automatic; `--refresh`** | Transparency genuinely changes over time, but an age-based refresh adds recurring API cost that grows with the corpus and mutates a stored result without being asked. |
| Retry of an indeterminate result | **Bounded by an attempt count** | See "Why not `unknown_reason`" below. |
| bmlib pin | **Stays put** | Nothing in this design needs a bmlib symbol the pinned commit lacks. |

## Why not `unknown_reason`

The obvious retry rule is "re-attempt anything bmlib reported as `UNREACHABLE`,
leave `NO_IDENTIFIER` alone", using the `TransparencyUnknownReason` enum bmlib
added for exactly this kind of branching. It does not work, for two independent
reasons.

**It does not distinguish what it appears to.** `TransparencyAnalyzer` sets its
`_api_reachable` flag only on an HTTP 200. A paper indexed in none of the five
APIs answers 404 everywhere, so it is reported `UNREACHABLE` — the same value a
genuine network outage produces. "Retry every `UNREACHABLE`" therefore means
re-attempting every unindexed preprint on every run, four to eight requests
each, against a corpus that only grows. The enum cannot separate the transient
case from the permanent one because bmlib cannot either.

**The pinned bmlib does not export it.** `TransparencyUnknownReason` is on
bmlib's `origin/main` (`ced28eb`) but not in the commit `uv.lock` resolves to,
and `uv run` re-syncs to that pin — so importing it would fail the whole suite
at collection, the trap HANDOVER.md documents. Moving the pin would also pull in
bmlib's `parse_json` contract change, which `bmnews.scoring.relevance_agent`
depends on. Neither risk is worth taking for a symbol that does not solve the
problem.

So retries are bounded by a stored attempt count instead: an outage retries a
few times and succeeds, an unindexed paper stops after
`TRANSPARENCY_MAX_ATTEMPTS` and displays as UNKNOWN, honestly. `unknown_reason`
still reaches storage inside `result_json` — the blob is whatever
`TransparencyResult.to_dict()` produced, so the key starts appearing by itself
if the pin ever moves, with no migration and no code change.

## The extra is vestigial

`bmlib[transparency]` is exactly `httpx>=0.25`, and bmnews already declares
`httpx>=0.25` as a **core** dependency (the Matrix channel needs it). So the
`transparency` extra installs nothing a bmnews user does not already have, and
the `ImportError` path in bmlib's `analyze()` is unreachable from bmnews. The
extra stays declared as documentation of the dependency's origin, but no code
guards against its absence and the install docs should stop implying it is
required.

## Configuration

```python
@dataclass
class TransparencyConfig:
    enabled: bool = False
    min_combined_score: float = 0.6   # gate: which papers are worth analysing
    score_threshold: int = 40         # bmlib's HIGH-risk cutoff, 0–100
    concurrency: int = 3
```

`min_score_threshold` → `min_combined_score` resolves a genuine collision. The
old name sits one section away from `scoring.min_combined` while meaning the
same kind of thing, and one field away from bmlib's `score_threshold`, which
means something else entirely on a different scale: ours gates *which papers
get analysed* (a combined score, 0.0–1.0), bmlib's decides *what counts as HIGH
risk* (a transparency score, 0–100). Two fields called `*_score_threshold` in
one section, disagreeing about both subject and scale, is a bug waiting to be
filed.

Renaming is safe precisely because the feature never ran: a user who changed
`min_score_threshold` has been changing nothing. It is still carried forward
rather than dropped, through a `_DEPRECATED_KEYS: dict[type, dict[str, str]]`
map consulted by `_apply_section`, which warns and assigns to the new name. The
value survives, the warning names the fix, and `save_config` writes the new key
on the next save. `_apply_section` ignores unknown keys silently, so without
this the rename would quietly revert a customised gate to its default.

`concurrency` is separate from `llm.concurrency` because the two bound
different resources — one an LLM endpoint, the other a set of public HTTP APIs
with a shared rate limit.

### How it maps onto bmlib's `TransparencySettings`

bmlib's settings object has eight fields to bmnews's four, and which of the
remaining ones matter is not obvious — two of them affect the risk level we
display even though this design does not honour the downgrade.

| bmlib field | bmnews supplies | Effect here |
|---|---|---|
| `enabled` | `True`, unconditionally | The stage returns early when `config.transparency.enabled` is false, so the analyzer is never constructed. Passing the config value through instead would let a direct `run_transparency()` call store a row full of bmlib's `DISABLED` placeholder as though it were a finding. |
| `score_threshold` | `config.transparency.score_threshold` | The 0–100 cutoff below which `calculate_risk_level()` returns HIGH. |
| `industry_funding_triggers_downgrade` | bmlib default (`True`) | **Affects the displayed risk level**, not only the tier downgrade: with it set, industry funding plus restricted data reads as HIGH. |
| `missing_coi_triggers_downgrade` | bmlib default (`True`) | Likewise — an explicit missing COI reads as HIGH. Only an explicit `False` counts; an undeterminable `None` does not. |
| `tier_downgrade_amount` | bmlib default (`1`) | Computed into `tier_downgrade_applied` and stored, never applied. |
| `filtering_enabled` | bmlib default (`False`) | Caller-honoured, and this caller does not filter. Left false so the settings object does not claim otherwise. |
| `max_concurrent_analyses` | `config.transparency.concurrency` | Caller-honoured; bmnews sizes its own pool from the same value, passed through so the settings object cannot disagree with the pool that was actually built. |
| `cache_results` | bmlib default (`True`) | Caller-honoured. Every result is stored, so this is already true. |

The two downgrade flags keeping bmlib's defaults is a real choice, not an
omission: they are what makes an industry-funded paper with restricted data show
as HIGH rather than MEDIUM. Exposing them is deferred until the badge has shown
whether those defaults are right for this corpus.

## Storage

A new bmnews-owned table, added by `Migration(7, "add_transparency", ...)` as a
DDL pair like every migration before it — SQLite:

```sql
CREATE TABLE IF NOT EXISTS transparency (
    paper_id INTEGER PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
    transparency_score INTEGER NOT NULL DEFAULT 0,
    risk_level TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 1,
    result_json TEXT NOT NULL DEFAULT '{}',
    analyzed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_transparency_risk ON transparency (risk_level);
```

and PostgreSQL, differing only in the timestamp column, as `scores` already
does: `analyzed_at TIMESTAMP NOT NULL DEFAULT NOW()`.

`paper_id` is the primary key directly rather than a surrogate `id` with
`UNIQUE(paper_id)`: there is exactly one result per paper, and it gives the
upsert its conflict target for free. It keeps the name `paper_id` like every
other bmnews table that references a publication.

`result_json` holds `TransparencyResult.to_dict()` whole, mirroring
`scores.assessment_json`. That is what makes a field bmlib adds later free —
including `unknown_reason`, as above.

`attempts` is the bound from "Why not `unknown_reason`". Candidate selection is
*no row, **or** `risk_level = 'UNKNOWN'` and `attempts < TRANSPARENCY_MAX_ATTEMPTS`*,
with `TRANSPARENCY_MAX_ATTEMPTS = 3` in `constants.py`. An attempt count rather
than a `last_attempt_at` timestamp keeps the selection free of clock arithmetic,
which would otherwise be a third piece of backend-specific SQL to maintain
across the two backends.

Three rules govern the column, and each is load-bearing:

- **A first result inserts `attempts = 1`.** The row records an analysis that
  happened, not one that is pending.
- **A repeat analysis increments it** — `attempts = transparency.attempts + 1`
  on the conflict path. Without this the cap never binds and an unindexed paper
  is retried forever, which is the whole failure the column exists to prevent.
- **`--refresh` resets it to 1.** An explicit refresh is the user asking for the
  work to be redone, so it must also restore the automatic retries; otherwise
  refreshing a paper that has already exhausted its attempts would analyse it
  once and immediately re-exhaust the cap, and a subsequent outage would leave
  it permanently UNKNOWN with no way back short of editing the row.

A determinate result therefore never re-enters the queue whatever its
`attempts` value, because the `risk_level = 'UNKNOWN'` half of the condition
fails first.

Papers with neither DOI nor PMID are excluded in SQL. Migration 4 established
that a publication keyed on neither cannot exist, so the filter is defensive
rather than load-bearing — but it is free, and it is what guarantees bmlib's
`NO_IDENTIFIER` path is never reached, which is the other reason the attempt
count needs no reason codes.

## The stage

`bmnews/transparency/service.py`:

```python
run_transparency(config, *, refresh=False, paper_id=None, limit=None,
                 dry_run=False, on_progress=None) -> TransparencyReport
```

A package rather than a module, symmetrical with `notify/`, and named
`bmnews.transparency` beside `bmlib.transparency` without ambiguity because
every import in the project is absolute.

It returns a report rather than a bare count, because four outcomes need telling
apart and a single integer conflates them:

```python
@dataclass
class TransparencyReport:
    candidates: int      # papers selected; the only field --dry-run fills
    analyzed: int        # results stored, determinate or not
    indeterminate: int   # subset of `analyzed` that came back UNKNOWN
    exhausted: int       # subset of `indeterminate` now at the attempt cap
    failed: int          # analyses that raised; no row written, so retried
```

`indeterminate` and `exhausted` are nested subsets, not disjoint buckets:
`analyzed - indeterminate` is how many papers were actually assessed, and
`exhausted` is how many of the rest will never be attempted again without
`--refresh`. That last number is the one worth surfacing, because it is the only
outcome the user cannot fix by waiting.

`run_transparency()` **returns early when `config.transparency.enabled` is
false**, before constructing an analyzer or opening a connection. That is not
merely an optimisation: bmlib's `analyze()` answers a disabled call with an
`UNKNOWN`/`DISABLED` placeholder result, and storing one would write a row that
reads like a finding and, worse, satisfies the "no row" half of the candidate
condition so the paper is never analysed once the feature is switched on.

**One analyzer instance shared across the pool.** bmlib's rate-limit lock is
per-instance and documented as spanning every thread using that analyzer, and
`_api_reachable` is thread-local for exactly this case — a second analyzer would
defeat the rate limit, and sharing reachability state across threads would let a
thread whose APIs were all down inherit another's success and be scored 0 (HIGH)
instead of UNKNOWN.

**Results are stored on the calling thread**, in the `as_completed` loop, the
same discipline `score_papers`' progress callback follows and for the same
reason: a SQLite connection is not safe to touch from a worker.

**A paper that raises is logged and skipped**, leaving no row, so it is
retried — matching `score_papers`, where one unscoreable paper costs only
itself.

**Throughput does not scale with `concurrency`.** bmlib enforces a 0.35 s
minimum interval between requests across the whole analyzer, and one analysis
makes four to eight of them (CrossRef, a Europe PMC search, its full text,
PubMed `efetch`, OpenAlex, and up to three ClinicalTrials.gov lookups). That is
1.4–2.8 s of mandatory spacing per paper no matter how many threads are used;
concurrency only hides per-request latency, it cannot raise the ceiling. Hence
`TRANSPARENCY_BATCH_SIZE = 100` in `constants.py` — a few minutes of wall clock
— with the same "more may remain, re-run" warning `run_score` already gives at
`UNSCORED_BATCH_SIZE`. `--limit` overrides it downwards for a trial run.

### Placement in the pipeline

```
SYNC → SCORE → TRANSPARENCY → NOTIFY → DIGEST
```

After SCORE because the gate reads `combined_score`; before NOTIFY and DIGEST
because both render the badge. Two properties are inherited deliberately from
the NOTIFY stage:

- **Not gated on `scored > 0`.** Loosening `min_combined_score` must pick up
  papers already scored on a run that scores nothing new, and a bounded retry
  is still owed its next attempt.
- **Failure-contained**, in a `_run_transparency_stage()` mirroring
  `_run_notify_stage()`. Sync and scoring have already done the expensive work
  by then; an unreachable CrossRef must not cost the digest that would
  otherwise have gone out.

It is additionally gated on `config.transparency.enabled`, which — unlike
today — will then mean something.

## Read path

`_PAPER_FROM` gains `LEFT JOIN transparency t ON t.paper_id = p.id`, and a
`_TRANSPARENCY_COLUMNS` fragment (`t.risk_level AS transparency_risk,
t.transparency_score`) joins the digest, GUI and tag SELECTs.

`get_notification_candidates()` builds its own FROM and gets the join and those
two columns added there — deliberately **not** `result_json`. The reasoning
already recorded at `operations.py:589` applies unchanged: that scan walks the
whole candidate set rather than one page, so a column it does not need is
multiplied by every candidate.

Only `get_paper_with_score()` selects `t.result_json AS transparency_json`,
because the reading pane is the one surface that renders the findings.

Joining `_TRANSPARENCY_COLUMNS` into the GUI's list query makes the badge
*available* to `paper_list.html`; this design does not put it there. The list
already carries relevance, tier and design badges in a narrow column, and a
fifth would crowd it. Two small indexed columns cost nothing to select, and
having them join by the same route as every other score column is what keeps
the alternative a template change rather than a query change.

`_row_to_paper()` stays the single place a row becomes a paper dict:
`transparency_risk` joins `_NULLABLE_TEXT_COLUMNS`, so a LEFT JOIN miss decodes
to `""` rather than `None`, and `transparency_json` becomes `paper["transparency"]`
through a defensive decoder alongside `metadata.parse_metadata`. A paper with no
result therefore renders as nothing at all, exactly as `quality_tier` and
`study_design` already do — no template needs an "or not yet analysed" branch.

The decoder returns a plain dict rather than reconstructing
`TransparencyResult`. `from_dict()` raises on an `unknown_reason` value it does
not recognise, which is right for bmlib and wrong here: a display surface must
not fail to render a paper because a newer bmlib wrote a member this one has
not heard of.

## Surfaces

Display only. No matcher criterion, no selection change, nothing that alters
which papers are chosen — that is what "informs only" means.

**GUI reading pane.** A risk badge beside the existing relevance, tier and
design badges, plus a findings block (funder disclosure, data availability, COI,
trial registration and posted results) and the `risk_indicators` list. Flask's
Jinja autoescapes, so the funder names inside those indicators are safe here.

**Digest and notify templates.** The badge only — `risk_level` and
`transparency_score`, an enum value and an integer, both ours. Keeping the
API-derived indicator strings away from bmlib's `TemplateEngine`, which runs
`autoescape=False`, leaves that surface with no injection exposure at all rather
than an escaped one. Interpolations are still escaped explicitly, as all four
`notify_*` templates already do.

**CLI.** `bmnews transparency`, reusing `bmnews notify`'s flag vocabulary. Each
flag's exact effect, since "narrow the selection" is ambiguous in more than one
way here:

| Flag | Effect |
|---|---|
| *(none)* | Run the stage over the normal queue, up to `TRANSPARENCY_BATCH_SIZE`. |
| `--limit N` | Cap the batch below the default. |
| `--refresh` | Re-analyse papers that already hold a determinate result, and reset `attempts` to 1. Still subject to `min_combined_score` and the batch cap, so it is not an accidental way to re-analyse the whole corpus. |
| `--paper-id ID` | Restrict to one paper and **bypass `min_combined_score`** — the user named that paper, so a gate meant to avoid spending requests on papers nobody will read does not apply. Alone it selects nothing if the paper already has a determinate result; combine with `--refresh` to redo it. |
| `--list` | Print stored results — risk level, transparency score, indicators — instead of analysing anything. Ordered worst risk first, honouring `--limit`. |
| `--dry-run` | Report the selection (`TransparencyReport.candidates`) and stop. No analyzer is constructed, no request is made, no row is written. |

`--dry-run` earns its place here more than elsewhere: it reports what a loosened
`min_combined_score` would cost before any request is spent.

## Testing

`tests/test_transparency.py`, with `TransparencyAnalyzer.analyze` mocked — no
HTTP, no LLM. It pins:

- the gate — an unscored paper and one below `min_combined_score` are not
  selected
- the `attempts` ceiling — an UNKNOWN result retries until the cap and then
  stops being selected; a determinate result is never re-selected
- `--refresh` overriding both, and `--paper-id` narrowing to one paper
- `--dry-run` storing nothing and calling no analyzer
- a raising analysis leaving no row and not aborting the rest of the batch
- a disabled config building no analyzer at all
- the batch cap warning when a run fills it

`tests/test_db.py` covers the table's operations, migration 7 against an
existing database, the candidate-selection SQL, and the LEFT JOIN miss decoding
to `""`. This touches both `db/operations.py` and `db/migrations.py`, so the
PostgreSQL half must actually run rather than skip:
`BMNEWS_TEST_PG_DSN=... uv run pytest tests/test_db.py -v`.

Then the config rename in `test_config.py` (old key carried forward with a
warning, new keys honoured, `save_config` round-trip), stage placement and
containment in `test_pipeline.py`, and the badge in `test_digest.py`,
`test_gui_app.py` and `test_notify_channels.py`.

## Documentation to update

`docs/user/configuration.md` (the section rewritten — rename, new fields, and
what the analysis actually assesses), `docs/user/usage.md` (the new command),
`docs/user/installation.md` (drop "declared but not yet wired up"),
`docs/dev/bmlib-integration.md` (its transparency section currently says
"not wired up"), `docs/dev/database.md` (the table and migration 7),
`docs/dev/architecture.md` (both diagrams gain the stage),
`docs/dev/codebase.md` (the new package), `CLAUDE.md` (transparency leaves
"Not currently used"), and `HANDOVER.md`.

## Deliberately out of scope

- **Tier downgrade and digest filtering.** Both are additive later: the
  downgrade is already stored in `result_json`, and filtering is one argument
  on `get_papers_for_digest()`. Neither is designed here, because neither
  should be built before the badge has shown what the analyzer actually
  concludes about a real corpus.
- **A transparency watch criterion.** Same reason — the matcher stays untouched
  so that a watch's behaviour cannot change because an external API did.
- **A GUI settings pane section.** `[quality]` and `[notifications]` have none
  either; the settings pane covers sources, scoring, LLM, user and email.
  Adding transparency alone would be inconsistent in both directions.
