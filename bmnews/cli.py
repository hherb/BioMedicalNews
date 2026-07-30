"""Command-line interface for bmnews."""

from __future__ import annotations

import logging
import sys
from contextlib import closing

import click

from bmnews import __version__
from bmnews.config import load_config, write_default_config
from bmnews.constants import CLI_TITLE_TRUNCATE, DEFAULT_PAGE_SIZE
from bmnews.metadata import parse_transparency


@click.group()
@click.option("-c", "--config", "config_path", default=None, help="Path to config file.")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.version_option(version=__version__)
@click.pass_context
def main(ctx: click.Context, config_path: str | None, verbose: bool) -> None:
    """BioMedical News Reader — discover relevant preprints."""
    ctx.ensure_object(dict)
    config = load_config(config_path)
    ctx.obj["config"] = config

    # getattr on a lowercase name silently falls back to INFO, so normalise.
    configured = getattr(logging, str(config.log_level).upper(), logging.INFO)
    level = logging.DEBUG if verbose else configured
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option("--days", default=None, type=int, help="Override lookback days for fetching.")
@click.option(
    "--show_cached",
    is_flag=True,
    default=False,
    help="Show cached digests instead of running pipeline.",
)
@click.pass_context
def run(ctx: click.Context, days: int | None, show_cached: bool) -> None:
    """Run the full pipeline: fetch → score → transparency → notify → digest."""
    from bmnews.pipeline import run_pipeline

    run_pipeline(ctx.obj["config"], days=days, show_cached=show_cached)


@main.command()
@click.option("--days", default=None, type=int, help="Override lookback days.")
@click.pass_context
def fetch(ctx: click.Context, days: int | None) -> None:
    """Fetch papers from configured sources."""
    from bmnews.pipeline import run_sync

    config = ctx.obj["config"]
    if days is not None:
        config.sources.lookback_days = days

    report = run_sync(config)
    stored = report.records_added + report.records_merged
    if stored:
        click.echo(
            f"Fetched and stored {stored} papers "
            f"({report.records_added} new, {report.records_merged} merged)."
        )
    else:
        click.echo("No papers fetched.")
    for error in report.errors:
        click.echo(f"  warning: {error}", err=True)


@main.command()
@click.pass_context
def score(ctx: click.Context) -> None:
    """Score unscored papers for relevance and quality."""
    from bmnews.pipeline import run_score

    count = run_score(ctx.obj["config"])
    click.echo(f"Scored {count} papers.")


@main.command()
@click.option("-o", "--output", default=None, help="Write digest to file instead of stdout/email.")
@click.pass_context
def digest(ctx: click.Context, output: str | None) -> None:
    """Generate and deliver a digest of top papers."""
    from bmnews.pipeline import run_digest

    text = run_digest(ctx.obj["config"], output=output)
    if not text:
        click.echo("No papers above threshold for digest.")


@main.command()
@click.option("--watch", default="", help="Only act on this watch.")
@click.option("--count", default=None, type=int, help="Deliver this many per watch.")
@click.option("--all", "drain", is_flag=True, help="Deliver every pending match, not one batch.")
@click.option("--dry-run", is_flag=True, help="Show what would be sent; deliver nothing.")
@click.option("--list", "list_only", is_flag=True, help="Report each watch and deliver nothing.")
@click.pass_context
def notify(
    ctx: click.Context,
    watch: str,
    count: int | None,
    drain: bool,
    dry_run: bool,
    list_only: bool,
) -> None:
    """Deliver watch notifications for newly matching papers.

    Nothing is ever silently dropped: a watch's max_per_run bounds one batch
    and the rest stay queued. Run the command again to pull the next batch,
    or --all to drain it.
    """
    from bmnews.notify import service

    config = ctx.obj["config"]

    # Both mean "how many", and --all wins. Honouring one and discarding the
    # other without saying so would deliver a number nobody asked for.
    if drain and count is not None:
        raise click.UsageError("--all and --count both set the batch size; use one or the other.")
    if count is not None and count < 1:
        raise click.UsageError("--count must be at least 1.")

    if list_only:
        reports = service.pending_counts(config, watch=watch)
        if not reports:
            click.echo("No watches configured.")
            return
        for report in reports:
            state = "" if report.enabled else " (disabled)"
            click.echo(
                f"{report.watch} → {report.channel}{state}: "
                f"{report.sent_total} delivered, {report.matching} matching, "
                f"{report.remaining} remaining"
            )
        return

    reports = service.run_notify(config, watch=watch, count=count, drain=drain, dry_run=dry_run)

    delivered = sum(report.delivered for report in reports)
    failed = sum(report.failed for report in reports)

    if not delivered and not failed:
        click.echo("Nothing to notify.")
        return

    prefix = "Would deliver" if dry_run else "Delivered"
    for report in reports:
        if report.delivered:
            remaining = f", {report.remaining} remaining" if report.remaining else ""
            click.echo(
                f"{prefix} {report.delivered} paper(s) for {report.watch} "
                f"→ {report.channel}{remaining}"
            )
        if report.failed:
            click.echo(
                f"Failed to deliver {report.failed} paper(s) for {report.watch} "
                f"→ {report.channel} — they stay queued and will be retried",
                err=True,
            )

    # A run whose deliveries all failed has done nothing the user asked for,
    # and a cron job that cannot tell is a cron job that never reports it.
    if failed and not delivered:
        ctx.exit(1)


