"""Main orchestration pipeline.

Runs the full fetch → store → score → digest → deliver cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from bmlib.llm import LLMClient
from bmlib.llm.providers import list_providers
from bmlib.publications import source_names, sync
from bmlib.publications.models import FetchedRecord, SyncProgress, SyncReport
from bmlib.templates import TemplateEngine

# Importing the package registers the bmnews-supplied sources (Europe PMC)
# into bmlib's registry, so every enabled source resolves through it.
import bmnews.fetchers  # noqa: F401
from bmnews.config import AppConfig
from bmnews.constants import EXTRAS_FLUSH_THRESHOLD, UNSCORED_BATCH_SIZE
from bmnews.db.operations import (
    get_cached_digest_papers,
    get_papers_for_digest,
    get_unscored_papers,
    publication_id,
    record_digest,
    save_paper_metadata,
    save_paper_tags,
    save_score,
)
from bmnews.db.schema import init_db, open_db
from bmnews.digest.renderer import render_digest
from bmnews.digest.sender import send_email
from bmnews.scoring.scorer import score_papers, tiers_below

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


def _source_configs(config: AppConfig) -> dict[str, dict[str, Any]]:
    """Build the per-source keyword arguments bmlib's fetchers receive."""
    configs: dict[str, dict[str, Any]] = {}
    for source_name in config.sources.enabled:
        src_config = dict(config.sources.source_options.get(source_name, {}))
        # OpenAlex asks for a contact address for polite API access.
        if source_name == "openalex" and "email" not in src_config and config.user.email:
            src_config["email"] = config.user.email
        configs[source_name] = src_config
    return configs


def _known_sources(config: AppConfig) -> list[str]:
    """Return the enabled sources bmlib's registry actually knows about."""
    registry_names = set(source_names())
    known = []
    for source_name in config.sources.enabled:
        if source_name in registry_names:
            known.append(source_name)
        else:
            logger.warning(
                "Unknown source %r — skipping. Known sources: %s",
                source_name, ", ".join(sorted(registry_names)),
            )
    return known


def _progress_reporter(
    on_progress: Callable[[str], None] | None,
) -> Callable[[SyncProgress], None] | None:
    """Adapt bmlib's :class:`SyncProgress` to bmnews's string callback.

    The GUI status bar and the CLI both take a plain message, so the
    structured progress record is rendered down to one.
    """
    if on_progress is None:
        return None

    def report(progress: SyncProgress) -> None:
        counts = ""
        if progress.records_total:
            counts = f" ({progress.records_processed}/{progress.records_total})"
        detail = f" — {progress.message}" if progress.message else ""
        on_progress(f"Fetching from {progress.source}: {progress.date}{counts}{detail}")

    return report


def _record_extras(record: FetchedRecord) -> dict[str, Any]:
    """Pull out the source-specific fields bmlib's schema has no column for.

    Everything else a fetcher reports — identifiers, journal, publication
    types, license, open access, full-text source URLs — now has a real column
    or table of its own, so only the genuine leftovers are kept here.
    """
    extras = dict(record.extras) if record.extras else {}
    # Europe PMC supplies a ready-made ``url``; bmnews derives its links from
    # the identifiers instead (see ``operations.paper_url``), so it would only
    # go stale in storage.
    extras.pop("url", None)
    return extras


def run_sync(
    config: AppConfig,
    on_progress: Callable[[str], None] | None = None,
) -> SyncReport:
    """Fetch from every configured source and store what comes back.

    Delegates the whole fetch-and-store cycle to :func:`bmlib.publications.sync`,
    which brings three things bmnews's own loop did not have: days that failed
    are recorded and retried on the next run rather than silently lost, records
    are deduplicated across sources by DOI *and* PMID (so a paper with no DOI is
    stored instead of dropped), and each day is written in a single transaction
    whose write lock is not held across network I/O.

    Args:
        config: Application config.
        on_progress: Optional callback receiving a status message string.

    Returns:
        bmlib's :class:`SyncReport` for the run.
    """
    sources = _known_sources(config)
    if not sources:
        logger.warning("No known sources enabled — nothing to fetch")
        return SyncReport(
            sources_synced=[], days_processed=0, records_added=0,
            records_merged=0, records_failed=0,
        )

    end = date.today()
    start = end - timedelta(days=config.sources.lookback_days)

    with closing(open_db(config)) as conn:
        init_db(conn)

        # bmlib hands each record to on_record *before* storing it, so extras
        # are buffered here and written once the publication ids exist.  See
        # ``_store_extras`` for how the buffer is drained without growing with
        # the size of the lookback window.
        pending_extras: dict[tuple[str | None, str | None], dict[str, Any]] = {}

        def collect_extras(record: FetchedRecord) -> None:
            extras = _record_extras(record)
            if extras:
                pending_extras[(record.doi, record.pmid)] = extras
            if len(pending_extras) >= EXTRAS_FLUSH_THRESHOLD:
                _store_extras(conn, pending_extras)

        try:
            report = sync(
                conn,
                sources=sources,
                date_from=start,
                date_to=end,
                source_configs=_source_configs(config),
                on_record=collect_extras,
                on_progress=_progress_reporter(on_progress),
            )
        finally:
            # bmlib commits each day as it goes, so a sync that dies partway
            # still leaves earlier days durably stored.  Draining here rather
            # than only on success means their extras are stored too, instead
            # of being discarded with the exception — the next run will not
            # re-deliver those records, because bmlib now considers them known.
            try:
                _store_extras(conn, pending_extras)
            except Exception:
                # Never let a failed drain replace the exception that is
                # already on its way out; extras are the least of the losses.
                logger.exception("Could not store buffered source extras")

    logger.info(
        "Sync complete: %d added, %d merged, %d failed across %d day(s)",
        report.records_added, report.records_merged,
        report.records_failed, report.days_processed,
    )
    for error in report.errors:
        logger.warning("Sync error: %s", error)

    return report


