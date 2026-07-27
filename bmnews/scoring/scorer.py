"""Orchestrates scoring of papers: relevance (LLM) + quality (bmlib).

Separate LLM calls for relevance+summary and quality assessment.
Configurable concurrency: parallel for API providers, sequential for Ollama.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from bmlib.llm import LLMClient
from bmlib.quality import QualityAssessment, QualityFilter, QualityManager, QualityTier
from bmlib.templates import TemplateEngine

from bmnews.constants import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_QUALITY_SCORE,
    DEFAULT_TEMPERATURE,
    QUALITY_SCORE_SCALE,
    QUALITY_TIER_LLM_CLASSIFIER,
    QUALITY_TIER_METADATA_ONLY,
    QUALITY_TIER_SCORES,
    QUALITY_WEIGHT,
    RELEVANCE_WEIGHT,
)
from bmnews.scoring.relevance_agent import RelevanceAgent

logger = logging.getLogger(__name__)


def score_papers(
    papers: list[dict],
    llm: LLMClient,
    model: str,
    template_engine: TemplateEngine,
    interests: str,
    concurrency: int = 1,
    quality_enabled: bool = True,
    quality_tier: int = QUALITY_TIER_LLM_CLASSIFIER,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    progress_callback: Callable[[int, int, dict], None] | None = None,
) -> list[dict]:
    """Score a list of papers for relevance and quality.

    Args:
        papers: List of paper dicts (from db).
        llm: LLM client instance.
        model: Model string (e.g. "ollama:medgemma4B_it_q8").
        template_engine: Template engine for prompt rendering.
        interests: Free-text description of user research interests.
        concurrency: Number of concurrent scoring tasks.
        quality_enabled: When False, quality assessment is skipped entirely
            and the combined score is the relevance score alone.
        quality_tier: Max quality assessment tier (1=metadata, 2=classifier, 3=deep).
        temperature: Sampling temperature for the relevance agent.
        max_tokens: Output token ceiling for the relevance agent.
        progress_callback: Optional ``callback(current, total, result)`` invoked
            after each paper. It is always called on the calling thread — even
            when *concurrency* > 1 — so callbacks may safely touch a database
            connection that is not shared across threads.

    Returns:
        List of dicts with scoring results, each containing:
            paper_id, relevance_score, quality_score, combined_score,
            summary, study_design, quality_tier, matched_tags, assessment_json
    """
    agent = RelevanceAgent(
        llm=llm,
        model=model,
        template_engine=template_engine,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    quality_mgr: QualityManager | None = None
    quality_filter: QualityFilter | None = None
    if quality_enabled:
        quality_mgr = QualityManager(
            llm=llm,
            classifier_model=model,
            assessor_model=model,
            template_engine=template_engine,
        )
        quality_filter = _build_quality_filter(quality_tier)
    results = []
    total = len(papers)

    if concurrency <= 1:
        for i, paper in enumerate(papers):
            # A paper that cannot be scored costs only itself. This loop used
            # to let the exception escape, so a handful of unscoreable papers
            # stranded every paper queued behind them — and it is the branch
            # local-Ollama users run. The concurrent branch below already
            # logged and carried on; the two now agree.
            try:
                result = _score_single(paper, agent, quality_mgr, quality_filter, interests)
            except Exception:
                logger.exception("Error scoring paper %s", paper.get("doi", "?"))
                continue
            results.append(result)
            if progress_callback:
                progress_callback(i + 1, total, result)
    else:
        completed = 0
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(
                    _score_single, paper, agent, quality_mgr, quality_filter, interests,
                ): paper
                for paper in papers
            }
            for future in as_completed(futures):
                paper = futures[future]
                # Count every finished paper, including failures, so progress
                # still reaches total/total when some papers error out.
                completed += 1
                try:
                    result = future.result()
                except Exception:
                    logger.exception("Error scoring paper %s", paper.get("doi", "?"))
                    continue
                results.append(result)
                if progress_callback:
                    progress_callback(completed, total, result)

    return results


def tiers_below(min_tier: str) -> list[str]:
    """Names of the quality tiers ranked below *min_tier*.

    The evidence hierarchy is bmlib's :class:`~bmlib.quality.QualityTier`, so
    the ordering is never restated here.  ``UNCLASSIFIED`` is deliberately
    excluded from the result: a paper the pipeline could not classify is
    unjudged, not judged-and-rejected, and dropping it would silently hide
    every paper whose design the classifier did not recognise.

    Args:
        min_tier: Tier name from config, e.g. ``"TIER_3_CONTROLLED"``.

    Returns:
        Tier names to filter out, or ``[]`` when *min_tier* is blank, the
        weakest tier, or not a tier bmlib knows.
    """
    if not min_tier or not min_tier.strip():
        return []
    try:
        floor = QualityTier[min_tier.strip().upper()]
    except KeyError:
        logger.warning("Unknown min_quality_tier %r — not filtering by tier", min_tier)
        return []
    return [
        tier.name
        for tier in QualityTier
        if tier < floor and tier is not QualityTier.UNCLASSIFIED
    ]


def _build_quality_filter(max_tier: int) -> QualityFilter:
    """Map a max-tier integer to the matching :class:`QualityFilter`.

    Args:
        max_tier: 1 = metadata only, 2 = add the LLM classifier,
            3 or more = also run the deep assessment.

    Returns:
        A filter enabling every assessment stage up to *max_tier*.
    """
    if max_tier <= QUALITY_TIER_METADATA_ONLY:
        return QualityFilter(
            use_metadata_only=True,
            use_llm_classification=False,
            use_detailed_assessment=False,
        )
    if max_tier == QUALITY_TIER_LLM_CLASSIFIER:
        return QualityFilter(
            use_metadata_only=False,
            use_llm_classification=True,
            use_detailed_assessment=False,
        )
    return QualityFilter(
        use_metadata_only=False,
        use_llm_classification=True,
        use_detailed_assessment=True,
    )


def _score_single(
    paper: dict,
    agent: RelevanceAgent,
    quality_mgr: QualityManager | None,
    quality_filter: QualityFilter | None,
    interests: str,
) -> dict:
    """Score a single paper: relevance (LLM) + quality (metadata/LLM).

    When *quality_mgr* is None, quality assessment is disabled and the
    combined score is the relevance score alone.
    """
    paper_id = paper.get("id", 0)
    title = paper.get("title", "")
    # ``get(key, "")`` is not a guard when the key exists holding None, which
    # a NULL abstract does: the default never applies and the None reaches the
    # quality tiers, which slice it. ``db.operations._row_to_paper`` normalises
    # the column, but this takes a plain dict from any caller, so it cannot
    # assume the row came through there.
    abstract = paper.get("abstract") or ""
    # The prompt template renders keywords as one line, so the list is joined
    # here rather than teaching the template to format it.
    categories = "; ".join(paper.get("keywords") or [])

    # --- Relevance scoring (LLM) ---
    relevance_result = agent.score(
        title=title,
        abstract=abstract,
        interests=interests,
        categories=categories,
    )
    relevance_score = relevance_result.get("relevance_score", 0.0)
    summary = relevance_result.get("summary", "")

    if quality_mgr is None:
        return {
            "paper_id": paper_id,
            "relevance_score": relevance_score,
            "quality_score": 0.0,
            "combined_score": relevance_score,
            "summary": summary,
            "study_design": "",
            "quality_tier": "",
            "matched_tags": relevance_result.get("matched_tags", []),
            "assessment_json": json.dumps({"relevance": relevance_result}),
        }

    # --- Quality assessment (bmlib.quality tiered pipeline) ---
    pub_types = _extract_pub_types(paper)
    logger.debug("Paper %s pub_types for classification: %s", paper_id, pub_types)
    quality_assessment = quality_mgr.assess(
        title=title,
        abstract=abstract,
        publication_types=pub_types,
        filter_settings=quality_filter,
    )
    quality_score = _quality_tier_to_score(quality_assessment)
    study_design = (
        quality_assessment.study_design.value if quality_assessment.study_design else ""
    )
    quality_tier_name = (
        quality_assessment.quality_tier.name if quality_assessment.quality_tier else ""
    )

    logger.debug(
        "Paper %s quality: design=%s tier=%s (assessment_tier=%d, confidence=%.2f)",
        paper_id, study_design, quality_tier_name,
        quality_assessment.assessment_tier, quality_assessment.confidence,
    )

    # --- Combined score (weighted) ---
    combined = RELEVANCE_WEIGHT * relevance_score + QUALITY_WEIGHT * quality_score

    return {
        "paper_id": paper_id,
        "relevance_score": relevance_score,
        "quality_score": quality_score,
        "combined_score": combined,
        "summary": summary,
        "study_design": study_design,
        "quality_tier": quality_tier_name,
        "matched_tags": relevance_result.get("matched_tags", []),
        "assessment_json": json.dumps({
            "relevance": relevance_result,
            "quality": quality_assessment.to_dict(),
        }),
    }


def _extract_pub_types(paper: dict) -> list[str]:
    """Collect publication-type hints for a paper.

    These feed bmlib's free Tier-1 metadata classification, so dropping them
    silently forces every paper onto the LLM classifier instead.

    Args:
        paper: Paper dict carrying ``publication_types`` and ``keywords``
            lists, as decoded by ``db.operations._row_to_paper``.

    Returns:
        A new list combining publication types and subject keywords.
    """
    # Copy: extending in place would mutate the caller's paper dict.
    pub_types = list(paper.get("publication_types") or [])
    pub_types.extend(paper.get("keywords") or [])
    return pub_types


def _quality_tier_to_score(assessment: QualityAssessment) -> float:
    """Convert a quality assessment to a 0.0–1.0 score.

    Prefers bmlib's explicit 0–10 ``quality_score`` when present, otherwise
    falls back to the approximate score for the assessed tier.

    Args:
        assessment: The assessment returned by bmlib's QualityManager.

    Returns:
        A score in the range 0.0–1.0.
    """
    if assessment.quality_score is not None and assessment.quality_score > 0:
        return min(1.0, assessment.quality_score / QUALITY_SCORE_SCALE)

    tier_name = assessment.quality_tier.name if assessment.quality_tier else "UNCLASSIFIED"
    return QUALITY_TIER_SCORES.get(tier_name, DEFAULT_QUALITY_SCORE)
