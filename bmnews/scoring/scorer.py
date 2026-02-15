"""Orchestrates scoring of papers: relevance (LLM) + quality (bmlib).

Separate LLM calls for relevance+summary and quality assessment.
Configurable concurrency: parallel for API providers, sequential for Ollama.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from bmlib.llm import LLMClient
from bmlib.templates import TemplateEngine
from bmlib.quality.data_models import QualityAssessment, QualityFilter
from bmlib.quality.manager import QualityManager

from bmnews.scoring.relevance_agent import RelevanceAgent

logger = logging.getLogger(__name__)


def score_papers(
    papers: list[dict],
    llm: LLMClient,
    model: str,
    template_engine: TemplateEngine,
    interests: str,
    concurrency: int = 1,
    quality_tier: int = 2,
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

    if concurrency <= 1:
        for paper in papers:
            result = _score_single(paper, agent, quality_mgr, quality_filter, interests)
            results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(
                    _score_single, paper, agent, quality_mgr, quality_filter, interests,
                ): paper
                for paper in papers
            }
            for future in as_completed(futures):
                paper = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception:
                    logger.exception("Error scoring paper %s", paper.get("doi", "?"))

    return results


def _build_quality_filter(max_tier: int) -> QualityFilter:
    """Map a max-tier integer to a QualityFilter."""
    if max_tier <= 1:
        return QualityFilter(use_metadata_only=True, use_llm_classification=False, use_detailed_assessment=False)
    if max_tier == 2:
        return QualityFilter(use_metadata_only=False, use_llm_classification=True, use_detailed_assessment=False)
    # max_tier >= 3
    return QualityFilter(use_metadata_only=False, use_llm_classification=True, use_detailed_assessment=True)


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
    study_design = quality_assessment.study_design.value if quality_assessment.study_design else ""
    quality_tier_name = quality_assessment.quality_tier.name if quality_assessment.quality_tier else ""

    logger.debug(
        "Paper %s quality: design=%s tier=%s (assessment_tier=%d, confidence=%.2f)",
        paper_id, study_design, quality_tier_name,
        quality_assessment.assessment_tier, quality_assessment.confidence,
    )

    # --- Combined score (weighted) ---
    combined = 0.6 * relevance_score + 0.4 * quality_score

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
    """Extract publication types from paper metadata."""
    metadata_str = paper.get("metadata_json", "{}")
    try:
        metadata = json.loads(metadata_str) if isinstance(metadata_str, str) else metadata_str
    except (json.JSONDecodeError, TypeError):
        metadata = {}

    pub_types = metadata.get("pub_type", [])
    if isinstance(pub_types, str):
        pub_types = [pub_types]

    # Also check categories
    categories = paper.get("categories", "")
    if categories:
        pub_types.extend(c.strip() for c in categories.split(";") if c.strip())

    return pub_types


def _quality_tier_to_score(assessment: QualityAssessment) -> float:
    """Convert a quality assessment to a 0.0–1.0 score."""
    if assessment.quality_score is not None and assessment.quality_score > 0:
        return min(1.0, assessment.quality_score / 10.0)

    # Map tier to approximate score
    tier_scores = {
        "UNCLASSIFIED": 0.3,
        "TIER_1_ANECDOTAL": 0.3,
        "TIER_2_OBSERVATIONAL": 0.5,
        "TIER_3_CONTROLLED": 0.7,
        "TIER_4_EXPERIMENTAL": 0.85,
        "TIER_5_SYNTHESIS": 0.95,
    }
    tier_name = assessment.quality_tier.name if assessment.quality_tier else "UNCLASSIFIED"
    return tier_scores.get(tier_name, 0.3)
