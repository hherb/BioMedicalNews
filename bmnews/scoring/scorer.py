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
from bmlib.quality.metadata_filter import classify_from_metadata
from bmlib.quality.data_models import QualityAssessment

from bmnews.scoring.relevance_agent import RelevanceAgent

logger = logging.getLogger(__name__)


def score_papers(
    papers: list[dict],
    llm: LLMClient,
    model: str,
    template_engine: TemplateEngine,
    interests: list[str],
    concurrency: int = 1,
    quality_tier: int = 2,
) -> list[dict]:
    """Score a list of papers for relevance and quality.

    Args:
        papers: List of paper dicts (from db).
        llm: LLM client instance.
        model: Model string (e.g. "ollama:medgemma4B_it_q8").
        template_engine: Template engine for prompt rendering.
        interests: User research interests.
        concurrency: Number of concurrent scoring tasks.
        quality_tier: Max quality assessment tier (1=metadata, 2=classifier, 3=deep).

    Returns:
        List of dicts with scoring results, each containing:
            paper_id, relevance_score, quality_score, combined_score,
            summary, study_design, quality_tier, assessment_json
    """
    agent = RelevanceAgent(llm=llm, model=model, template_engine=template_engine)
    results = []

    if concurrency <= 1:
        for paper in papers:
            result = _score_single(paper, agent, interests, quality_tier)
            results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(_score_single, paper, agent, interests, quality_tier): paper
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


def _score_single(
    paper: dict,
    agent: RelevanceAgent,
    interests: list[str],
    quality_tier: int,
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

    # --- Quality assessment (bmlib.quality) ---
    quality_assessment = _assess_quality(paper, quality_tier)
    quality_score = _quality_tier_to_score(quality_assessment)
    study_design = quality_assessment.study_design.value if quality_assessment.study_design else ""
    quality_tier_name = quality_assessment.quality_tier.name if quality_assessment.quality_tier else ""

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
        "assessment_json": json.dumps({
            "relevance": relevance_result,
            "quality": quality_assessment.to_dict(),
        }),
    }


def _assess_quality(paper: dict, max_tier: int) -> QualityAssessment:
    """Run quality assessment up to the specified tier."""
    # Tier 1: metadata-based classification (always free)
    pub_types = _extract_pub_types(paper)
    assessment = classify_from_metadata(pub_types)

    # Tier 2+ would use LLM-based classification via bmlib.quality.manager
    # but that requires a separate LLM call. For now, Tier 1 metadata
    # classification covers the basic case. Tier 2/3 integration happens
    # via the QualityManager in the pipeline when the user opts in.

    return assessment


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
        "TIER_2_DESCRIPTIVE": 0.5,
        "TIER_3_CONTROLLED": 0.7,
        "TIER_4_EXPERIMENTAL": 0.85,
        "TIER_5_SYNTHESIS": 0.95,
    }
    tier_name = assessment.quality_tier.name if assessment.quality_tier else "UNCLASSIFIED"
    return tier_scores.get(tier_name, 0.3)
