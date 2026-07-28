# Notification Service — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Status: implemented.** All five tasks are done and on `main` via the
`feat/notification-service` branch; the GUI watches pane remains out of scope,
as stated below. Where the implementation diverged from this plan, the reasons
are recorded in "Deviations" at the bottom — the task bodies are left as
written, as the record of what was planned.

**Goal:** Deliver the watch-based notification stage designed in
`2026-07-26-notification-service-design.md` — selection, paging, email and
Matrix delivery, recording, the `bmnews notify` CLI, and pipeline wiring.

**Architecture:** A fourth pipeline stage, `SYNC → SCORE → NOTIFY → DIGEST`.
Selection is query-based, not callback-based: `run_notify()` derives the
pending queue on each run as "papers this watch matches now, minus those with a
`sent` row for that channel". SQL does the indexable narrowing and the
ordering; `notify/matcher.py` applies the rest in Python. Delivery goes through
one adapter per channel *kind*, and every attempt is recorded per channel.

**Tech Stack:** Python 3.11+, SQLite/PostgreSQL via `bmlib.db`, `httpx`,
`smtplib`, Jinja2 via `bmlib.templates.TemplateEngine`, Click, pytest.

## Global Constraints

- **`notifications` must never be conflated with `digest_papers`.**
  `get_papers_for_digest()` excludes papers in `digest_papers` and nothing
  else, so recording a notification there would silently suppress that paper's
  digest entry. A notified paper stays digest-eligible.
- **No bare `LIMIT N` as the whole selection.** Python filtering runs *after*
  SQL narrowing, so limiting first under-delivers silently. Use the top-up loop
  in Task 3, and keep "N matches collected" distinct from "a chunk came back
  short" — only the second means exhaustion.
- **The pending queue is derived, never stored.** No queue table, no
  reconciliation pass when a watch is edited.
- **A failed send must not mark papers as sent** — `status = 'failed'` keeps
  the paper in the derived queue so the next run retries it.
- Both database backends: SQL goes in `db/operations.py` with `placeholder()`
  and `is_sqlite()`, and the new tests run under `tests/test_db.py`'s
  `db_backend` fixture.
- Coding conventions from CLAUDE.md: `from __future__ import annotations`,
  keyword-only args for writes, Google-style docstrings, module-level loggers,
  no magic numbers (fixed values → `bmnews/constants.py`), ruff line-length 100.
- **Out of scope for this plan:** the GUI watches pane, and the LLM predicate.
  Both are additive and neither is depended on by anything here.

---

### Task 1: Notification storage and selection SQL

**Files:**
- Modify: `bmnews/db/operations.py` (new `# --- Notifications ---` section)
- Modify: `bmnews/constants.py` (add `NOTIFY_SCAN_CHUNK`)
- Test: `tests/test_db.py` (new `TestNotifications` class, runs on both backends)

**Interfaces:**
- Consumes: the existing `_PAPER_COLUMNS` / `_SCORE_COLUMNS` / `_PAPER_FROM`
  fragments and `_row_to_paper()`.
- Produces:
  ```python
  def get_notification_candidates(
      conn, *, watch: str, channel: str, min_relevance: float = 0.0,
      min_combined: float = 0.0, exclude_tiers: Sequence[str] = (),
      limit: int, offset: int = 0,
  ) -> list[dict]        # paper dicts + score columns + a "tags" list, best combined first

  def record_notification(
      conn, *, watch: str, paper_id: int, channel: str,
      status: str, error: str = "",
  ) -> None              # upsert on (watch, paper_id, channel); attempts += 1 on conflict

  def count_notifications(conn, *, watch: str, channel: str = "", status: str = "sent") -> int
  ```

- [x] **Step 1: Write the failing tests** in `tests/test_db.py`:
  `test_candidates_exclude_already_sent`, `test_candidates_include_failed`
  (a `failed` row stays in the queue so it retries),
  `test_candidates_respect_score_floors`, `test_candidates_exclude_tiers`,
  `test_candidates_order_by_combined_desc`, `test_candidates_carry_tags`,
  `test_candidates_paginate_by_offset`, `test_record_notification_upserts`
  (a second call for the same `(watch, paper_id, channel)` increments
  `attempts` and overwrites `status`/`error` rather than raising),
  `test_record_notification_is_per_channel` (same watch and paper, two
  channels, two rows), `test_count_notifications_filters_by_status`.
- [x] **Step 2: Run them and watch them fail** —
  `uv run pytest tests/test_db.py -k Notifications -v`, expect
  `ImportError`/`AttributeError`.
- [x] **Step 3: Implement.** The anti-join is
  `LEFT JOIN notifications n ON n.paper_id = p.id AND n.watch = ? AND n.channel = ? AND n.status = 'sent'`
  with `n.paper_id IS NULL`. Tags are attached with one follow-up
  `SELECT paper_id, tag FROM paper_tags WHERE paper_id IN (...)` per chunk
  rather than SQL aggregation, which spells differently on each backend.
  The upsert is `ON CONFLICT(watch, paper_id, channel) DO UPDATE SET
  status = excluded.status, error = excluded.error,
  attempts = notifications.attempts + 1` (`EXCLUDED` upper-case on PostgreSQL,
  as `_upsert_extras` already does).
