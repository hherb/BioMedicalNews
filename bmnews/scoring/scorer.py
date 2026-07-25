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
from bmlib.quality.data_models import QualityAssessment, QualityFilter
from bmlib.quality.manager import QualityManager
from bmlib.templates import TemplateEngine

from bmnews.constants import (
    DEFAULT_QUALITY_SCORE,
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
    quality_tier: int = QUALITY_TIER_LLM_CLASSIFIER,
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
        quality_tier: Max quality assessment tier (1=metadata, 2=classifier, 3=deep).
        progress_callback: Optional ``callback(current, total, result)`` invoked
            after each paper. It is always called on the calling thread — even
            when *concurrency* > 1 — so callbacks may safely touch a database
            connection that is not shared across threads.

    Returns:
        List of dicts with scoring results, each containing:
            paper_id, relevance_score, quality_score, combined_score,
            summary, study_design, quality_tier, matched_tags, assessment_json
    """
    agent = RelevanceAgent(llm=llm, model=model, template_engine=template_engine)
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
            result = _score_single(paper, agent, quality_mgr, quality_filter, interests)
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
    quality_mgr: QualityManager,
    quality_filter: QualityFilter,
    interests: str,
) -> dict:
    """Score a single paper: relevance (LLM) + quality (metadata/LLM)."""
    paper_id = paper.get("id", 0)
    title = paper.get("title", "")
    abstract = paper.get("abstract", "")
    categories = paper.get("categories", "")

    # --- Relevance scoring (LLM) ---
    relevance_result = agent.score(
        title=title,
        abstract=abstract,
        interests=interests,
        categories=categories,
    )
    relevance_score = relevance_result.get("relevance_score", 0.0)
    summary = relevance_result.get("summary", "")

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

    Args:
        paper: Paper dict, optionally carrying a ``metadata_json`` blob with a
            ``pub_type`` entry and a semicolon-separated ``categories`` string.

    Returns:
        A new list combining metadata publication types and categories.
    """
    metadata_str = paper.get("metadata_json", "{}")
    try:
        metadata = json.loads(metadata_str) if isinstance(metadata_str, str) else metadata_str
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    raw_types = metadata.get("pub_type", [])
    if isinstance(raw_types, str):
        pub_types = [raw_types]
    elif isinstance(raw_types, list):
        # Copy: extending in place would mutate the caller's metadata dict.
        pub_types = list(raw_types)
    else:
        pub_types = []

    # Also check categories
    categories = paper.get("categories", "")
    if categories:
        pub_types.extend(c.strip() for c in categories.split(";") if c.strip())

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
