"""Command-line interface for BioMedical News."""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import click

from bmnews import __version__
from bmnews.config import AppConfig, load_config

# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "-c", "--config",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Path to config file (default: ~/.bmnews/config.toml)",
)
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.version_option(__version__, prog_name="bmnews")
@click.pass_context
def main(ctx: click.Context, config: Path | None, verbose: bool):
    """BioMedical News — preprint discovery and delivery."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


def _load(ctx: click.Context) -> AppConfig:
    return load_config(ctx.obj.get("config_path"))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@main.command()
@click.pass_context
def run(ctx: click.Context):
    """Run the full pipeline: fetch, score, and send digest."""
    cfg = _load(ctx)
    from bmnews.pipeline import run as pipeline_run

    summary = pipeline_run(cfg)
    click.echo(f"Fetched: {summary['fetched']}")
    click.echo(f"Scored:  {summary['scored']}")
    click.echo(f"Digest:  {summary['digest_size']} papers")
    click.echo(f"Email:   {'sent' if summary['email_sent'] else 'not sent'}")


@main.command()
@click.option("--days", default=7, help="Look back N days")
@click.pass_context
def fetch(ctx: click.Context, days: int):
    """Fetch papers from configured sources (no scoring or email)."""
    cfg = _load(ctx)
    from bmnews.db.engine import init_engine, get_session
    from bmnews.db.models import Publication, create_tables
    from bmnews.db import repository as repo
    from bmnews.pipeline import _fetch_all, _store_papers

    engine = init_engine(cfg)
    create_tables(engine)

    since = date.today() - timedelta(days=days)
    papers = _fetch_all(cfg, since)
    click.echo(f"Fetched {len(papers)} papers")

    with get_session() as session:
        stored = _store_papers(session, papers)
    click.echo(f"Stored {len(stored)} papers in database")


@main.command()
@click.pass_context
def score(ctx: click.Context):
    """Score unscored publications for the configured user."""
    cfg = _load(ctx)
    from bmnews.db.engine import init_engine, get_session
    from bmnews.db.models import PublicationScore, UserProfile, create_tables
    from bmnews.db import repository as repo
    from bmnews.fetchers.base import FetchedPaper
    from bmnews.scoring.quality import score_quality
    from bmnews.scoring.relevance import get_scorer

    engine = init_engine(cfg)
    create_tables(engine)

    scorer = get_scorer(cfg.scoring.scorer)

    with get_session() as session:
        user = repo.get_or_create_user(
            session,
            name=cfg.user.name,
            email=cfg.user.email,
            interests=cfg.user.interests,
            min_relevance=cfg.scoring.min_relevance,
            min_quality=cfg.scoring.min_quality,
        )
        unscored = repo.get_unscored_publications(session, user.id)
        click.echo(f"Scoring {len(unscored)} publications ...")

        for pub in unscored:
            paper_proxy = FetchedPaper(
                doi=pub.doi, title=pub.title, authors=pub.authors or [],
                abstract=pub.abstract or "", url=pub.url or "",
                source=pub.source or "", published_date=pub.published_date,
                categories=pub.categories or [],
            )
            rel = scorer.score(paper_proxy, user.interests or [])
            qual = score_quality(paper_proxy)
            combined = 0.6 * rel + 0.4 * qual

            repo.save_score(session, PublicationScore(
                publication_id=pub.id,
                user_id=user.id,
                relevance_score=rel,
                quality_score=qual,
                combined_score=combined,
            ))
    click.echo("Done")


@main.command()
@click.option("--limit", default=20, help="Max papers to show")
@click.pass_context
def digest(ctx: click.Context, limit: int):
    """Generate and display/send the digest."""
    cfg = _load(ctx)
    from bmnews.db.engine import init_engine, get_session
    from bmnews.db.models import create_tables
    from bmnews.db import repository as repo
    from bmnews.digest.renderer import render_html, render_text
    from bmnews.digest.sender import send_digest

    engine = init_engine(cfg)
    create_tables(engine)

    with get_session() as session:
        user = repo.get_or_create_user(
            session,
            name=cfg.user.name,
            email=cfg.user.email,
            interests=cfg.user.interests,
            min_relevance=cfg.scoring.min_relevance,
            min_quality=cfg.scoring.min_quality,
        )
        top = repo.get_top_scored(
            session, user.id,
            min_relevance=cfg.scoring.min_relevance,
            min_quality=cfg.scoring.min_quality,
            limit=limit,
            exclude_sent=True,
        )

        if not top:
            click.echo("No papers above threshold.")
            return

        text = render_text(top)
        html = render_html(top)
        pub_ids = [pub.id for pub, _ in top]

        if cfg.email.smtp_host and cfg.email.smtp_user:
            ok = send_digest(
                config=cfg.email,
                to_address=cfg.user.email,
                subject=f"BioMedical News Digest — {date.today().strftime('%B %d, %Y')}",
                html_body=html,
                text_body=text,
            )
            repo.record_digest(session, user.id, pub_ids, "sent" if ok else "failed")
            click.echo("Digest sent." if ok else "Failed to send digest.")
        else:
            click.echo(text)
            repo.record_digest(session, user.id, pub_ids, "printed")


@main.command()
@click.pass_context
def init(ctx: click.Context):
    """Initialise the database and create a default config file."""
    cfg = _load(ctx)
    from bmnews.db.engine import init_engine
    from bmnews.db.models import create_tables

    engine = init_engine(cfg)
    create_tables(engine)
    click.echo(f"Database initialised ({cfg.database_backend})")

    config_path = ctx.obj.get("config_path") or Path("~/.bmnews/config.toml").expanduser()
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # Copy example config
        example = Path(__file__).parent.parent / "config.example.toml"
        if example.exists():
            config_path.write_text(example.read_text())
            click.echo(f"Config written to {config_path}")
        else:
            click.echo(f"Create your config at {config_path}")
    else:
        click.echo(f"Config already exists: {config_path}")


@main.command()
@click.option("--query", "-q", required=True, help="Search query")
@click.option("--limit", default=10, help="Max results")
@click.pass_context
def search(ctx: click.Context, query: str, limit: int):
    """Search stored publications by keyword."""
    cfg = _load(ctx)
    from sqlalchemy import select, or_
    from bmnews.db.engine import init_engine, get_session
    from bmnews.db.models import Publication, create_tables

    engine = init_engine(cfg)
    create_tables(engine)

    with get_session() as session:
        q = query.lower()
        # Simple LIKE search — works on both SQLite and PostgreSQL
        results = list(session.execute(
            select(Publication).where(
                or_(
                    Publication.title.ilike(f"%{q}%"),
                    Publication.abstract.ilike(f"%{q}%"),
                )
            ).limit(limit)
        ).scalars().all())

        if not results:
            click.echo("No results found.")
            return

        for pub in results:
            click.echo(f"\n  {pub.title}")
            if pub.authors:
                click.echo(f"  Authors: {', '.join(pub.authors[:3])}")
            click.echo(f"  Source: {pub.source}  Date: {pub.published_date}")
            click.echo(f"  URL: {pub.url or 'N/A'}")


if __name__ == "__main__":
    main()
