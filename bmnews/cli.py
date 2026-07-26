"""Command-line interface for bmnews."""

from __future__ import annotations

import logging
import sys
from contextlib import closing

import click

from bmnews import __version__
from bmnews.config import load_config, write_default_config
from bmnews.constants import CLI_TITLE_TRUNCATE, DEFAULT_PAGE_SIZE


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
    """Run the full pipeline: fetch → score → digest."""
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
