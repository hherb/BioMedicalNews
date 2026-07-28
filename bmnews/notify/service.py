"""The notification stage: select, page, dispatch, record.

Sits between scoring and the digest as a fourth stage, and is **query-based
rather than callback-based**. `run_score()` does expose a per-paper hook, but a
stage that queries for scored-but-unnotified papers survives a crash mid-run,
tests without running the scorer at all, and is structurally the same shape as
``get_papers_for_digest()``.

The pending queue is *derived*, never stored: "papers this watch matches now,
minus those already sent over this channel". That is what makes paging
idempotent — asking for five more runs the identical selection, and the five
just delivered have dropped out of it — and it means editing a watch's criteria
cannot leave orphaned queue rows behind, because there are no queue rows.

Selection is split in two, and the split is the thing to be careful about. SQL
narrows on what is indexable and imposes the ordering; :mod:`bmnews.notify.matcher`
applies the rest in Python. So the delivery cap cannot live in the SQL: limiting
to five rows before the matcher rejects three of them would deliver two while
further matches sat unread, and the paging above would appear to run dry early.
:func:`collect_matches` therefore scans in chunks and filters as it goes, and
the cap is applied to what it returns.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from typing import Any

from bmnews.config import AppConfig
from bmnews.constants import NOTIFY_SCAN_CHUNK
from bmnews.db.operations import (
    count_notifications,
    get_notification_candidates,
    record_notifications,
)
from bmnews.db.schema import init_db, open_db
from bmnews.notify.channels import ChannelError, Message, build_adapter, transaction_key
from bmnews.notify.matcher import matches
from bmnews.notify.renderer import notification_subject, render_notification
from bmnews.notify.watches import Channel, Watch, parse_channels, parse_watches, resolve_channels
from bmnews.scoring.scorer import tiers_below
from bmnews.templating import build_template_engine

logger = logging.getLogger(__name__)

#: Which template pair a channel kind renders with. Email gets the styled pair;
#: everything else that is not Matrix would need its own entry here.
_MEDIUM_FOR_KIND = {"email": "email", "matrix": "matrix"}


@dataclass(frozen=True)
class DeliveryReport:
    """What one run did for one ``(watch, channel)`` pair.

    Reported per channel rather than per watch because that is the grain the
    queue works at: one watch delivering to both email and Matrix has two
    independent queues, and one of them failing says nothing about the other.

    Attributes:
        watch: The watch's name.
        channel: The channel's name.
        enabled: Whether the watch is enabled. A disabled watch is never
            delivered but is still counted, so ``notify --list`` can show what
            turning it on would send.
        dry_run: Whether this report describes a rehearsal. Nothing was sent
            and nothing recorded, so ``delivered`` says what *would* have gone
            and ``sent_total`` has not moved.
        delivered: Papers recorded ``sent`` **by this run** — or, under
            ``dry_run``, the batch that would have been. Zero from
            :func:`pending_counts`, which sends nothing.
        failed: Papers recorded ``failed`` by this run.
        sent_total: Papers this watch has ever had delivered over this channel,
            this run's included. Kept separate from ``delivered`` because
            "5 went out just now" and "5 have gone out in total" are different
            answers and a single field would silently give the wrong one.
        matching: Papers the watch matches in total, delivered ones included.
        remaining: Papers still pending after this run. A failed delivery is
            counted here — it stays in the queue and retries.
        exhausted: Whether the queue is now empty.
    """

    watch: str
    channel: str
    enabled: bool = True
    dry_run: bool = False
    delivered: int = 0
    failed: int = 0
    sent_total: int = 0
    matching: int = 0
    remaining: int = 0
    exhausted: bool = True


def collect_matches(conn: Any, watch: Watch, channel_name: str) -> list[dict]:
    """Walk the derived queue, collecting every paper *watch* actually matches.

    Chunked, and the chunk is the thing to be careful about: it is a window on
    the SQL-*narrowed* set, not on the matching set, because the matcher rejects
    rows afterwards in Python. So :data:`NOTIFY_SCAN_CHUNK` is a scan window and
    never a delivery cap — capping here would return five rows, have the matcher
    reject three, and deliver two while further matches sat unread. The loop
    tops up from the next offset until a chunk comes back short, which is the
    only thing that means the narrowed set is spent.

    The scan runs to the end rather than stopping at a batch's worth, because
    the caller's ``remaining`` count has to be exact for paging to be worth
    trusting, and this is the scan that can answer it. That is affordable
    because the columns are narrow (no cached full text) and the score floors
    have already done the heavy narrowing in SQL.

    Args:
        conn: DB-API connection.
        watch: The watch whose criteria are applied.
        channel_name: The channel whose already-sent papers are excluded.

    Returns:
        Every pending match, best combined score first.
    """
    collected: list[dict] = []
    scanned = 0

    while True:
        chunk = get_notification_candidates(
            conn,
            watch=watch.name,
            channel=channel_name,
            min_relevance=watch.min_relevance,
            min_combined=watch.min_combined,
            exclude_tiers=tiers_below(watch.min_quality_tier),
            limit=NOTIFY_SCAN_CHUNK,
            offset=scanned,
        )
        scanned += len(chunk)
        collected.extend(paper for paper in chunk if matches(paper, watch))

        if len(chunk) < NOTIFY_SCAN_CHUNK:
            return collected


def run_notify(
    config: AppConfig,
    *,
    watch: str = "",
    count: int | None = None,
    drain: bool = False,
    dry_run: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> list[DeliveryReport]:
    """Deliver pending notifications for every enabled watch.

    Args:
        config: Application config.
        watch: Restrict to one watch by name, or ``""`` for all of them.
        count: Deliver this many per watch, overriding each watch's
            ``max_per_run``.
        drain: Deliver every pending match rather than one batch.
        dry_run: Select and report, but send nothing and record nothing — which
            is what makes a watch tunable against the stored corpus without
            waiting for a fetch.
        on_progress: Optional callback receiving a status message.

    Returns:
        One report per ``(watch, channel)`` pair that was evaluated.
    """
    if not config.notifications.enabled:
        logger.debug("Notifications are disabled — nothing to do")
        return []

    watches = _selected_watches(config, watch, enabled_only=True)
    if not watches:
        return []

    channels = parse_channels(config.notifications.channels)
    templates = build_template_engine(config)
    reports: list[DeliveryReport] = []

    with closing(open_db(config)) as conn:
        init_db(conn)
        for each in watches:
            for channel in resolve_channels(each, channels):
                reports.append(
                    _deliver(
                        conn,
                        watch=each,
                        channel=channel,
                        config=config,
                        templates=templates,
                        wanted=None if drain else (each.max_per_run if count is None else count),
                        dry_run=dry_run,
                        on_progress=on_progress,
                    )
                )

    return reports


def pending_counts(config: AppConfig, *, watch: str = "") -> list[DeliveryReport]:
    """Report what each watch has delivered and what it still has queued.

    Sends nothing and records nothing. Disabled watches are included: knowing
    what one *would* send is the point of being able to look.

    Args:
        config: Application config.
        watch: Restrict to one watch by name, or ``""`` for all of them.

    Returns:
        One report per ``(watch, channel)`` pair, with ``delivered``,
        ``matching`` and ``remaining`` filled in and no delivery counts.
    """
    watches = _selected_watches(config, watch, enabled_only=False)
    if not watches:
        return []

    channels = parse_channels(config.notifications.channels)
    reports: list[DeliveryReport] = []

    with closing(open_db(config)) as conn:
        init_db(conn)
        for each in watches:
            for channel in resolve_channels(each, channels):
                pending = collect_matches(conn, each, channel.name)
                sent_total = count_notifications(conn, watch=each.name, channel=channel.name)
                reports.append(
                    DeliveryReport(
                        watch=each.name,
                        channel=channel.name,
                        enabled=each.enabled,
                        sent_total=sent_total,
                        matching=sent_total + len(pending),
                        remaining=len(pending),
                        exhausted=not pending,
                    )
                )

    return reports


# --- Internals --------------------------------------------------------------


def _selected_watches(config: AppConfig, name: str, *, enabled_only: bool) -> list[Watch]:
    """Parse the configured watches and narrow them to the ones to act on."""
    watches = parse_watches(config.notifications.watches)

    if name:
        chosen = watches.get(name)
        if chosen is None:
            logger.error("No watch named %r is configured", name)
            return []
        selected = [chosen]
    else:
        selected = list(watches.values())

    if enabled_only:
        selected = [watch for watch in selected if watch.enabled]
    return selected


def _deliver(
    conn: Any,
    *,
    watch: Watch,
    channel: Channel,
    config: AppConfig,
    templates: Any,
    wanted: int | None,
    dry_run: bool,
    on_progress: Callable[[str], None] | None,
) -> DeliveryReport:
    """Select, send and record one batch for one watch on one channel."""
    # The whole pending queue, not just this batch: `remaining` has to be exact
    # for paging to be trustworthy, and the scan that answers it is the same
    # one that assembles the batch. The alternative is approximating a number
    # whose entire job is to promise nothing was dropped.
    pending = collect_matches(conn, watch, channel.name)
    batch = pending if wanted is None else pending[:wanted]
    remaining_after = len(pending) - len(batch)
    sent_before = count_notifications(conn, watch=watch.name, channel=channel.name)

    if not batch:
        logger.debug("Watch %r has nothing pending on channel %r", watch.name, channel.name)
        return DeliveryReport(
            watch=watch.name,
            channel=channel.name,
            sent_total=sent_before,
            matching=sent_before,
            remaining=0,
            exhausted=True,
        )

    paper_ids = [paper["id"] for paper in batch]
    report = DeliveryReport(
        watch=watch.name,
        channel=channel.name,
        delivered=len(batch),
        sent_total=sent_before + len(batch),
        matching=sent_before + len(pending),
        remaining=remaining_after,
        exhausted=remaining_after == 0,
    )

    if dry_run:
        _report_progress(
            on_progress, f"[dry run] {watch.name} → {channel.name}: {len(batch)} paper(s)"
        )
        # `delivered` stays the batch size — that is the question a dry run is
        # asking — but nothing was recorded, so the running total must not move.
        return replace(report, dry_run=True, sent_total=sent_before)

    try:
        _send(
            batch,
            watch=watch,
            channel=channel,
            config=config,
            templates=templates,
            remaining=remaining_after,
        )
    except ChannelError as exc:
        logger.error("Watch %r could not deliver over %r: %s", watch.name, channel.name, exc)
        record_notifications(
            conn,
            watch=watch.name,
            paper_ids=paper_ids,
            channel=channel.name,
            status="failed",
            error=str(exc),
        )
        _report_progress(on_progress, f"{watch.name} → {channel.name}: delivery failed")
        # A failed send must not mark anything sent: these papers stay in the
        # derived queue, which is what makes the next run a retry.
        return DeliveryReport(
            watch=watch.name,
            channel=channel.name,
            failed=len(batch),
            sent_total=sent_before,
            matching=report.matching,
            remaining=len(pending),
            exhausted=False,
        )

    record_notifications(
        conn, watch=watch.name, paper_ids=paper_ids, channel=channel.name, status="sent"
    )
    _report_progress(on_progress, f"{watch.name} → {channel.name}: {len(batch)} paper(s) notified")
    return report


def _send(
    batch: list[dict],
    *,
    watch: Watch,
    channel: Channel,
    config: AppConfig,
    templates: Any,
    remaining: int,
) -> None:
    """Render the batch for this channel's medium and hand it to the adapter.

    *remaining* is how many matches this batch leaves behind, and it goes into
    the message body rather than only into the run's report: a capped batch
    that says nothing looks like the whole answer, when the point of paging is
    that the rest is still there to be pulled.
    """
    # build_adapter has already refused any kind not in this map, so a missing
    # entry here would be a new kind whose templates nobody wrote — worth
    # saying rather than quietly rendering it as email.
    adapter = build_adapter(channel, config)
    medium = _MEDIUM_FOR_KIND.get(channel.kind)
    if medium is None:
        raise ChannelError(f"channel kind {channel.kind!r} has no notification templates")

    message = Message(
        subject=notification_subject(watch.name, len(batch)),
        html=render_notification(
            batch,
            watch_name=watch.name,
            templates=templates,
            medium=medium,
            fmt="html",
            remaining=remaining,
        ),
        text=render_notification(
            batch,
            watch_name=watch.name,
            templates=templates,
            medium=medium,
            fmt="text",
            remaining=remaining,
        ),
    )

    adapter.send(
        message, txn_key=transaction_key(watch.name, channel.name, [p["id"] for p in batch])
    )


def _report_progress(on_progress: Callable[[str], None] | None, message: str) -> None:
    """Forward a status line to the caller's progress hook, if any."""
    logger.info(message)
    if on_progress:
        on_progress(message)
