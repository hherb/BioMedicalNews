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
from bmlib.publications.fetchers import list_sources, get_fetcher, source_names
from bmlib.publications.models import FetchedRecord

from bmnews.fetchers import FetchedPaper, fetch_europepmc
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
        api_key=config.llm.api_key or None,
        base_url=config.llm.base_url or None,
    )


def _record_to_fetched_paper(record: FetchedRecord) -> FetchedPaper:
    """Convert a bmlib :class:`FetchedRecord` to a bmnews :class:`FetchedPaper`."""
    doi = record.doi or ""
    url = f"https://doi.org/{doi}" if doi else ""
    authors = "; ".join(record.authors) if record.authors else ""
    categories = "; ".join(record.keywords) if record.keywords else ""
    metadata: dict[str, Any] = {}
    if record.pmid:
        metadata["pmid"] = record.pmid
    if record.pmc_id:
        metadata["pmcid"] = record.pmc_id
    if record.fulltext_sources:
        metadata["fulltext_sources"] = [s.to_dict() for s in record.fulltext_sources]
    if record.extras:
        metadata.update(record.extras)
    return FetchedPaper(
        doi=doi,
        title=record.title,
        authors=authors,
        abstract=record.abstract or "",
        url=url,
        source=record.source,
        published_date=record.publication_date or "",
        categories=categories,
        metadata=metadata,
    )


def _fetch_via_registry(
    source_name: str,
    lookback_days: int,
    source_config: dict[str, str],
    on_progress: Callable[[str], None] | None = None,
) -> list[FetchedPaper]:
    """Fetch papers from a bmlib-registered source via the registry."""
    import httpx
    from datetime import date, timedelta

    fetcher = get_fetcher(source_name)
    end = date.today()
    start = end - timedelta(days=lookback_days)

    papers: list[FetchedPaper] = []

    with httpx.Client(timeout=30.0) as client:
        current = start
        while current <= end:
            collected: list[FetchedRecord] = []
            fetcher(
                client, current,
                on_record=collected.append,
                **source_config,
            )
            for record in collected:
                papers.append(_record_to_fetched_paper(record))
            current += timedelta(days=1)

    return papers


# Sources handled locally (not in bmlib registry)
_LOCAL_SOURCES: dict[str, str] = {
    "europepmc": "Europe PMC",
}


def run_fetch(
    config: AppConfig,
    on_progress: Callable[[str], None] | None = None,
) -> list[FetchedPaper]:
    """Fetch papers from all configured sources."""
    papers: list[FetchedPaper] = []
    lookback = config.sources.lookback_days
    registry_names = set(source_names())

    for source_name in config.sources.enabled:
        if source_name in registry_names:
            if on_progress:
                on_progress(f"Fetching from {source_name}...")
            logger.info("Fetching from %s (registry)...", source_name)
            src_config = dict(config.sources.source_options.get(source_name, {}))
            # Provide email for openalex if available
            if source_name == "openalex" and "email" not in src_config and config.user.email:
                src_config["email"] = config.user.email
            papers.extend(_fetch_via_registry(
                source_name, lookback, src_config, on_progress,
            ))
        elif source_name == "europepmc":
            if on_progress:
                on_progress("Fetching from Europe PMC...")
            logger.info("Fetching from EuropePMC...")
            papers.extend(fetch_europepmc(
                query=config.sources.europepmc_query,
                lookback_days=lookback,
            ))
        else:
            logger.warning("Unknown source %r — skipping", source_name)

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
    on_scored: Callable[[int], None] | None = None,
) -> int:
    """Score unscored papers. Returns count of papers scored.

    Args:
        config: Application config.
        on_progress: Optional callback receiving a status message string.
        on_scored: Optional callback receiving the paper_id after each
            score is committed to the database.
    """
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

    scored_count = 0

    def _score_progress(i: int, _total: int, result: Any) -> None:
        nonlocal scored_count
        # Save each score immediately so the GUI sees updates
        if isinstance(result, dict):
            paper_id = result["paper_id"]
            tags = result.pop("matched_tags", [])
            save_score(conn, **result)
            if tags:
                save_paper_tags(conn, paper_id=paper_id, tags=tags)
            scored_count += 1
            if on_scored:
                on_scored(paper_id)
        if on_progress:
            on_progress(f"Scoring paper {i}/{_total}...")

    score_papers(
        papers=unscored,
        llm=llm,
        model=model,
        template_engine=templates,
        interests=config.user.research_interests,
        concurrency=config.llm.concurrency,
        quality_tier=config.quality.default_tier,
        progress_callback=_score_progress,
    )

    conn.close()
    logger.info("Scored %d papers", scored_count)
    return scored_count


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
    on_scored: Callable[[int], None] | None = None,
) -> None:
    """Execute the full pipeline: fetch → store → score → digest.

    Args:
        config: Application config.
        days: Override lookback_days for fetching.
        show_cached: If True, skip pipeline and show cached digests.
        on_progress: Optional callback receiving a status message string.
        on_scored: Optional callback receiving the paper_id after each
            score is committed.
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

    scored = run_score(config, on_progress=on_progress, on_scored=on_scored)
    if scored > 0:
        if on_progress:
            on_progress("Generating digest...")
        run_digest(config)

    logger.info("Pipeline complete")
