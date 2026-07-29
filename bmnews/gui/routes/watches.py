"""The watches pane: what each watch has delivered, and what it still has queued.

Monitoring and delivery only. Creating and editing watches stays in
``config.toml`` — see ``docs/plans/2026-07-29-gui-watches-pane-design.md``.

The rows are built from :func:`~bmnews.notify.watches.parse_watches` with the
counts joined **onto** them, rather than from the counts alone. Three
configurations produce no counts at all and would otherwise render an empty or
misleading pane: a watch naming no configured channel, a watch that fails to
parse, and a watch that is switched off. Each of those is a case where the user
believes they are being alerted and are not, which is the whole thing this pane
exists to make visible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from flask import Blueprint, current_app, render_template

from bmnews.config import AppConfig
from bmnews.gui import jobs

if TYPE_CHECKING:
    # Imported for annotations only: bmnews.notify pulls in bmlib.quality, and
    # the routes below defer that cost to the request that needs it.
    from bmnews.notify.service import DeliveryReport
    from bmnews.notify.watches import Watch

watches_bp = Blueprint("watches", __name__)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelRow:
    """One watch's queue on one channel.

    Attributes:
        name: The channel's config table name.
        delivered: Papers ever sent for this pair. Failed attempts are not
            counted here — they stay queued and are counted in ``remaining``.
        matching: Papers the watch matches in total, delivered ones included.
        remaining: Papers still pending.
    """

    name: str
    delivered: int
    matching: int
    remaining: int


@dataclass(frozen=True)
class WatchRow:
    """One watch as the pane renders it.

    Attributes:
        name: The watch's config table name.
        enabled: Whether the watch is evaluated at all.
        criteria: One-line summary of what it matches.
        max_per_run: How many papers one batch delivers — the ``N`` in
            "Notify N more".
        channels: Its queues, one per resolved channel. Empty when none of its
            channel names matches a configured channel.
        remaining: Pending papers summed across channels.
        deliverable: Whether the delivery buttons are offered. False whenever
            pressing one would do nothing: notifications switched off, the
            watch disabled, no channel resolved, or nothing left to send.
    """

    name: str
    enabled: bool
    criteria: str
    max_per_run: int
    channels: tuple[ChannelRow, ...]
    remaining: int
    deliverable: bool


@watches_bp.route("/watches")
def watches_page() -> str:
    """Render the watches pane.

    Returns:
        The ``watches_view`` HTMX fragment.
    """
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    rows, unreadable = _build_rows(config)
    return render_template(
        "fragments/watches_view.html",
        rows=rows,
        unreadable=unreadable,
        notifications_enabled=config.notifications.enabled,
        # Opening the tab during a run picks up the completion refresh too.
        polling=jobs.running(),
    )


# --- Internals --------------------------------------------------------------


def _build_rows(config: AppConfig) -> tuple[list[WatchRow], list[str]]:
    """Join the pending counts onto the parsed watches.

    Args:
        config: Application config.

    Returns:
        The rows in config order, and the names of watches ``parse_watches``
        could not read — which it logs and skips, so the raw config keys are
        the only place they still exist.
    """
    # Deferred: bmnews.notify pulls in bmlib.quality, which the GUI should not
    # pay for at import time.
    from bmnews.notify.service import pending_counts
    from bmnews.notify.watches import parse_watches

    configured = config.notifications.watches or {}
    watches = parse_watches(configured)
    unreadable = sorted(set(configured) - set(watches))

    counts: dict[str, list[DeliveryReport]] = {}
    if watches:
        for report in pending_counts(config):
            counts.setdefault(report.watch, []).append(report)

    rows = []
    for watch in watches.values():
        channels = tuple(
            ChannelRow(
                name=report.channel,
                delivered=report.sent_total,
                matching=report.matching,
                remaining=report.remaining,
            )
            for report in counts.get(watch.name, ())
        )
        remaining = sum(channel.remaining for channel in channels)
        rows.append(
            WatchRow(
                name=watch.name,
                enabled=watch.enabled,
                criteria=_describe(watch),
                max_per_run=watch.max_per_run,
                channels=channels,
                remaining=remaining,
                deliverable=(
                    config.notifications.enabled
                    and watch.enabled
                    and bool(channels)
                    and remaining > 0
                ),
            )
        )
    return rows, unreadable


def _describe(watch: Watch) -> str:
    """Summarise a watch's criteria in one line, in the config's vocabulary.

    A watch constraining nothing is a real configuration — it matches every
    scored paper — so it says so rather than rendering an empty line that
    reads like a display bug.
    """
    parts: list[str] = []
    if watch.min_relevance:
        parts.append(f"relevance ≥ {watch.min_relevance:g}")
    if watch.min_combined:
        parts.append(f"combined ≥ {watch.min_combined:g}")
    if watch.min_quality_tier:
        parts.append(f"tier ≥ {watch.min_quality_tier}")
    for label, values in (
        ("tags", watch.tags),
        ("keywords", watch.keywords),
        ("sources", watch.sources),
        ("journals", watch.journals),
        ("designs", watch.study_designs),
    ):
        if values:
            parts.append(f"{label}: {', '.join(values)}")
    return " · ".join(parts) or "no criteria — matches every scored paper"
