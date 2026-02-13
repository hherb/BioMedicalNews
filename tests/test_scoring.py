"""Tests for bmnews.scoring — unit tests that don't require LLM."""

from __future__ import annotations

import json

from bmlib.quality.data_models import QualityAssessment, QualityTier, StudyDesign

from bmnews.scoring.scorer import _quality_tier_to_score, _extract_pub_types


class TestQualityTierToScore:
    def test_unclassified(self):
        a = QualityAssessment.unclassified()
        score = _quality_tier_to_score(a)
        assert score == 0.3

    def test_rct(self):
        a = QualityAssessment.from_metadata(StudyDesign.RCT)
        score = _quality_tier_to_score(a)
        assert score == 0.8  # DESIGN_TO_SCORE maps RCT → 8.0, /10 = 0.8

    def test_systematic_review(self):
        a = QualityAssessment.from_metadata(StudyDesign.SYSTEMATIC_REVIEW)
        score = _quality_tier_to_score(a)
        assert score == 0.9  # DESIGN_TO_SCORE maps SR → 9.0, /10 = 0.9

    def test_explicit_quality_score(self):
        a = QualityAssessment(
            assessment_tier=3, quality_score=7.5,
            study_design=StudyDesign.COHORT_PROSPECTIVE,
            quality_tier=QualityTier.TIER_3_CONTROLLED,
        )
        score = _quality_tier_to_score(a)
        assert score == 0.75


class TestExtractPubTypes:
    def test_from_metadata_json(self):
        paper = {
            "metadata_json": json.dumps({"pub_type": ["Randomized Controlled Trial"]}),
            "categories": "",
        }
        types = _extract_pub_types(paper)
        assert "Randomized Controlled Trial" in types

    def test_from_categories(self):
        paper = {"metadata_json": "{}", "categories": "Oncology; Clinical Trial"}
        types = _extract_pub_types(paper)
        assert "Oncology" in types
        assert "Clinical Trial" in types

    def test_empty(self):
        paper = {"metadata_json": "{}", "categories": ""}
        types = _extract_pub_types(paper)
        assert types == []