def _store_extras(
    conn: Any,
    pending: dict[tuple[str | None, str | None], dict[str, Any]],
) -> None:
    """Persist buffered source extras, keeping the ones not yet storable.

    Drains *pending* in place.  An entry whose publication cannot be resolved
    yet is left in the buffer rather than dropped: bmlib stores a day's records
    only after that day's fetch completes, so the records currently in flight
    have no row to attach to until the day commits.  Anything still unresolved
    when the sync ends never made it into the database at all — a record that
    failed to store, or one with no usable identifier — so the final drain
    discards it.

    This bounds the buffer to roughly one day of in-flight records rather than
    the whole lookback window.

    Args:
        conn: DB-API connection.
        pending: Extras keyed by ``(doi, pmid)``, emptied of everything that
            could be written.
    """
    unresolved: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for key, extras in pending.items():
        doi, pmid = key
        paper_id = publication_id(conn, doi=doi, pmid=pmid)
        if paper_id is None:
            unresolved[key] = extras
            continue
        # Merge rather than replace: two records for the same work can carry
        # different identifier tuples (a DOI-only one and a DOI+PMID one) and
        # still resolve to this same publication, so overwriting here would
        # discard whatever the other source contributed.
        save_paper_metadata(conn, paper_id=paper_id, metadata=extras)

    pending.clear()
    pending.update(unresolved)


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
    scored_count = 0

    with closing(open_db(config)) as conn:
        init_db(conn)

        unscored = get_unscored_papers(conn, limit=UNSCORED_BATCH_SIZE)
        if not unscored:
            logger.info("No unscored papers found")
            return 0

        total = len(unscored)
        if total == UNSCORED_BATCH_SIZE:
            logger.warning(
                "Scoring the %d most recently stored unscored papers; more may "
                "remain. Re-run `bmnews score` to continue.", UNSCORED_BATCH_SIZE,
            )
        logger.info("Scoring %d papers...", total)
        if on_progress:
            on_progress(f"Scoring {total} papers...")

        llm = build_llm_client(config)
        templates = build_template_engine(config)
        model = _resolve_model_string(config)

        def _score_progress(i: int, _total: int, result: Any) -> None:
            """Persist one paper's score and report progress.

            Args:
                i: 1-based index of the paper just scored.
                _total: Total number of papers in this run.
                result: The scoring result dict for that paper.
            """
            nonlocal scored_count
            # Save each score immediately so the GUI sees updates
            if isinstance(result, dict):
                # Copy: matched_tags is not a `scores` column, and popping it
                # from the caller's dict would corrupt the returned results.
                fields = dict(result)
                paper_id = fields["paper_id"]
                tags = fields.pop("matched_tags", [])
                save_score(conn, **fields)
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
            quality_enabled=config.quality.enabled,
            quality_tier=min(config.quality.default_tier, config.quality.max_tier),
            temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
            progress_callback=_score_progress,
        )

    logger.info("Scored %d papers", scored_count)
    return scored_count


def _resolve_model_string(config: AppConfig) -> str:
    """Build the ``"provider:model"`` string that bmlib's LLMClient expects.

    ``config.llm.model`` may hold either an already-qualified
    ``"anthropic:claude-sonnet-4-5"`` or a bare Ollama name with a tag such as
    ``"llama3.1:latest"``. These are told apart by asking bmlib which provider
    names it actually knows, so a provider added to bmlib needs no change here.

    Args:
        config: Application config.

    Returns:
        A ``"provider:model"`` string.
    """
    raw_model = config.llm.model
    if not raw_model:
        return f"{config.llm.provider}:"
    prefix = raw_model.split(":", 1)[0].lower()
    if ":" in raw_model and prefix in {p.lower() for p in list_providers()}:
        return raw_model
    return f"{config.llm.provider}:{raw_model}"


def run_digest(config: AppConfig, output: str | None = None) -> str:
    """Generate a digest from top-scoring papers.

    Args:
        config: Application config.
        output: If provided, write to this file path instead of stdout/email.

    Returns:
        The rendered digest text.
    """
    with closing(open_db(config)) as conn:
        init_db(conn)

        papers = get_papers_for_digest(
            conn,
            min_combined=config.scoring.min_combined,
            max_papers=config.email.max_papers,
            min_relevance=config.scoring.min_relevance,
            exclude_tiers=(
                tiers_below(config.quality.min_quality_tier)
                if config.quality.enabled else ()
            ),
        )

        if not papers:
            logger.info("No papers above threshold for digest")
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
            record_digest(
                conn, paper_ids,
                delivery_method="email" if success else "email_failed",
            )
        else:
            print(text_body)
            record_digest(conn, paper_ids, delivery_method="stdout")

    return text_body


def show_cached_digests(config: AppConfig, days: int | None = None) -> str:
    """Re-render previously digested papers to stdout.

    Args:
        config: Application config.
        days: If provided, filter to papers published in the last N days.

    Returns:
        Rendered text, or empty string if no cached papers.
    """
    with closing(open_db(config)) as conn:
        init_db(conn)
        papers = get_cached_digest_papers(conn, days=days)

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

    run_sync(config, on_progress=on_progress)

    scored = run_score(config, on_progress=on_progress, on_scored=on_scored)
    if scored > 0:
        if on_progress:
            on_progress("Generating digest...")
        run_digest(config)

    logger.info("Pipeline complete")