- [x] **Step 4: Run both backends** — `uv run pytest tests/test_db.py -v` and
  `BMNEWS_TEST_PG_DSN=... uv run pytest tests/test_db.py -v`.
- [x] **Step 5: Commit** — `feat(db): notification recording and candidate selection`.

---

### Task 2: Channel adapters

**Files:**
- Create: `bmnews/notify/channels/__init__.py`, `channels/email.py`, `channels/matrix.py`
- Create: `templates/notify_email.html`, `notify_email.txt`, `notify_matrix.html`, `notify_matrix.txt`
- Create: `bmnews/notify/renderer.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: `Channel` from `notify/watches.py`; `send_email` from
  `digest/sender.py`; `AppConfig`.
- Produces:
  ```python
  class ChannelError(RuntimeError): ...

  @dataclass(frozen=True)
  class Message:
      subject: str
      html: str
      text: str

  class ChannelAdapter(Protocol):
      def send(self, message: Message, *, txn_key: str) -> None: ...   # raises ChannelError

  def build_adapter(channel: Channel, config: AppConfig) -> ChannelAdapter  # raises ChannelError

  # renderer.py
  def render_notification(papers, watch, templates, *, fmt: str, medium: str) -> str
  ```

- [x] **Step 1: Write the failing tests.** Email: `send_email` is mocked, and a
  `False` return raises `ChannelError` (delivery failure must not look like
  success); the recipient falls back to `[email].to_address` then
  `[user].email`. Matrix, against a fake httpx client:
  `test_matrix_puts_to_send_endpoint` (path, `Authorization: Bearer`, body
  shape with `msgtype`/`format`/`formatted_body`),
  `test_matrix_txn_id_is_deterministic` (same watch, channel and paper ids →
  same `txnId`; a different paper set → a different one),
  `test_matrix_resolves_alias_once` (a `#alias:server` is resolved via
  `/directory/room/` and the result reused),
  `test_matrix_refuses_encrypted_room` (an `m.room.encryption` state event
  raises `ChannelError` rather than posting ciphertext nobody can read),
  `test_matrix_raises_on_http_error`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement.** `txn_key` is derived by the caller as a stable
  digest of `(watch, channel, sorted paper_ids)` — the homeserver treats a
  repeat PUT with the same `txnId` as a retransmission, which closes the
  "message sent, row not yet written" crash window. Room state check is
  `GET /_matrix/client/v3/rooms/{id}/state/m.room.encryption`; a 404 means
  unencrypted, which is the supported configuration.
- [x] **Step 4: Templates.** `notify_email.*` may be CSS-styled like the digest;
  `notify_matrix.html` must stay to headings/lists/links — Matrix's HTML subset
  has no CSS at all, and `formatted_body` needs its plain-text `body` twin.
- [x] **Step 5: Run** `uv run pytest tests/test_notify.py -v`.
- [x] **Step 6: Commit** — `feat(notify): email and Matrix channel adapters`.

---

### Task 3: `run_notify()` — select, page, dispatch, record

**Files:**
- Create: `bmnews/notify/service.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: Task 1's three DB functions, Task 2's `build_adapter` /
  `render_notification`, `parse_watches` / `parse_channels` /
  `resolve_channels`, `matches()`.
- Produces:
  ```python
  @dataclass
  class WatchReport:
      watch: str
      delivered: int
      failed: int
      remaining: int
      exhausted: bool

  def run_notify(
      config, *, watch: str = "", count: int | None = None,
      drain: bool = False, dry_run: bool = False,
      on_progress: Callable[[str], None] | None = None,
  ) -> list[WatchReport]

  def pending_counts(config) -> list[WatchReport]   # for `notify --list`; sends nothing
  ```

- [x] **Step 1: Write the failing tests.** The paging property is the one that
  matters: deliver 5, then 5 more, then confirm exhaustion, asserting **no gaps
  and no repeats** — this is what breaks if `LIMIT` moves into the SQL. Then:
  a second run with no new papers delivers nothing; a `failed` delivery is
  retried and flips to `sent`; a `sent` one is never re-sent; Matrix succeeding
  while email fails leaves per-channel rows and retries only the email;
  `dry_run` records nothing and sends nothing; a disabled watch is skipped by
  `run_notify` but still counted by `pending_counts`.
  Include the boundary case directly: `NOTIFY_SCAN_CHUNK` matches filled
  exactly at a chunk boundary must **not** report exhaustion.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement the top-up loop:**
  ```
  collected, offset = [], 0
  while len(collected) < wanted:
      chunk = get_notification_candidates(..., limit=NOTIFY_SCAN_CHUNK, offset=offset)
      offset += len(chunk)
      collected += [p for p in chunk if matches(p, watch)]
      if len(chunk) < NOTIFY_SCAN_CHUNK:
          exhausted = True      # the narrowed set is spent — not the same as a full batch
          break
  ```
  Note the offset walks *scanned* rows, not matched ones, and the anti-join
  shifts under it as rows are recorded — so selection happens before dispatch
  for a batch, not interleaved with it.
- [x] **Step 4: Run** `uv run pytest tests/test_notify.py -v`.
- [x] **Step 5: Commit** — `feat(notify): run_notify with derived queue and paging`.

---

### Task 4: CLI command and pipeline wiring

**Files:**
- Modify: `bmnews/cli.py` (new `notify` command)
- Modify: `bmnews/pipeline.py` (`run_pipeline`, after `run_score`)
- Test: `tests/test_notify.py` (Click's `CliRunner`), `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `run_notify`, `pending_counts`.
- Produces: `bmnews notify [--watch NAME] [--count N] [--all] [--dry-run] [--list]`.

