"""The transparency stage: select, analyse, store.

Sits between scoring and the notifications as a fifth stage, and like the
notify stage it is **query-based**: it asks which scored papers still want a
result rather than being driven by a per-paper callback, so it survives a crash
mid-run and tests without running the scorer at all.

It **informs only**. A result is displayed beside a paper and never changes
which papers are selected or how they rank — bmlib's ``tier_downgrade_applied``
is stored and not applied. A value derived from five external APIs must not be
able to move a ``combined_score`` the user has already acted on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from bmlib.transparency import TransparencyAnalyzer, TransparencyRisk, TransparencySettings

from bmnews.config import AppConfig
from bmnews.constants import (
    DEFAULT_CONTACT_EMAIL,
    TRANSPARENCY_BATCH_SIZE,
    TRANSPARENCY_MAX_ATTEMPTS,
)
from bmnews.db.operations import (
    get_transparency_candidates,
    get_transparency_results,
    save_transparency,
)
from bmnews.db.schema import init_db, open_db

logger = logging.getLogger(__name__)

#: bmlib's own string for an indeterminate result, read from the enum rather
#: than spelled out so a rename upstream cannot silently stop matching.
_UNKNOWN = TransparencyRisk.UNKNOWN.value


@dataclass(frozen=True)
class TransparencyReport:
    """What one transparency run did.

    ``indeterminate`` and ``exhausted`` are nested subsets of ``analyzed``, not
    disjoint buckets: ``analyzed - indeterminate`` is how many papers were
    actually assessed, and ``exhausted`` is how many of the remainder will never
    be attempted again without ``--refresh``. That last number is the one worth
    surfacing, because it is the only outcome waiting will not fix.

    Attributes:
        candidates: Papers selected. The only field a dry run fills.
        analyzed: Results stored, determinate or not.
        indeterminate: Subset of ``analyzed`` that came back UNKNOWN.
        exhausted: Subset of ``indeterminate`` now at the attempt ceiling.
        failed: Papers this run could not record — the analysis raised, or it
            succeeded and the write did. Either way no row was written, so
            they retry; the two are one count because the caller's response to
            both is the same.
    """

    candidates: int = 0
    analyzed: int = 0
    indeterminate: int = 0
    exhausted: int = 0
    failed: int = 0


def build_settings(config: AppConfig) -> TransparencySettings:
    """Build bmlib's settings object from bmnews's four config fields.

    ``enabled`` is hard-coded ``True`` because :func:`run_transparency` returns
    before reaching here when the feature is off. Passing the config value
    through would be worse than redundant: bmlib answers a disabled
    ``analyze()`` with an UNKNOWN placeholder, and storing one both reads as a
    finding and satisfies the "no row yet" half of the candidate query, so the
    paper would never be analysed once the feature was switched on.

    ``industry_funding_triggers_downgrade`` and
    ``missing_coi_triggers_downgrade`` keep bmlib's defaults deliberately. They
    are not only about the tier downgrade this stage ignores — they feed
    ``calculate_risk_level()``, so they are what makes an industry-funded paper
    with restricted data read HIGH rather than MEDIUM. ``filtering_enabled``
    stays false because this caller does not filter, and the settings object
    should not claim otherwise.

    Args:
        config: Application config.

    Returns:
        Settings for a :class:`~bmlib.transparency.TransparencyAnalyzer`.
    """
    return TransparencySettings(
        enabled=True,
        score_threshold=config.transparency.score_threshold,
        max_concurrent_analyses=config.transparency.concurrency,
    )


def _build_analyzer(config: AppConfig) -> TransparencyAnalyzer:
    """Construct the analyzer, reusing whatever contact details config holds.

    The PubMed API key is read from the source options rather than duplicated
    into ``[transparency]``: it is the same NCBI credential the PubMed fetcher
    already takes, and sending it moves bmlib's ``efetch`` traffic out of the
    per-IP rate bucket that bmnews's own E-utilities requests compete for.
    """
    pubmed_options = config.sources.source_options.get("pubmed", {})
    return TransparencyAnalyzer(
        email=config.user.email or DEFAULT_CONTACT_EMAIL,
        pubmed_api_key=pubmed_options.get("api_key") or None,
        settings=build_settings(config),
    )


def run_transparency(
    config: AppConfig,
    *,
    refresh: bool = False,
    paper_id: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> TransparencyReport:
    """Analyse the transparency of scored papers that still want a result.

    Args:
        config: Application config.
        refresh: Re-analyse papers that already hold a determinate result, and
            restart their retry budget. Successive refresh runs work through
            the corpus least-recently-analysed first, so one run per
            ``limit`` papers covers it rather than redoing the same batch.
        paper_id: Restrict to one publication, ignoring the score gate.
        limit: Batch size for this run, overriding
            :data:`TRANSPARENCY_BATCH_SIZE` in **either** direction. That
            constant paces outbound requests, so raising it past the default
            is the caller taking the pacing decision knowingly.
        dry_run: Report the selection and stop — no analyzer is built, no
            request is made, no row is written.
        on_progress: Optional callback receiving a status message string.

    Returns:
        A :class:`TransparencyReport` for the run.
    """
    if not config.transparency.enabled:
        logger.info("Transparency analysis is disabled — skipping")
        return TransparencyReport()

    batch = limit if limit is not None else TRANSPARENCY_BATCH_SIZE

    with closing(open_db(config)) as conn:
        init_db(conn)

        candidates = get_transparency_candidates(
            conn,
            min_combined=config.transparency.min_combined_score,
            limit=batch,
            max_attempts=TRANSPARENCY_MAX_ATTEMPTS,
            refresh=refresh,
            paper_id=paper_id,
        )
        if not candidates:
            logger.info("No papers awaiting transparency analysis")
            return TransparencyReport()

        total = len(candidates)
        # A full batch cannot be told from an exactly-full queue, so say so
        # rather than leaving the run looking complete — as run_score does.
        if paper_id is None and total == batch:
            logger.warning(
                "Analysing the %d highest-scoring papers awaiting transparency; more "
                "may remain. Re-run `bmnews transparency` to continue.",
                batch,
            )

        if dry_run:
            return TransparencyReport(candidates=total)

        if on_progress:
            on_progress(f"Analysing transparency for {total} paper(s)...")

        return _analyze_all(
            conn,
            _build_analyzer(config),
            candidates,
            refresh=refresh,
            concurrency=config.transparency.concurrency,
            on_progress=on_progress,
        )


def _analyze_all(
    conn: Any,
    analyzer: TransparencyAnalyzer,
    candidates: list[dict],
    *,
    refresh: bool,
    concurrency: int,
    on_progress: Callable[[str], None] | None,
) -> TransparencyReport:
    """Analyse every candidate, storing each result as it lands.

    **One analyzer is shared across the pool** on purpose: bmlib's rate-limit
    lock is per-instance and spans every thread using it, so a second analyzer
    would double the request rate against APIs that asked us not to. Its
    reachability flag is thread-local for the matching reason, so concurrent
    analyses cannot contaminate each other's UNKNOWN.

    **Storing happens here, on the calling thread**, never inside a worker: a
    SQLite connection is not safe to touch from another thread. This mirrors
    ``score_papers``, whose progress callback carries the same guarantee.

    A paper this run cannot record is logged and counted in ``failed``, leaving
    no row — so it returns to the queue next run, exactly as an unscoreable
    paper does. That covers the write as well as the analysis: a storage error
    escaping this loop would discard a report describing rows that are already
    committed, and would do it only after the pool's exit had waited out every
    analysis still in flight.

    ``on_progress`` fires once per finished paper whatever the outcome, so the
    count it reports reaches ``total``.

    Only the *first* storage failure of a run is logged with its traceback:
    every paper shares one connection, so a dropped one fails all of them
    identically and a batch of tracebacks would bury the cause rather than
    show it. The rest are logged at WARNING and counted.

    Args:
        conn: DB-API connection, used only from this thread.
        analyzer: The shared analyzer.
        candidates: Rows from :func:`get_transparency_candidates`.
        refresh: Whether this run resets each paper's retry budget.
        concurrency: Worker count. Throughput is capped by bmlib's shared
            request interval regardless, so this hides latency rather than
            multiplying speed.
        on_progress: Optional callback receiving a status message string.

    Returns:
        A :class:`TransparencyReport` for the batch.
    """
    total = len(candidates)
    analyzed = indeterminate = exhausted = failed = done = 0
    storage_failures = 0

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(
                analyzer.analyze,
                str(paper["id"]),
                pmid=paper.get("pmid") or None,
                doi=paper.get("doi") or None,
            ): paper
            for paper in candidates
        }
        for future in as_completed(futures):
            paper = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception:
                logger.exception(
                    "Transparency analysis failed for paper %s — it stays queued",
                    paper["id"],
                )
                failed += 1
            else:
                risk = result.risk_level.value
                # Derived rather than read back: this is exactly what
                # save_transparency is about to write, and a second query per
                # paper to learn it would be pure overhead.
                attempts = 1 if refresh else (paper.get("attempts") or 0) + 1

                try:
                    save_transparency(
                        conn,
                        paper_id=paper["id"],
                        transparency_score=result.transparency_score,
                        risk_level=risk,
                        result_json=json.dumps(result.to_dict()),
                        reset_attempts=refresh,
                    )
                except Exception:
                    # Counted with the analyses that raised, for the same
                    # reason: no row was written, so the paper stays queued.
                    # Letting a lock timeout or a dropped connection escape
                    # here discarded the report for everything already stored
                    # — after the pool had waited out every analysis still
                    # running, several external requests each, to produce it.
                    failed += 1
                    storage_failures += 1
                    # Traceback for the first only. Every paper shares this one
                    # connection, so a dropped one fails all of them the same
                    # way: one traceback names the cause, a batch of them
                    # buries it in the log a user is asked to attach to a bug
                    # report. A raising *analysis* keeps its own traceback
                    # every time — those are independent interactions with
                    # five APIs and need not share a cause.
                    if storage_failures == 1:
                        logger.exception(
                            "Storing the transparency result for paper %s failed — it stays "
                            "queued. Further storage failures this run are logged without "
                            "their traceback.",
                            paper["id"],
                        )
                    else:
                        logger.warning(
                            "Storing the transparency result for paper %s failed too — it "
                            "stays queued",
                            paper["id"],
                        )
                else:
                    analyzed += 1
                    if risk == _UNKNOWN:
                        indeterminate += 1
                        if attempts >= TRANSPARENCY_MAX_ATTEMPTS:
                            exhausted += 1

            # Reported for a failure too. The bar only ever moves forward, so
            # skipping the update here left a run whose last completion failed
            # reporting n-1/n with nothing coming to correct it.
            if on_progress:
                on_progress(f"Analysing transparency {done}/{total}...")

    logger.info(
        "Transparency: %d analysed, %d indeterminate (%d at the attempt ceiling), %d failed",
        analyzed,
        indeterminate,
        exhausted,
        failed,
    )
    return TransparencyReport(
        candidates=total,
        analyzed=analyzed,
        indeterminate=indeterminate,
        exhausted=exhausted,
        failed=failed,
    )


def list_results(config: AppConfig, *, limit: int | None = None) -> list[dict]:
    """Read stored transparency results for reporting, worst risk first.

    ``limit`` is passed through only when the caller sets it. Left unset,
    :func:`~bmnews.db.operations.get_transparency_results` applies its own
    default rather than borrowing :data:`TRANSPARENCY_BATCH_SIZE` — that
    constant paces *outbound* analysis requests and has no bearing on how many
    rows a read-only listing shows; retuning it for rate-limit reasons must
    not silently change what ``--list`` displays.

    Args:
        config: Application config.
        limit: Maximum rows to return.

    Returns:
        Rows as :func:`~bmnews.db.operations.get_transparency_results` returns
        them.
    """
    with closing(open_db(config)) as conn:
        init_db(conn)
        kwargs: dict[str, int] = {} if limit is None else {"limit": limit}
        return get_transparency_results(conn, **kwargs)