@main.command()
@click.option("--limit", default=None, type=int, help="Analyse at most this many papers.")
@click.option("--refresh", is_flag=True, help="Re-analyse papers that already have a result.")
@click.option(
    "--paper-id", default=None, type=int, help="Restrict to one paper, ignoring the gate."
)
@click.option("--list", "list_only", is_flag=True, help="Print stored results; analyse nothing.")
@click.option("--dry-run", is_flag=True, help="Report what would be analysed; call no API.")
@click.pass_context
def transparency(
    ctx: click.Context,
    limit: int | None,
    refresh: bool,
    paper_id: int | None,
    list_only: bool,
    dry_run: bool,
) -> None:
    """Assess research integrity for scored papers.

    Checks funder disclosure, COI statements, data availability and trial
    results reporting against CrossRef, Europe PMC, PubMed, OpenAlex and
    ClinicalTrials.gov. Results are displayed beside a paper and never change
    which papers are selected or how they rank.

    Each analysis costs several external requests, so only papers scoring above
    transparency.min_combined_score are analysed. --paper-id ignores that gate,
    but does not by itself redo a paper that already has a determinate result —
    combine it with --refresh to force that.
    """
    from bmnews.transparency import service

    config = ctx.obj["config"]

    if limit is not None and limit < 1:
        raise click.UsageError("--limit must be at least 1.")
    # Refusing rather than quietly ignoring: --list means "analyse nothing" and
    # these two mean "analyse differently", so honouring one and dropping the
    # other would do something the user did not ask for.
    if list_only and (refresh or dry_run):
        raise click.UsageError("--list analyses nothing; drop --refresh/--dry-run.")

    if list_only:
        rows = service.list_results(config, limit=limit)
        if not rows:
            click.echo("No results stored yet. Run `bmnews transparency` first.")
            return
        for row in rows:
            click.echo(
                f"{row['risk_level'].upper()} {row['transparency_score']}/100 — "
                f"{row['title']} ({row['doi'] or 'no DOI'})"
            )
            for indicator in parse_transparency(row["result_json"]).get("risk_indicators", []):
                click.echo(f"    - {indicator}")
        return

    if not config.transparency.enabled:
        click.echo(
            "Transparency analysis is disabled. Set enabled = true under "
            "[transparency] in your config to turn it on."
        )
        return

    report = service.run_transparency(
        config, refresh=refresh, paper_id=paper_id, limit=limit, dry_run=dry_run
    )

    if dry_run:
        click.echo(f"Would analyse {report.candidates} paper(s).")
        return

    if not report.analyzed and not report.failed:
        click.echo("Nothing to analyse.")
        return

    click.echo(f"Analysed {report.analyzed} paper(s).")
    if report.indeterminate:
        click.echo(
            f"{report.indeterminate} could not be determined "
            f"({report.exhausted} will not be retried without --refresh)."
        )
    if report.failed:
        click.echo(
            f"{report.failed} analysis attempt(s) failed — they stay queued and retry.",
            err=True,
        )


@main.command()
@click.option("--config-path", default=None, help="Where to create the config file.")
@click.pass_context
def init(ctx: click.Context, config_path: str | None) -> None:
    """Initialize database and create default config file."""
    from bmnews.db.schema import init_db, open_db

    # Create config
    path = write_default_config(config_path)
    click.echo(f"Config file: {path}")

    # Init database
    config = load_config(path)
    with closing(open_db(config)) as conn:
        init_db(conn)
    click.echo("Database initialized.")


@main.command()
@click.option("--port", default=None, type=int, help="Fixed port for Flask server (default: auto).")
@click.pass_context
def gui(ctx: click.Context, port: int | None) -> None:
    """Launch the desktop GUI."""
    # launch() imports pywebview lazily, so the missing-dependency case only
    # surfaces once it runs — importing the module alone would not catch it.
    try:
        from bmnews.gui.launcher import launch

        launch(ctx.obj["config"], port=port)
    except ImportError as e:
        click.echo(f"GUI dependencies not installed ({e}).")
        click.echo("Run: uv pip install 'bmnews[gui]'")
        sys.exit(1)


@main.command()
@click.argument("query")
@click.option(
    "--limit",
    default=DEFAULT_PAGE_SIZE,
    type=int,
    show_default=True,
    help="Maximum number of results to show.",
)
@click.pass_context
def search(ctx: click.Context, query: str, limit: int) -> None:
    """Search stored papers by keyword."""
    from bmnews.db.operations import get_papers_filtered
    from bmnews.db.schema import open_db

    config = ctx.obj["config"]
    with closing(open_db(config)) as conn:
        papers = get_papers_filtered(conn, search=query, limit=limit)

    if not papers:
        click.echo("No papers found.")
        return

    for paper in papers:
        score = paper.get("combined_score")
        score_str = f" [{score:.2f}]" if score is not None else ""
        title = (paper.get("title") or "")[:CLI_TITLE_TRUNCATE]
        click.echo(f"  {paper.get('doi', '')}{score_str} — {title}")


if __name__ == "__main__":
    main()
