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

Which channels a watch resolves to is settled from the parsed config rather
than from the reports that came back, so every one of those notices survives a
render that skips the counts entirely — which is what happens while a
background job holds the database (see :func:`watches_page`). Telling a user
their channel name is wrong is the point of the pane; making that answer wait
on a full scan would be the tail wagging the dog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from flask import Blueprint, Flask, Response, abort, current_app, render_template
from markupsafe import escape

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
            "Notify N more". Per channel, so it stays a paper count.
        channel_names: The configured channels it resolves to, in the order it
            lists them. Settled from the parsed config, so it is known even
            when the counts were not gathered.
        unresolved: The channel names it asks for that match no configured
            channel, and so were dropped with nothing but a log line to say
            so. Rendered beside the table; when *every* name is unresolved
            ``channel_names`` is empty and the whole-watch notice covers it
            instead, so this is only shown for the partial case.
        channels: Its queues, one per resolved channel. Empty when the counts
            were skipped — which is not the same as no channel resolving, and
            ``channel_names`` is what tells the two apart.
        pending: Pending **deliveries** summed across channels — not papers.
            One paper queued for two channels is two deliveries, so this
            over-counts papers by design and is never rendered as a number;
            it exists to answer "is there anything at all left to send", where
            summing is exactly right.
        deliverable: Whether the delivery buttons are offered. False whenever
            pressing one would do nothing: notifications switched off, the
            watch disabled, no channel resolved, or nothing left to send.
    """

    name: str
    enabled: bool
    criteria: str
    max_per_run: int
    channel_names: tuple[str, ...]
    unresolved: tuple[str, ...]
    channels: tuple[ChannelRow, ...]
    pending: int
    deliverable: bool


@watches_bp.route("/watches")
def watches_page() -> str:
    """Render the watches pane.

    The counts are skipped while a background job runs, for the reason
    :func:`rows` returns 204 in the same state: gathering them is a full pass
    over every candidate for every ``(watch, channel)`` pair, and doing it
    against a database a delivery is still writing to buys a number that is
    stale before it reaches the screen. The poller goes out with the page and
    fills the tables in on the first idle answer. Everything the pane says
    about a *configuration* renders either way — that part never needed the
    scan.

    Returns:
        The ``watches_view`` HTMX fragment.
    """
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    busy = jobs.running()
    rows, unreadable = _build_rows(config, with_counts=not busy)
    return render_template(
        "fragments/watches_view.html",
        rows=rows,
        unreadable=unreadable,
        notifications_enabled=config.notifications.enabled,
        # Opening the tab during a run picks up the completion refresh too.
        polling=busy,
    )


@watches_bp.route("/watches/<path:name>/notify", methods=["POST"])
def notify(name: str) -> str:
    """Deliver one batch — the watch's ``max_per_run`` — in the background.

    Args:
        name: The watch's config table name.

    Returns:
        The ``status_bar`` fragment, plus an OOB swap attaching the completion
        poller — or, for a delivery refused before any job started, the status
        bar and a notice saying why.
    """
    return _start_delivery(name, drain=False)


@watches_bp.route("/watches/<path:name>/notify-all", methods=["POST"])
def notify_all(name: str) -> str:
    """Deliver every pending match for a watch, in the background.

    Args:
        name: The watch's config table name.

    Returns:
        The ``status_bar`` fragment, plus an OOB swap attaching the completion
        poller — or, for a delivery refused before any job started, the status
        bar and a notice saying why.
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
        an OOB swap emptying the poller's slot, which stops the polling, and
        one emptying the refusal notice's slot, which clears a stale "did not
        start" message left over from a delivery click refused while the job
        that just finished was running.
    """
    if jobs.running():
        return Response(status=204)

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    watch_rows, unreadable = _build_rows(config, with_counts=True)
    # ``unreadable`` reaches watches_view.html's include for free, but this
    # route renders the fragment on its own: without it a page whose every
    # watch failed to parse would go back to claiming none are configured.
    return (
        render_template("fragments/watch_list.html", rows=watch_rows, unreadable=unreadable)
        + '<div id="watch-poller" hx-swap-oob="innerHTML"></div>'
        + '<div id="watch-message" hx-swap-oob="innerHTML"></div>'
    )


# --- Internals --------------------------------------------------------------


def _build_rows(config: AppConfig, *, with_counts: bool) -> tuple[list[WatchRow], list[str]]:
    """Join the pending counts onto the parsed watches.

    Args:
        config: Application config.
        with_counts: Whether to gather the per-channel counts. False skips the
            scan entirely, leaving every row's ``channels`` empty; everything
            the pane says about a *configuration* is unaffected, because none
            of it is derived from the reports.

    Returns:
        The rows in config order, and the names of watches ``parse_watches``
        could not read — which it logs and skips, so the raw config keys are
        the only place they still exist.
    """
    # Deferred: bmnews.notify pulls in bmlib.quality, which the GUI should not
    # pay for at import time.
    from bmnews.notify.watches import parse_channels, parse_watches, resolve_channels

    configured = config.notifications.watches or {}
    watches = parse_watches(configured)
    unreadable = sorted(set(configured) - set(watches))
    channels = parse_channels(config.notifications.channels or {})

    counts: dict[str, list[DeliveryReport]] = {}
    if with_counts and watches:
        from bmnews.notify.service import pending_counts

        for report in pending_counts(config):
            counts.setdefault(report.watch, []).append(report)

    rows = []
    for watch in watches.values():
        # resolve_channels() logs an unknown channel name and skips it, then
        # returns the rest — so a watch naming one good channel and one typo
        # would render a perfectly healthy-looking table. Asking it rather than
        # re-deriving the lookup keeps the pane's answer and the delivery
        # stage's in lockstep by construction.
        resolved = tuple(channel.name for channel in resolve_channels(watch, channels))
        known = set(resolved)
        unresolved = tuple(name for name in watch.channels if name not in known)
        channel_rows = tuple(
            ChannelRow(
                name=report.channel,
                delivered=report.sent_total,
                matching=report.matching,
                remaining=report.remaining,
            )
            for report in counts.get(watch.name, ())
        )
        pending = sum(row.remaining for row in channel_rows)
        rows.append(
            WatchRow(
                name=watch.name,
                enabled=watch.enabled,
                criteria=_describe(watch),
                max_per_run=watch.max_per_run,
                channel_names=resolved,
                unresolved=unresolved,
                channels=channel_rows,
                pending=pending,
                deliverable=(
                    config.notifications.enabled
                    and watch.enabled
                    and bool(resolved)
                    # Nothing pending, or counts not gathered — either way there
                    # is no number on screen for a button to act on.
                    and pending > 0
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

    watch = parse_watches(config.notifications.watches or {}).get(name)
    if watch is None:
        abort(404)

    # run_notify() returns no reports at all for either of these, which
    # _terminal() cannot tell from "every queue was empty" and would report as
    # a cheerful "nothing to notify" for a delivery that was never attempted.
    # Neither is reachable by clicking — the buttons are gated on the same two
    # conditions — but saying so here costs two lines and means the terminal
    # message never has to be read as "…or the run never happened".
    if not config.notifications.enabled:
        return _no_run(f"Notifications are switched off, so nothing was delivered for {name}.")
    if not watch.enabled:
        return _no_run(f"Watch {name} is disabled, so nothing was delivered for it.")

    def _run() -> None:
        with app.app_context():
            reports = run_notify(config, watch=name, drain=drain, on_progress=jobs.progress)
        # `running` is left alone: jobs.start()'s finally block is the one
        # place that clears it, so it is cleared exactly once whether the
        # target returned, raised, or forgot. Clearing it here as well would
        # widen the window in which running() reads False while the lock is
        # still held from a couple of bytecodes to the whole of this update.
        jobs.status().update(**_terminal(name, reports))

    started = jobs.start(
        message=f"Notifying {name}...",
        target=_run,
        error_label=f"Notification error for {name}",
    )
    # ``started`` is False for a held lock *or* for a thread that could not be
    # spawned at all; the notice names the first, which is the only one
    # reachable short of the process running out of threads — and that one puts
    # its own error in the status bar rendered beside this.
    notice = (
        ""
        if started
        else (
            "A background job is already running — this delivery did not "
            "start. Try again when it finishes."
        )
    )
    # The poller goes out either way: when the *blocking* job ends, the counts
    # still need refreshing.
    return jobs.render_status_bar() + _oob_poller() + _oob_message(notice)


def _terminal(name: str, reports: list[DeliveryReport]) -> dict[str, str]:
    """Turn a run's reports into the status line it ends on.

    A run whose deliveries all failed has done nothing that was asked for, so
    it reports as an error rather than as a quiet success — the distinction
    ``bmnews notify`` already makes on the command line. Failed deliveries stay
    in the derived queue and retry, which is worth saying in the same breath.

    Counted in **notifications**, not papers. One report covers one
    ``(watch, channel)`` pair, so a watch alerting both email and Matrix
    reports the same five papers twice; calling that "10 paper(s)" is simply
    false. One notification is one paper on one channel, which is the grain the
    ``notifications`` table records and the only unit that stays true however
    many channels a watch names.
    """
    delivered = sum(report.delivered for report in reports)
    failed = sum(report.failed for report in reports)

    if failed and not delivered:
        return {
            "message": f"{name}: delivery failed — {failed} notification(s) stay queued",
            "status": "error",
        }
    if failed:
        return {
            "message": (
                f"{name}: {delivered} notification(s) sent, {failed} failed and stay queued"
            ),
            "status": "error",
        }
    if delivered:
        return {"message": f"{name}: {delivered} notification(s) sent", "status": "success"}
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


def _no_run(notice: str) -> str:
    """Report a delivery that was refused before any job was started.

    No poller goes out: nothing is running on this watch's behalf, so there is
    no completion for one to wait on. A job started by something *else* already
    has its own poller from whichever response started it.

    Args:
        notice: What to say in the pane's message slot.

    Returns:
        The unchanged ``status_bar`` fragment plus the OOB notice.
    """
    return jobs.render_status_bar() + _oob_message(notice)


def _oob_message(notice: str) -> str:
    """An OOB swap putting *notice* in the pane's message slot, or clearing it.

    The slot exists because — unlike the pipeline's Fetch & Score button, which
    ``status_bar.html`` stops rendering while a job runs — the delivery buttons
    stay on screen throughout. Without it a refused click returns the *running*
    job's progress line and nothing anywhere says the delivery never happened.

    None of this goes through :func:`jobs.status`: writing it there would
    overwrite the running job's live progress line, which ``jobs.start`` avoids
    on purpose (see its comment).

    Args:
        notice: Plain text to show, or ``""`` to empty the slot. Escaped here
            rather than by a template, because a watch name reaches this from
            user-authored TOML.

    Returns:
        An OOB swap replacing ``#watch-message``'s contents.
    """
    body = f'<p class="watch-notice error">{escape(notice)}</p>' if notice else ""
    return f'<div id="watch-message" hx-swap-oob="innerHTML">{body}</div>'
