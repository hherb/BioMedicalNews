"""Pipeline execution routes."""

from __future__ import annotations

import logging
from collections import deque

from flask import Blueprint, Flask, current_app, render_template

from bmnews.config import AppConfig
from bmnews.constants import DEFAULT_PAGE_SIZE
from bmnews.db.operations import count_unscored_papers, get_paper_with_score
from bmnews.gui import jobs

pipeline_bp = Blueprint("pipeline", __name__)
logger = logging.getLogger(__name__)

# Paper IDs scored since last status poll, consumed on each poll. deque's
# append/popleft are atomic, so no extra locking is needed here.
_scored_paper_ids: deque[int] = deque()


@pipeline_bp.route("/pipeline/run", methods=["POST"])
def run() -> str:
    """Start a full fetch → store → score → digest run in the background.

    Returns:
        The ``status_bar`` HTMX fragment — of the run just started, or of
        whatever job was already in flight.
    """
    from bmnews.pipeline import run_pipeline

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app: Flask = current_app._get_current_object()

    def _run() -> None:
        """Run the pipeline, then publish a terminal status."""

        def _progress_with_refresh(message: str) -> None:
            """Record progress and flag the list for reload once scoring starts."""
            jobs.progress(message)
            # After storing completes, the paper list needs a full reload
            if "Scoring" in message and not jobs.status().get("refresh_list"):
                jobs.status()["refresh_list"] = True

        with app.app_context():
            run_pipeline(
                config,
                on_progress=_progress_with_refresh,
                on_scored=_scored_paper_ids.append,
            )
        # `running` is left alone: jobs.start()'s finally block is the one
        # place that clears it, so it is cleared exactly once whether the
        # target returned, raised, or forgot. Clearing it here as well would
        # widen the window in which running() reads False while the lock is
        # still held from a couple of bytecodes to the whole of this update.
        jobs.status().update(
            message="Pipeline complete — papers fetched, scored, and digested.",
            status="success",
            refresh_list=True,
        )

    jobs.start(message="Starting pipeline...", target=_run, error_label="Pipeline error")
    return jobs.render_status_bar()


@pipeline_bp.route("/pipeline/resume", methods=["POST"])
def resume() -> str:
    """Resume scoring papers left unscored by a previous session.

    Called on app startup. Does nothing when everything is already scored or
    a run is in flight.

    Returns:
        The ``status_bar`` HTMX fragment.
    """
    from bmnews.pipeline import run_score

    conn = current_app.config["BMNEWS_DB"]
    count = count_unscored_papers(conn)

    if count == 0 or jobs.running():
        return jobs.render_status_bar()

    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    app: Flask = current_app._get_current_object()

    def _run() -> None:
        """Score the outstanding papers, then publish a terminal status."""
        with app.app_context():
            scored = run_score(
                config,
                on_progress=jobs.progress,
                on_scored=_scored_paper_ids.append,
            )
        # See the note in run(): jobs.start() clears `running` for us.
        jobs.status().update(
            message=f"Resumed scoring complete — {scored} papers scored.",
            status="success",
        )

    jobs.start(
        message=f"Resuming scoring of {count} papers...",
        target=_run,
        error_label="Scoring error",
    )
    return jobs.render_status_bar()


@pipeline_bp.route("/pipeline/status")
def status() -> str:
    """Report pipeline progress, with out-of-band updates for scored papers.

    Polled by the status bar. Each poll drains the queue of newly scored paper
    ids and emits an OOB swap for each affected card, or a single full-list
    refresh when the pipeline signalled that new papers were stored.

    Returns:
        The ``status_bar`` fragment, optionally followed by OOB swap markup.
    """
    conn = current_app.config["BMNEWS_DB"]

    # If the pipeline stored new papers, reload the full paper list via OOB
    needs_list_refresh = jobs.status().get("refresh_list", False)
    if needs_list_refresh:
        jobs.status()["refresh_list"] = False

    # Drain any paper IDs scored since last poll and render OOB card updates
    oob_cards: list[str] = []
    if not needs_list_refresh:
        # Only do per-card OOB updates when we're NOT doing a full list refresh
        # (a full refresh already includes the latest card state)
        while _scored_paper_ids:
            pid = _scored_paper_ids.popleft()
            paper = get_paper_with_score(conn, pid)
            if paper:
                card_html = render_template("fragments/paper_card.html", paper=paper)
                card_html = card_html.replace(
                    f'id="paper-card-{pid}"',
                    f'id="paper-card-{pid}" hx-swap-oob="outerHTML"',
                    1,
                )
                oob_cards.append(card_html)
    else:
        _scored_paper_ids.clear()

    html = jobs.render_status_bar()

    if needs_list_refresh:
        # Trigger a full paper list reload via OOB swap
        from bmnews.db.operations import get_papers_filtered

        papers, total = get_papers_filtered(
            conn,
            sort="date",
            limit=DEFAULT_PAGE_SIZE,
            offset=0,
            with_total=True,
        )
        list_html = render_template(
            "fragments/paper_list.html",
            papers=papers,
            total=total,
            offset=0,
            limit=DEFAULT_PAGE_SIZE,
            sort="date",
            source="",
            tier="",
            design="",
            search="",
        )
        html += f'<div id="paper-list" hx-swap-oob="innerHTML">{list_html}</div>'
    elif oob_cards:
        html += "\n".join(oob_cards)

    return html
