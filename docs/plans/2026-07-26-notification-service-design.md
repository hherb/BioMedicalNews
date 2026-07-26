# Notification Service Design

**Date:** 2026-07-26
**Status:** design only — no implementation in this document.

## Problem

The digest is a periodic roundup: it batches whatever crossed the threshold
since last time and mails it. There is no way to say "if a paper like *this*
turns up, tell me about it now, separately from the digest".

This adds **watches** — named criteria that, when a newly scored paper matches
them, deliver a notification over email or Matrix.

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| When watches are evaluated | **After scoring only** | Every criterion worth writing refers to relevance, quality tier or matched tags, none of which exist before the scorer runs. Evaluating at ingest time would mean a second, weaker matcher over raw metadata for no real gain. |
| Criteria language | **Declarative typed fields**, AND-combined | Validates cleanly, tests without an LLM, and renders as a GUI form. |
| LLM predicate | **Deferred** | The declarative fields cover the known use cases. Adding a free-text predicate later is additive — one extra field on a watch, one extra filter stage. |
| Channels | **Email + Matrix** | Email reuses `digest/sender.py` unchanged. Matrix is a plain HTTP PUT (no SDK), the user already self-hosts a homeserver, and Matrix clients are free and ubiquitous — which matters if this ever becomes a public service. |
| Over-cap behaviour | **Paging, not truncation** | `max_per_run` bounds one batch; the rest stay visible as "remaining" and can be pulled on demand until exhausted. Nothing is ever silently dropped. |
| Notified papers in the digest | **Yes, still included** | A notification is "now"; the digest is the record. Zero work — see below. |

That last one is free: `get_papers_for_digest()` excludes papers present in
`digest_papers` and nothing else, so as long as notifications live in their own
table, a notified paper stays digest-eligible automatically. The corollary is a
constraint worth stating explicitly: **notifications must not reuse
`digest_papers`**, or delivering an alert would silently suppress the digest
entry for that paper.

## Where it hooks into the pipeline

A fourth stage, symmetrical with the existing three:

```
SYNC → SCORE → NOTIFY → DIGEST
```

`run_notify()` goes into `run_pipeline()` after `run_score()`. Two details:

- **It is query-based, not callback-based.** `run_score()` already exposes a
  per-paper `on_scored(paper_id)` hook, but a stage that *queries* for
  scored-but-unnotified papers is resumable across a crash and testable
  without running the scorer at all. It is also structurally the same query as
  `get_papers_for_digest()`, so it reuses a shape the codebase already has.
- **It must not be gated on `scored > 0`.** `run_digest()` currently is
  (`pipeline.py:480`), which is fine for a digest. A notify run with nothing
  newly scored still has work to do: retrying a delivery that failed last
  time, and picking up papers that a just-loosened watch now matches.

A consequence of query-based matching: loosening a watch's threshold makes it
match papers already in the database, so history resurfaces. That is the right
behaviour — and it is exactly the case paging protects against turning into a
flood.

One thing this design gives up by being score-gated: on local Ollama with
`concurrency = 1`, a long scoring run means alerts arrive as a batch at the end
rather than trickling in. Firing incrementally from `on_scored` is a later
addition if that becomes annoying; it changes nothing structural.

## Storage: deliveries only, queue derived

**Migration 5** adds one table:

```sql
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,               -- SERIAL on PostgreSQL
    watch TEXT NOT NULL,
    paper_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,                 -- 'sent' | 'failed'
    attempts INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (watch, paper_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_notifications_lookup
    ON notifications (watch, channel, status);
```

The unique key includes `channel` because one watch can deliver to both email
and Matrix, and one can succeed while the other fails — retry state is
per-channel or it is wrong.

**The pending queue is not stored.** It is derived on each run: papers that
match watch *W* now, minus those with a `sent` row for *(W, channel)*, ordered
by combined score descending. This is what makes paging fall out for free:

- `max_per_run` takes the top N of that derived queue.
- "Give me 5 more" runs the identical selection again — the 5 just delivered
  now have `sent` rows, so the next 5 come out. Idempotent, no queue state
  that can go stale or need reconciling.
- "Exhausted" is simply the selection returning fewer rows than asked for.
- A `failed` row is *not* excluded, so a failed delivery is retried on the next
  run and its row upserted on the unique key.

Had the queue been materialised, editing a watch's criteria would have left
orphaned rows queued under criteria that no longer match, needing a
reconciliation pass. Deriving it sidesteps that entirely.

### SQL narrowing vs. Python matching

The matcher stays a pure function — `(paper, watch) -> bool` over the dict
`_row_to_paper()` already produces — so the criteria engine unit-tests against
in-memory dicts with no database, no SMTP and no LLM. That splits selection
in two:

- **SQL** does the indexable narrowing and the ordering: score floors, tier
  exclusion, the not-already-sent anti-join, `ORDER BY combined_score DESC`.
- **Python** applies the rest: keyword substring, tag sets, sources, journal,
  study design.

This means **a bare `LIMIT N` cannot be the whole story.** Limiting to 5 rows
before the Python filter rejects 3 of them would deliver 2 while more matches
sat further down — silent under-delivery, and paging that appears to exhaust
early.

The fix is a top-up loop in Python: fetch a chunk (`NOTIFY_SCAN_CHUNK`), filter
it, and if fewer than N matches have accumulated, fetch the next chunk from
where the last one ended and keep going. Ask for 5, have 3 rejected, fetch more
and take 3 further matches. The loop terminates on one of two conditions, and
they mean different things:

- **N matches collected** — a full batch; more may remain.
- **A chunk comes back short of `NOTIFY_SCAN_CHUNK`** — the narrowed set is
  genuinely exhausted, which is what "no papers remaining" reports.

Conflating those two is the bug to avoid: a batch that happens to fill exactly
at a chunk boundary is not exhaustion.

The GUI's "N remaining" count comes from the same scan with no limit. On a
personal corpus, behind score-floor narrowing, that is milliseconds; if it ever
stops being cheap, cache it per run rather than approximating it.

## Configuration

A hard constraint shapes this. `save_config()`'s `_write_section`
(`config.py:321`) supports exactly three levels — `[section.field.key]` with
scalar or list-of-scalar leaves. Two failure modes follow:

1. **Anything four levels deep is silently dropped on every GUI save.** So a
   per-watch `headers = {...}` dict is out. Channels are typed by a `kind`
   field, with a small adapter per kind that knows how to shape the request —
   which is better config anyway than hand-assembled HTTP headers.
2. **A list of dicts is silently corrupted.** `_toml_value` renders a list by
   stringifying each element (`config.py:280`), so `[[notifications.watch]]`
   would round-trip as TOML strings of Python dict reprs. Watches are therefore
   keyed by name — a dict of dicts, exactly as `sources.source_options`
   already is.

Both shapes below round-trip through the existing serializer untouched:

```toml
[notifications]
enabled = true

[notifications.channels.matrix]
kind = "matrix"
homeserver = "https://matrix.example.org"
access_token = "syt_..."
room = "#bmnews-alerts:example.org"

[notifications.channels.mail]
kind = "email"          # reuses the [email] SMTP settings
to_address = "me@example.org"

[notifications.watches.melanoma-trials]
enabled = true
min_relevance = 0.8
min_combined = 0.0
min_quality_tier = "TIER_4_EXPERIMENTAL"
tags = ["melanoma", "immunotherapy"]
keywords = []
sources = []
study_designs = []
channels = ["matrix", "mail"]
max_per_run = 5
```

`_apply_section` setattrs raw dicts with no validation, so a typo'd criterion
key would be silently ignored. `notify/watches.py` therefore parses these dicts
into `Watch` / `Channel` dataclasses and **warns on unknown keys** rather than
dropping them quietly.

New `NotificationsConfig` dataclass, added to `section_map` in `load_config`,
to the `_write_section` calls in `save_config`, and to `DEFAULT_CONFIG_TOML`
as a commented-out example.

## Module layout

```
bmnews/notify/
├── __init__.py
├── watches.py     # Watch/Channel dataclasses, parse + validate from config dicts
├── matcher.py     # pure: (paper, watch) -> bool. No I/O, no LLM
├── channels/
│   ├── __init__.py    # kind -> adapter dispatch
│   ├── email.py       # wraps digest.sender.send_email
│   └── matrix.py      # httpx PUT to the client-server API
└── service.py     # run_notify(): select, page, dispatch, record
```

Templates, via the existing `TemplateEngine` with its `~/.bmnews/templates/`
override:

| Template | Purpose |
|---|---|
| `notify_email.html` / `.txt` | Email body, both alternatives |
| `notify_matrix.html` / `.txt` | Matrix `formatted_body` and its required plain-text `body` |

`digest_email.html` is deliberately **not** reused for Matrix: it is CSS-heavy,
and Matrix's HTML subset has no CSS support at all (see below).

## Matrix delivery

Sending is one authenticated HTTP PUT — no SDK, no new dependency beyond the
`httpx` already in use:

```
PUT {homeserver}/_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}
Authorization: Bearer {access_token}

{
  "msgtype": "m.text",
  "body": "3 new papers match melanoma-trials\n\n...",
  "format": "org.matrix.custom.html",
  "formatted_body": "<h4>3 new papers …</h4><ul><li>…</li></ul>"
}
```

Three things to get right:

