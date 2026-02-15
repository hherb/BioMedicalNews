"""Main orchestration pipeline.

Runs the full fetch → store → score → digest → deliver cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from bmlib.db import transaction
from bmlib.llm import LLMClient
from bmlib.templates import TemplateEngine

from bmnews.config import AppConfig
from bmnews.db.schema import init_db, open_db
from bmnews.db.operations import (
    upsert_paper,
    update_paper_identifiers,
    get_unscored_papers,
    save_score,
    save_paper_tags,
    get_papers_for_digest,
    get_cached_digest_papers,
    record_digest,
)
from bmnews.fetchers import FetchedPaper, fetch_medrxiv, fetch_biorxiv, fetch_europepmc
from bmnews.scoring.scorer import score_papers
from bmnews.digest.renderer import render_digest
from bmnews.digest.sender import send_email

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def build_template_engine(config: AppConfig) -> TemplateEngine:
    """Build a TemplateEngine from config, with package defaults as fallback."""
    user_dir = Path(config.template_dir).expanduser() if config.template_dir else None
    return TemplateEngine(user_dir=user_dir, default_dir=TEMPLATES_DIR)


def build_llm_client(config: AppConfig) -> LLMClient:
    """Build an LLM client from config."""
    return LLMClient(
        default_provider=config.llm.provider,
        ollama_host=config.llm.ollama_host or None,
        anthropic_api_key=config.llm.anthropic_api_key or None,
    )


def run_fetch(
    config: AppConfig,
    on_progress: Callable[[str], None] | None = None,
) -> list[FetchedPaper]:
    """Fetch papers from all configured sources."""
    papers: list[FetchedPaper] = []
    lookback = config.sources.lookback_days

    if config.sources.medrxiv:
        if on_progress:
            on_progress("Fetching from medRxiv...")
        logger.info("Fetching from medRxiv...")
        papers.extend(fetch_medrxiv(lookback_days=lookback))

    if config.sources.biorxiv:
        if on_progress:
            on_progress("Fetching from bioRxiv...")
        logger.info("Fetching from bioRxiv...")
        papers.extend(fetch_biorxiv(lookback_days=lookback))

    if config.sources.europepmc:
        if on_progress:
            on_progress("Fetching from EuropePMC...")
        logger.info("Fetching from EuropePMC...")
        papers.extend(fetch_europepmc(
            query=config.sources.europepmc_query,
            lookback_days=lookback,
        ))

    logger.info("Total papers fetched: %d", len(papers))
    return papers


def run_store(config: AppConfig, papers: list[FetchedPaper]) -> int:
    """Store fetched papers in the database. Returns count of new papers."""
    conn = open_db(config)
    init_db(conn)

    stored = 0
    for paper in papers:
        pid = upsert_paper(
            conn,
            doi=paper.doi,
            title=paper.title,
            authors=paper.authors,
            abstract=paper.abstract,
            url=paper.url,
            source=paper.source,
            published_date=paper.published_date,
            categories=paper.categories,
            metadata_json=json.dumps(paper.metadata),
        )
        pmid = paper.metadata.get("pmid")
        pmcid = paper.metadata.get("pmcid")
        if pmid or pmcid:
            update_paper_identifiers(
                conn, paper_id=pid,
                pmid=pmid or None,
                pmcid=pmcid or None,
            )
        stored += 1

    conn.close()
    logger.info("Stored %d papers", stored)
    return stored


def run_score(
    config: AppConfig,
    on_progress: Callable[[str], None] | None = None,
) -> int:
    """Score unscored papers. Returns count of papers scored."""
    conn = open_db(config)
    init_db(conn)

    unscored = get_unscored_papers(conn)
    if not unscored:
        logger.info("No unscored papers found")
        conn.close()
        return 0

    total = len(unscored)
    logger.info("Scoring %d papers...", total)
    if on_progress:
        on_progress(f"Scoring {total} papers...")

    llm = build_llm_client(config)
    templates = build_template_engine(config)
    model = config.llm.model or f"{config.llm.provider}:"

    def _score_progress(i: int, _total: int, _result: Any) -> None:
        if on_progress:
            on_progress(f"Scoring paper {i}/{_total}...")

    results = score_papers(
        papers=unscored,
        llm=llm,
        model=model,
        template_engine=templates,
        interests=config.user.research_interests,
        concurrency=config.llm.concurrency,
        quality_tier=config.quality.default_tier,
        progress_callback=_score_progress,
    )

    for result in results:
        tags = result.pop("matched_tags", [])
        save_score(conn, **result)
        if tags:
            save_paper_tags(conn, paper_id=result["paper_id"], tags=tags)

    conn.close()
    logger.info("Scored %d papers", len(results))
    return len(results)


def run_digest(config: AppConfig, output: str | None = None) -> str:
    """Generate a digest from top-scoring papers.

    Args:
        config: Application config.
        output: If provided, write to this file path instead of stdout/email.

    Returns:
        The rendered digest text.
    """
    conn = open_db(config)
    init_db(conn)

    papers = get_papers_for_digest(
        conn,
        min_combined=config.scoring.min_combined,
        max_papers=config.email.max_papers,
    )

    if not papers:
        logger.info("No papers above threshold for digest")
        conn.close()
        return ""

    templates = build_template_engine(config)

    # Render both formats
    html_body = render_digest(
        papers, templates,
        subject_prefix=config.email.subject_prefix,
        fmt="html",
    )
    text_body = render_digest(
        papers, templates,
        subject_prefix=config.email.subject_prefix,
        fmt="text",
    )

    # Deliver
    paper_ids = [p["id"] for p in papers if "id" in p]

    if output:
        Path(output).write_text(html_body, encoding="utf-8")
        logger.info("Digest written to %s", output)
        record_digest(conn, paper_ids, delivery_method="file")
    elif config.email.enabled and config.email.smtp_host:
        subject = f"{config.email.subject_prefix} {datetime.now().strftime('%Y-%m-%d')}"
        success = send_email(
            html_body=html_body,
            text_body=text_body,
            subject=subject,
            from_address=config.email.from_address,
            to_address=config.email.to_address or config.user.email,
            smtp_host=config.email.smtp_host,
            smtp_port=config.email.smtp_port,
            smtp_user=config.email.smtp_user,
            smtp_password=config.email.smtp_password,
            use_tls=config.email.use_tls,
        )
        record_digest(conn, paper_ids, delivery_method="email" if success else "email_failed")
    else:
        print(text_body)
        record_digest(conn, paper_ids, delivery_method="stdout")

    conn.close()
    return text_body


def show_cached_digests(config: AppConfig, days: int | None = None) -> str:
    """Re-render previously digested papers to stdout.

    Args:
        config: Application config.
        days: If provided, filter to papers published in the last N days.

    Returns:
        Rendered text, or empty string if no cached papers.
    """
    conn = open_db(config)
    init_db(conn)

    papers = get_cached_digest_papers(conn, days=days)
    conn.close()

    if not papers:
        logger.info("No cached digest papers found")
        return ""

    templates = build_template_engine(config)
    text_body = render_digest(
        papers, templates,
        subject_prefix=config.email.subject_prefix,
        fmt="text",
    )
    print(text_body)
    return text_body


def run_pipeline(
    config: AppConfig,
    days: int | None = None,
    show_cached: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> None:
    """Execute the full pipeline: fetch → store → score → digest.

    Args:
        config: Application config.
        days: Override lookback_days for fetching.
        show_cached: If True, skip pipeline and show cached digests.
        on_progress: Optional callback receiving a status message string.
    """
    if show_cached:
        show_cached_digests(config, days=days)
        return

    if days is not None:
        config.sources.lookback_days = days

    logger.info("Starting pipeline run")

    papers = run_fetch(config, on_progress=on_progress)
    if papers:
        if on_progress:
            on_progress(f"Storing {len(papers)} papers...")
        run_store(config, papers)

    scored = run_score(config, on_progress=on_progress)
    if scored > 0:
        if on_progress:
            on_progress("Generating digest...")
        run_digest(config)

    logger.info("Pipeline complete")
