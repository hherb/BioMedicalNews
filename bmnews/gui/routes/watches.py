"""The watches pane: what each watch has delivered, and what it still has queued.

Monitoring and delivery only. Creating and editing watches stays in
``config.toml`` — see ``docs/plans/2026-07-29-gui-watches-pane-design.md``.

The rows are built from :func:`~bmnews.notify.watches.parse_watches` with the
counts joined **onto** them, rather than from the counts alone. Two
configurations produce no counts at all and would otherwise render an empty or
misleading pane: a watch naming no configured channel, and a watch that fails
to parse. Both are cases where the user believes they are being alerted and are
not, which is the whole thing this pane exists to make visible.

A disabled watch is not one of them: :func:`~bmnews.notify.service.pending_counts`
includes disabled watches deliberately, so their counts *do* render — what such
a watch does not get is delivery buttons.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from flask import Blueprint, Flask, Response, abort, current_app, render_template

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
        unresolved: The channel names it asks for that match no configured
            channel, and so were dropped with nothing but a log line to say
            so. Rendered beside the table; when *every* name is unresolved
            ``channels`` is empty and the whole-watch notice covers it
            instead, so this is only shown for the partial case.
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
    unresolved: tuple[str, ...]
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


@watches_bp.route("/watches/<path:name>/notify", methods=["POST"])
def notify(name: str) -> str:
    """Deliver one batch — the watch's ``max_per_run`` — in the background.

    Args:
        name: The watch's config table name.

    Returns:
        The ``status_bar`` fragment, plus an OOB swap attaching the completion
        poller.
    """
    return _start_delivery(name, drain=False)


@watches_bp.route("/watches/<path:name>/notify-all", methods=["POST"])
def notify_all(name: str) -> str:
    """Deliver every pending match for a watch, in the background.

    Args:
        name: The watch's config table name.

    Returns:
        The ``status_bar`` fragment, plus an OOB swap attaching the completion
        poller.
    """
    return _start_delivery(name, drain=True)


@watches_bp.route("/watches/rows")
def rows() -> Response | str:
    """Re-render the rows once the running job has finished.

    Returns **204 No Content** while a job is in flight. htmx does not swap on
    a 204, so the poller that asked survives to ask again and no scan is
    performed. That matters: a refresh is a full pass over every candidate for
    every ``(watch, channel)`` pair, which is worth paying once when the counts
    have settled and not every two seconds while they are still moving.

    ``jobs.running()`` is global rather than per-job, so a pipeline run started
    while the poller is alive holds it here until that finishes too. That is
    wanted — the pipeline's own NOTIFY stage delivers, and scoring moves papers
    into and out of the queue.

    Returns:
        204 while a job runs; otherwise the ``watch_list`` fragment followed by
        an OOB swap emptying the poller's slot, which stops the polling.
    """
    if jobs.running():
        return Response(status=204)

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    watch_rows, unreadable = _build_rows(config)
    # ``unreadable`` reaches watches_view.html's include for free, but this
    # route renders the fragment on its own: without it a page whose every
    # watch failed to parse would go back to claiming none are configured.
    return (
        render_template("fragments/watch_list.html", rows=watch_rows, unreadable=unreadable)
        + '<div id="watch-poller" hx-swap-oob="innerHTML"></div>'
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
        # resolve_channels() logs an unknown channel name and skips it, then
        # returns the rest — so a watch naming one good channel and one typo
        # renders a perfectly healthy-looking table. Reports for a watch come
        # only from that function's output, so the resolved names are always a
        # subset of the ones asked for and this diff cannot cry wolf.
        resolved = {channel.name for channel in channels}
        unresolved = tuple(name for name in watch.channels if name not in resolved)
        remaining = sum(channel.remaining for channel in channels)
        rows.append(
            WatchRow(
                name=watch.name,
                enabled=watch.enabled,
                criteria=_describe(watch),
                max_per_run=watch.max_per_run,
                channels=channels,
                unresolved=unresolved,
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


def _start_delivery(name: str, *, drain: bool) -> str:
    """Start one watch's delivery in the background and report it.

    A ``path`` converter carries *name* because a watch is named by its config
    table heading, which may contain a slash; ``string`` would 404 on one and
    the button would look broken for a reason nobody could see.
    """
    # Deferred for the reason given in _build_rows.
    from bmnews.notify.service import run_notify
    from bmnews.notify.watches import parse_watches

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app: Flask = current_app._get_current_object()

    if name not in parse_watches(config.notifications.watches or {}):
        abort(404)

    def _run() -> None:
        with app.app_context():
            reports = run_notify(config, watch=name, drain=drain, on_progress=jobs.progress)
        jobs.status().update(running=False, **_terminal(name, reports))

    started = jobs.start(
        message=f"Notifying {name}...",
        target=_run,
        error_label=f"Notification error for {name}",
    )
    # The poller goes out either way: when the *blocking* job ends, the counts
    # still need refreshing.
    return jobs.render_status_bar() + _oob_poller() + _oob_message(started)


def _terminal(name: str, reports: list[DeliveryReport]) -> dict[str, str]:
    """Turn a run's reports into the status line it ends on.

    A run whose deliveries all failed has done nothing that was asked for, so
    it reports as an error rather than as a quiet success — the distinction
    ``bmnews notify`` already makes on the command line. Failed papers stay in
    the derived queue and retry, which is worth saying in the same breath.
    """
    delivered = sum(report.delivered for report in reports)
    failed = sum(report.failed for report in reports)

    if failed and not delivered:
        return {
            "message": f"{name}: delivery failed — {failed} paper(s) stay queued",
            "status": "error",
        }
    if failed:
        return {
            "message": (f"{name}: {delivered} paper(s) notified, {failed} failed and stay queued"),
            "status": "error",
        }
    if delivered:
        return {"message": f"{name}: {delivered} paper(s) notified", "status": "success"}
    return {"message": f"{name}: nothing to notify", "status": "success"}


def _oob_poller() -> str:
    """An OOB swap putting the completion poller into its slot.

    The poller lives in ``#watch-poller`` rather than inside ``#watch-list``
    so that starting it costs nothing: re-rendering the rows here would mean a
    full scan per (watch, channel) pair at the very moment the delivery job
    starts changing the numbers it would report.
    """
    poller = render_template("fragments/watch_poller.html")
    return f'<div id="watch-poller" hx-swap-oob="innerHTML">{poller}</div>'


def _oob_message(started: bool) -> str:
    """An OOB swap saying whether the click actually started anything.

    ``jobs.start`` refuses while another job holds the lock, and — unlike the
    pipeline's Fetch & Score button, which ``status_bar.html`` stops rendering
    while a job runs — the delivery buttons stay on screen throughout. Without
    this slot the refused click returns the *running* job's progress line and
    nothing anywhere says the delivery never happened.

    The refusal deliberately does not go through :func:`jobs.status`: writing it
    there would overwrite the running job's live progress line, which
    ``jobs.start`` avoids on purpose (see its comment).

    Args:
        started: What ``jobs.start`` returned. That is False for a held lock
            *or* for a thread that could not be spawned at all; the notice
            names the first, which is the only one reachable short of the
            process running out of threads — and that one puts its own error
            in the status bar rendered beside this.

    Returns:
        An OOB swap clearing ``#watch-message`` when the job started, or
        filling it with the refusal notice when it did not.
    """
    notice = (
        ""
        if started
        else (
            '<p class="watch-notice error">A background job is already running — this '
            "delivery did not start. Try again when it finishes.</p>"
        )
    )
    return f'<div id="watch-message" hx-swap-oob="innerHTML">{notice}</div>'