**The transaction ID is an idempotency key, and we should exploit it.** The
homeserver treats a repeat PUT with the same `txnId` and path as a
retransmission and returns the original `event_id` without posting again. The
dangerous window in any notifier is *message sent, database row not yet
written* — a crash there means a duplicate on retry. Deriving `txnId`
deterministically from `(watch, channel, sorted paper_ids)` closes that window:
the retry is a server-side no-op. This complements the `notifications` table
rather than replacing it — transaction IDs are scoped per device/access token
and retained only for a bounded window, so they cover the crash case, not
general dedup. Email has no equivalent; a duplicate email after a crash at
exactly that point is the accepted worst case.

**Encrypted rooms will not work, and are not wanted.** A plain HTTP PUT cannot
produce a readable message in an E2EE room — that needs megolm, i.e.
`matrix-nio` with `libolm`, a heavy dependency. This is a deliberate
non-requirement rather than a limitation to work around: the content is
notifications about public preprints, so there is nothing confidential to
protect, and that holds for public-service use too. Unencrypted rooms are
therefore the supported configuration indefinitely, not a v1 shortcut.

The channel still checks for the `m.room.encryption` state event when
validating, and fails with a message saying the room is encrypted — not to
signal "encryption pending", but because the alternative is posting ciphertext
nobody can read and reporting success.

**Rooms and HTML.** Accept either a room ID (`!abc:server`) or an alias
(`#bmnews-alerts:server`), resolving an alias once via
`GET /_matrix/client/v3/directory/room/{alias}` and caching it — aliases are
what humans actually have. `formatted_body` is restricted to a suggested tag
subset (`a b i em strong code pre ul ol li blockquote h1`–`h6 p br span del hr
table thead tbody tr th td`) with no CSS and per-client sanitisation that
varies, notably around tables. So the Matrix template stays structurally
simple: headings, lists, links.

The bot's `access_token` sits in `config.toml` next to `smtp_password` — same
precedent, same exposure.

## Surfaces

**CLI:**

```
bmnews notify                      # every enabled watch, max_per_run each
bmnews notify --watch melanoma-trials --count 10
bmnews notify --all                # drain to exhaustion
bmnews notify --dry-run            # match and print, deliver nothing, record nothing
bmnews notify --list               # per watch: delivered / matching / remaining
```

`--dry-run` is what makes a watch tunable: replay criteria against the stored
corpus and see what *would* fire, without waiting for a fetch or sending
anything.

**GUI:** a watches pane listing each watch with `delivered / matching`, plus
"Notify N more" and "Notify all remaining" buttons posting to
`/notify/<watch>`. Delivery runs in a daemon thread behind the existing
`_pipeline_lock`, reporting through the same status-bar fragment as the
pipeline routes — no new async machinery.

## Failure handling

- A failed send records `status = 'failed'` with the error and an incremented
  `attempts`; the paper stays in the derived queue and is retried next run.
- A send that fails **must not** mark papers as sent — the `email_failed`
  distinction `record_digest` already makes, applied per channel.
- Per-channel independence: Matrix succeeding and email failing leaves the
  Matrix rows `sent` and the email rows `failed`, and only the email retries.

## Testing

Following the existing patterns — in-memory SQLite, mocked HTTP, mocked SMTP,
no LLM:

| Area | Coverage |
|---|---|
| `matcher.py` | Every criterion, in isolation, against literal paper dicts. Pure function, no fixtures. |
| Paging | Deliver 5, then 5 more, then confirm exhaustion. Asserts no gaps and no repeats — the property that would break if `LIMIT` moved into the SQL. |
| Dedup | A second run with no new papers delivers nothing. |
| Retry | A failed delivery is retried and flips to `sent`; a successful one is never re-sent. |
| Channels | Matrix against a fake httpx client: endpoint, auth header, body shape, deterministic `txnId`, alias resolution, and the encrypted-room refusal. Email against mocked SMTP. |
| Config round-trip | `save_config` → `load_config` preserves watches and channels intact. This guards the serializer traps above directly, and would fail today against an array-of-tables shape. |
| Migration | Migration 5 applies cleanly on both backends. |

## Out of scope

- **LLM predicate** on a watch — deferred, additive when wanted.
- **ntfy / Slack / Discord** — the `kind` adapter split means each is a small
  addition later; ntfy in particular is a single POST.
- **E2EE Matrix rooms** — a deliberate non-requirement, not deferred work.
  Public preprint notifications carry nothing confidential, including in a
  public-service deployment.
- **Daemon mode** — `run_notify()` is driven by `bmnews run` under cron or
  systemd. A `--daemon` loop over the same core is a thin follow-on.
- **Per-user DM fan-out** — a public service would need the bot to create and
  invite DM rooms per subscriber, which is a different feature.
