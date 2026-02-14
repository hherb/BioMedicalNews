"""Command-line interface for bmnews."""

from __future__ import annotations

import logging
import sys

import click

from bmnews import __version__
from bmnews.config import load_config, write_default_config


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

    level = logging.DEBUG if verbose else getattr(logging, config.log_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option("--days", default=None, type=int, help="Override lookback days for fetching.")
@click.option("--show_cached", is_flag=True, default=False,
              help="Show cached digests instead of running pipeline.")
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
    from bmnews.pipeline import run_fetch, run_store

    config = ctx.obj["config"]
    if days is not None:
        config.sources.lookback_days = days

    papers = run_fetch(config)
    if papers:
        stored = run_store(config, papers)
        click.echo(f"Fetched and stored {stored} papers.")
    else:
        click.echo("No papers fetched.")


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
    conn = open_db(config)
    init_db(conn)
    conn.close()
    click.echo("Database initialized.")


@main.command()
@click.option("--port", default=None, type=int, help="Fixed port for Flask server (default: auto).")
@click.pass_context
def gui(ctx: click.Context, port: int | None) -> None:
    """Launch the desktop GUI."""
    try:
        from bmnews.gui.launcher import launch
    except ImportError:
        click.echo("GUI dependencies not installed. Run: uv pip install bmnews[gui]")
        sys.exit(1)

    launch(ctx.obj["config"], port=port)


@main.command()
@click.argument("query")
@click.pass_context
def search(ctx: click.Context, query: str) -> None:
    """Search stored papers by keyword."""
    from bmlib.db import fetch_all
    from bmnews.db.schema import open_db

    config = ctx.obj["config"]
    conn = open_db(config)

    ph = "?" if "sqlite3" in type(conn).__module__ else "%s"
    like_param = f"%{query}%"
    rows = fetch_all(
        conn,
        f"""
        SELECT p.doi, p.title, p.published_date, s.combined_score
        FROM papers p
        LEFT JOIN scores s ON s.paper_id = p.id
        WHERE p.title LIKE {ph} OR p.abstract LIKE {ph}
        ORDER BY s.combined_score DESC NULLS LAST
        LIMIT 20
        """,
        (like_param, like_param),
    )

    if not rows:
        click.echo("No papers found.")
    else:
        for row in rows:
            score_str = f" [{row['combined_score']:.2f}]" if row["combined_score"] else ""
            click.echo(f"  {row['doi']}{score_str} — {row['title'][:80]}")

    conn.close()


if __name__ == "__main__":
    main()
