# GUI Watches Pane Design

**Date:** 2026-07-29
**Status:** design only — no implementation in this document.
**Completes:** `docs/plans/2026-07-26-notification-service-design.md`, whose
"Surfaces" section sketched this pane and left it unbuilt. Everything else in
that design shipped (`docs/plans/2026-07-28-notification-service-plan.md`).

## Problem

The notification service has a CLI and a pipeline stage but no GUI surface. In
the desktop app there is currently no way to see that a watch exists, no way to
see how much it has queued, and no way to pull the next batch — `max_per_run`
caps a run at, say, five papers and the other forty are visible only by running
`bmnews notify --list` in a terminal. Paging was chosen over truncation
precisely so nothing is silently dropped; without a surface that shows the
remaining count, the papers are dropped as far as a GUI-only user can tell.

`service.pending_counts()` already returns exactly what the pane needs, per
`(watch, channel)`, and sends nothing.

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Scope | **Monitor and deliver only** | Read the counts, pull a batch, drain the queue. Creating and editing watches stays in `config.toml`. Editing is a separate, larger surface: `WatchConfigError` would have to round-trip into form errors, and a channel form would put the Matrix `access_token` on screen. |
| Dry-run preview | **Out of scope** | The CLI's `--dry-run` is what makes a watch *tunable*, and tuning means editing — which is out of scope here. Listing pending papers without being able to change the criteria that select them is a view with no next action. |
| Placement | **A third nav tab, `/watches`** | Papers and Settings are already tabs; the pane is a peer view, not a section of Settings. |
| Background execution | **The existing job lock**, extracted to `gui/jobs.py` | Delivery must not race a pipeline run on the same database. Sharing `_pipeline_lock` is a hard requirement, so the only question is whether it is explicit or smuggled across a private import. |
| Progress reporting | **The existing status bar** | `run_notify(on_progress=...)` already emits one line per `(watch, channel)`. No new async machinery, as the notification design specified. |
| Count refresh | **Once, on completion** | Each refresh is a full `collect_matches()` scan per pair. Polling live would run 5–10 full scans every two seconds against the database the delivery job is writing to. |
| Delivery grain | **Per watch, all its channels** | The queues are per channel and the counts are displayed per channel, but a per-watch button already does the right thing per channel: a watch with email pending and Matrix exhausted delivers email only, and a retry after a failed email retries just the email. Per-channel buttons would add a column to serve no case. |

## Architecture

Three pieces. Only the first touches existing code.

### `bmnews/gui/jobs.py` — the shared background job

Today `_pipeline_lock`, `_pipeline_status` and `_start_pipeline_thread` are
module-level state in `gui/routes/pipeline.py`. The watches pane needs the same
lock — two delivery jobs, or a delivery and a pipeline run, must not overlap.

Both existing pipeline routes duplicate the same sequence: acquire
non-blocking, return the busy fragment if held, set a busy status, spawn a
daemon thread whose `finally` releases the lock, and handle the
`RuntimeError`-on-spawn case where the worker never ran and so its `finally`
never will. That sequence moves here once.

```python
def status() -> dict[str, Any]          # the live status dict
def running() -> bool
def start(*, message: str, target: Callable[[], None], error_label: str) -> bool
def wait_for_idle(timeout: float = 5.0) -> bool
```

`start()` returns `False` when a job is already in flight; the caller renders
the busy fragment. It catches an exception from *target*, logs it, and publishes
`f"{error_label}: {exc}"` with `status="error"` — so a target only has to
publish its own success message, which is what the existing `_run` closures do.
If the thread cannot be spawned at all it also returns `False`, after releasing
the lock the worker will never release and publishing the same error status;
the caller cannot tell that case from "busy" and does not need to, because both
mean "no job was started, show the user why".

`_scored_paper_ids` stays in `pipeline.py`: it is pipeline-specific, and the
OOB card machinery that drains it is too.

`wait_for_idle()` exists because the current GUI test asserts on a background
thread with `for _ in range(20): time.sleep(0.05)`. Joining is deterministic,
and shutdown has a use for it too.

### `bmnews/gui/routes/watches.py` — the pane

| Method | Path | Purpose |
|---|---|---|
| GET | `/watches` | The pane: one block per watch, one row per channel |
| POST | `/watches/<path:name>/notify` | One batch — `run_notify(config, watch=name)` |
| POST | `/watches/<path:name>/notify-all` | Drain — `run_notify(config, watch=name, drain=True)` |
| GET | `/watches/rows` | The completion poller (see below) |

`<path:name>` because a watch is named by its config table heading, which may
contain a slash; `<string:>` would 404 on it and the button would look broken.
A name not in the parsed set `abort(404)`s.