- [x] **Step 1: Write the failing tests.** `--list` prints per-watch
  delivered/matching/remaining and sends nothing; `--dry-run` prints matches
  and records nothing; `--all` drains; `--watch` restricts. For the pipeline:
  **notify is not gated on `scored > 0`** — unlike the digest — because a run
  with nothing newly scored still has a failed delivery to retry and a
  just-loosened watch to honour. Assert that directly with a mocked
  `run_notify` and `run_score` returning 0.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement**, mirroring the existing commands' shape
  (`ctx.obj["config"]`, `-c/--config`, logging).
- [x] **Step 4: Run the full suite** — `uv run pytest tests/ -v`, plus
  `uv run ruff check bmnews/ tests/` and `uv run ruff format --check`.
- [x] **Step 5: Commit** — `feat(cli): bmnews notify, wired into run_pipeline`.

---

### Task 5: Config round-trip and documentation

**Files:**
- Modify: `bmnews/config.py` (`DEFAULT_CONFIG_TOML` example, if incomplete)
- Modify: `CLAUDE.md`, `HANDOVER.md`, `docs/user/`, `docs/dev/`
- Test: `tests/test_config.py`

- [x] **Step 1: Write the failing test** — `save_config` → `load_config`
  preserves watches and channels intact. This guards the serializer traps
  directly: an array-of-tables shape would fail it today, and so would any
  four-level nesting.
- [x] **Step 2: Run, implement, re-run.**
- [x] **Step 3: Update the docs** — CLAUDE.md's Notifications section still
  says the service, channels, CLI and templates are unimplemented; the pipeline
  diagram gains `NOTIFY`; user docs gain the `bmnews notify` commands and a
  worked config example.
- [x] **Step 4: Commit** — `docs: notification service usage and status`.

---

## Deviations from this plan

Four, all decided while implementing and none changing what the stage does.

**1. `_deliver()` scans the whole pending set rather than early-exiting at the
batch.** The plan's top-up loop stops as soon as *N* matches are collected,
which is the cheaper thing to do — but the run then still owes an exact
`remaining` count, and computing that means the very scan the early exit just
skipped. Since `remaining`'s entire job is to promise nothing was dropped,
approximating it is worse than paying for it. `collect_matches()` keeps the
early exit (`wanted` is honoured) and `_deliver()` simply does not pass one.
The distinction the plan cared about — "enough collected" versus "a chunk came
back short" — still lives in the loop, and
`test_a_full_batch_at_a_chunk_boundary_is_not_exhaustion` pins it.

**2. Tests are split three ways, not folded into `tests/test_notify.py`.**
That file's whole premise is that it touches no database, no SMTP and no HTTP;
adding a fake homeserver to it would have cost that property. So
`test_notify_channels.py` covers the adapters and templates and
`test_notify_service.py` covers `run_notify` and the CLI.
`test_notify_service.py` uses a **file-backed** SQLite database rather than an
in-memory one, because each run opens and closes its own connection and an
in-memory database dies with the connection that made it — which is also a
more faithful exercise of paging across runs.

**3. `DeliveryReport` reports per `(watch, channel)`, and grew a `sent_total`
field.** Per channel because that is the grain the queue works at. The extra
field because "5 went out just now" and "5 have gone out in total" are
different answers, and `notify --list` wants the second while a run report
wants the first — one field would have silently given the wrong one.

**4. `build_template_engine` and `TEMPLATES_DIR` moved to
`bmnews/templating.py`.** Task 3 would otherwise have had the notification
stage import `bmnews.pipeline`, which imports it back. The GUI's template
editor was already importing the orchestrator just to learn a directory path,
so a neutral home fixed both.

Task 5's config round-trip test turned out to exist already
(`tests/test_config.py::test_a_watch_name_survives_the_round_trip_whatever_it_is`
and its neighbours), so that step was verification rather than new work.