Each watch block shows its name, an enabled badge, a one-line criteria summary,
and per channel: `sent / matching / remaining`. The buttons are **Notify N
more** (N = the watch's `max_per_run`) and **Notify all remaining**.

**Three cases the pane must not hide.** Each is one where rendering
`pending_counts()` alone would show an empty or misleading pane:

1. **A watch whose channels resolve to nothing** produces *zero*
   `DeliveryReport` rows — `resolve_channels()` logs an ERROR and skips a name
   with no matching channel — so the watch would vanish from the pane
   altogether. The route therefore lists watches from `parse_watches()` and
   joins the reports onto them, marking a channel-less watch explicitly.
2. **A watch that fails validation** is skipped by `parse_watches()` with an
   ERROR log. The route diffs `set(config.notifications.watches) - set(parsed)`
   and lists those names as misconfigured, pointing at the log.
3. **Disabled watches, and `notifications.enabled = false`.** `run_notify()`
   returns `[]` for both, silently. Both get a badge or banner and **no**
   delivery buttons, so no button can be pressed and appear to do nothing.
   `pending_counts()` reports disabled watches deliberately — knowing what one
   *would* send is the point of being able to look — so their counts still
   render.

### Refresh: one scan, on completion

A notify POST returns two swaps in one response, the technique
`/pipeline/status` already uses:

- the busy `status_bar` fragment into `#status-right`, which starts the
  existing 2-second status poller and shows `run_notify`'s per-channel progress
  lines as they arrive;
- an OOB swap putting an `hx-get="/watches/rows" hx-trigger="every 2s"` poller
  into `#watch-poller`, a slot of its own next to `#watch-list`.

The poller gets its own slot rather than living among the rows so that starting
it costs nothing: re-rendering the rows on the POST would mean a full scan per
pair at the moment the delivery job starts changing the numbers it would
report.

`/watches/rows` returns **204 No Content** while `jobs.running()` — htmx does
not swap on a 204, so the poller stays alive and no scan is performed. The
first idle answer returns freshly scanned rows into `#watch-list` *and* an OOB
swap emptying `#watch-poller`, so one response both refreshes the counts and
retires the poller.

Net cost: no scans during delivery, exactly one when it finishes. Nothing is
swapped out-of-band into an element that may not be on the page, and the poller
dies with the pane if the user switches tabs.

`jobs.running()` is global rather than per-job, so a pipeline run started while
the poller is alive holds it at 204 until that run finishes too. That is the
wanted behaviour, not a compromise: the pipeline's own NOTIFY stage delivers,
and scoring moves papers into and out of the queue, so the counts are worth
re-reading once it is done and not before.

A **Refresh** button re-fetches `/watches` for the remaining stale case: a
pipeline run (whose NOTIFY stage delivers) finishing while the pane is open but
was not started from it.

## What the numbers mean

Worth stating, because the pane is where they get labels. The candidate
anti-join excludes only rows with `status = 'sent'`, and `count_notifications`
defaults to `status="sent"`. So a failed delivery is *not* counted in
`sent_total` and *is* counted in `remaining` — it stays queued and retries —
and `matching = sent_total + len(pending)` does not double-count it.

The pane shows no per-failure detail: `pending_counts()` fills only
`sent_total`, `matching` and `remaining`, and `DeliveryReport` carries no error
text. Failures surface in the status bar at delivery time and in the log.

## Testing

`tests/test_gui_notify.py`, following the existing GUI pattern — Flask test
client, `pending_counts` and `run_notify` patched, no SMTP and no HTTP.
`jobs.wait_for_idle()` replaces sleep-polling.

| Case | Assertion |
|---|---|
| Pane renders | A row per `(watch, channel)` with sent / matching / remaining |
| Channel-less watch | Still listed, marked, no delivery buttons |
| Unparseable watch | Listed as misconfigured |
| Disabled watch | Counts shown, no delivery buttons |
| `notifications.enabled = false` | Banner, no delivery buttons |
| POST notify | Returns busy status bar; `run_notify` called with the watch name |
| POST notify-all | `run_notify` called with `drain=True` |
| POST while a job runs | "already running", `run_notify` not called |
| Unknown watch name | 404 |
| `/watches/rows` while running | 204, no scan |
| `/watches/rows` when idle | Rows, poller absent |
| `run_notify` raises | Error status published, lock released (a second POST succeeds) |

The `jobs.py` extraction is covered by the existing pipeline route tests, which
must keep passing unchanged apart from the sleep-poll they no longer need.

## Documentation to update

`bmnews/gui/CLAUDE.md` (route table), `CLAUDE.md` (GUI directory tree, test
file table), `docs/user/usage.md` (the pane), `docs/dev/architecture.md`,
`HANDOVER.md` (the notification service becomes fully shipped).

## Out of scope

Creating, editing, enabling or deleting watches and channels from the GUI; the
dry-run paper preview; per-failure error detail in the pane; the LLM predicate
the notification design deferred.
