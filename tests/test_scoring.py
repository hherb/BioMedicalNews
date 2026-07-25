"""Tests for bmnews.scoring — unit tests that don't require LLM."""

from __future__ import annotations

import json

from bmlib.quality.data_models import QualityAssessment, QualityTier, StudyDesign

from bmnews.scoring.scorer import _extract_pub_types, _quality_tier_to_score


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

    def test_does_not_mutate_caller_metadata(self):
        """extend() used to write categories back into the caller's dict."""
        metadata = {"pub_type": ["Journal Article"]}
        paper = {"metadata_json": metadata, "categories": "Oncology; Review"}

        types = _extract_pub_types(paper)

        assert "Oncology" in types
        assert metadata["pub_type"] == ["Journal Article"]

    def test_malformed_metadata_json(self):
        paper = {"metadata_json": "{not json", "categories": "Oncology"}
        assert _extract_pub_types(paper) == ["Oncology"]

    def test_non_dict_metadata_json(self):
        paper = {"metadata_json": "[1, 2, 3]", "categories": ""}
        assert _extract_pub_types(paper) == []

    def test_scalar_pub_type(self):
        paper = {"metadata_json": json.dumps({"pub_type": "Review"}), "categories": ""}
        assert _extract_pub_types(paper) == ["Review"]


class TestCombinedScoreWeights:
    def test_weights_sum_to_one(self):
        from bmnews.constants import QUALITY_WEIGHT, RELEVANCE_WEIGHT
        assert RELEVANCE_WEIGHT + QUALITY_WEIGHT == 1.0


class TestResolveModelString:
    """The pipeline must tell provider:model apart from ollama model:tag."""

    def _config(self, provider, model):
        from bmnews.config import AppConfig
        cfg = AppConfig()
        cfg.llm.provider = provider
        cfg.llm.model = model
        return cfg

    def test_provider_prefixed_model_is_left_alone(self):
        from bmnews.pipeline import _resolve_model_string
        cfg = self._config("ollama", "anthropic:claude-sonnet-4-5")
        assert _resolve_model_string(cfg) == "anthropic:claude-sonnet-4-5"

    def test_bare_model_with_tag_gets_provider_prefix(self):
        from bmnews.pipeline import _resolve_model_string
        cfg = self._config("ollama", "llama3.1:latest")
        assert _resolve_model_string(cfg) == "ollama:llama3.1:latest"

    def test_bare_model_gets_provider_prefix(self):
        from bmnews.pipeline import _resolve_model_string
        cfg = self._config("openai", "gpt-4o")
        assert _resolve_model_string(cfg) == "openai:gpt-4o"

    def test_empty_model(self):
        from bmnews.pipeline import _resolve_model_string
        assert _resolve_model_string(self._config("ollama", "")) == "ollama:"


class TestRelevanceScoreClamping:
    """RelevanceAgent.score must never raise on a badly behaved model."""

    def _agent(self, payload):
        from unittest.mock import MagicMock

        from bmnews.scoring.relevance_agent import RelevanceAgent

        agent = RelevanceAgent.__new__(RelevanceAgent)
        agent.render_template = MagicMock(return_value="prompt")
        agent.system_msg = MagicMock(side_effect=lambda m: {"role": "system"})
        agent.user_msg = MagicMock(side_effect=lambda m: {"role": "user"})
        agent.chat_json = MagicMock(return_value=payload)
        return agent

    def test_non_numeric_score_falls_back_to_zero(self):
        result = self._agent({"relevance_score": "very high"}).score("T", "A", "I")
        assert result["relevance_score"] == 0.0

    def test_out_of_range_score_is_clamped(self):
        assert self._agent({"relevance_score": 7.5}).score("T", "A", "I")[
            "relevance_score"] == 1.0
        assert self._agent({"relevance_score": -3}).score("T", "A", "I")[
            "relevance_score"] == 0.0

    def test_non_dict_response_yields_fallback(self):
        result = self._agent(["unexpected"]).score("T", "A", "I")
        assert result["relevance_score"] == 0.0
        assert result["matched_tags"] == []

    def test_missing_list_fields_are_normalised(self):
        result = self._agent({"relevance_score": 0.5, "matched_tags": "oncology"}).score(
            "T", "A", "I")
        assert result["matched_tags"] == []
        assert result["key_findings"] == []

    def test_valid_response_passes_through(self):
        result = self._agent({
            "relevance_score": 0.8, "summary": "Good",
            "matched_tags": ["oncology"], "key_findings": ["f1"],
        }).score("T", "A", "I")
        assert result["relevance_score"] == 0.8
        assert result["matched_tags"] == ["oncology"]


class TestTiersBelow:
    """The evidence hierarchy comes from bmlib's QualityTier, not a local copy."""

    def test_default_floor_excludes_nothing(self):
        from bmnews.scoring.scorer import tiers_below
        assert tiers_below("TIER_1_ANECDOTAL") == []

    def test_excludes_weaker_tiers(self):
        from bmnews.scoring.scorer import tiers_below
        excluded = tiers_below("TIER_4_EXPERIMENTAL")
        assert "TIER_1_ANECDOTAL" in excluded
        assert "TIER_3_CONTROLLED" in excluded
        assert "TIER_4_EXPERIMENTAL" not in excluded
        assert "TIER_5_SYNTHESIS" not in excluded

    def test_unclassified_is_never_excluded(self):
        """Unjudged is not the same as judged-and-rejected."""
        from bmnews.scoring.scorer import tiers_below
        assert "UNCLASSIFIED" not in tiers_below("TIER_5_SYNTHESIS")

    def test_blank_and_unknown_names_filter_nothing(self):
        from bmnews.scoring.scorer import tiers_below
        assert tiers_below("") == []
        assert tiers_below("   ") == []
        assert tiers_below("TIER_9_IMAGINARY") == []

    def test_name_is_case_insensitive(self):
        from bmnews.scoring.scorer import tiers_below
        assert tiers_below("tier_4_experimental") == tiers_below("TIER_4_EXPERIMENTAL")


class TestTierScoreTable:
    """QUALITY_TIER_SCORES is derived from bmlib, not hand-maintained."""

    def test_covers_every_classified_tier(self):
        from bmlib.quality import QualityTier

        from bmnews.constants import QUALITY_TIER_SCORES

        for tier in QualityTier:
            if tier is not QualityTier.UNCLASSIFIED:
                assert tier.name in QUALITY_TIER_SCORES

    def test_scores_increase_with_evidence_strength(self):
        from bmlib.quality import QualityTier

        from bmnews.constants import QUALITY_TIER_SCORES

        ranked = [
            QUALITY_TIER_SCORES[t.name]
            for t in sorted(QualityTier, key=lambda t: t.value)
            if t.name in QUALITY_TIER_SCORES and t is not QualityTier.UNCLASSIFIED
        ]
        assert ranked == sorted(ranked)
        assert all(0.0 <= s <= 1.0 for s in ranked)


class _StubAgent:
    """Records the relevance result it was asked to return."""

    def __init__(self, score=0.75):
        self.score_value = score
        self.calls = 0

    def score(self, title, abstract, interests, categories=""):
        self.calls += 1
        return {
            "relevance_score": self.score_value,
            "summary": "A summary.",
            "matched_tags": ["oncology"],
        }


class TestScorePapersQualityToggle:
    def _run(self, **kwargs):
        from unittest.mock import patch

        from bmnews.scoring.scorer import score_papers

        agent = _StubAgent()
        assessment = QualityAssessment.from_metadata(StudyDesign.RCT)
        with patch("bmnews.scoring.scorer.RelevanceAgent", return_value=agent), \
                patch("bmnews.scoring.scorer.QualityManager") as mgr:
            mgr.return_value.assess.return_value = assessment
            results = score_papers(
                papers=[{"id": 1, "title": "T", "abstract": "A"}],
                llm=None, model="ollama:x", template_engine=None,
                interests="oncology", **kwargs,
            )
        return results, mgr

    def test_disabled_quality_skips_the_manager(self):
        results, mgr = self._run(quality_enabled=False)

        mgr.assert_not_called()
        assert results[0]["quality_score"] == 0.0
        assert results[0]["combined_score"] == 0.75
        assert results[0]["quality_tier"] == ""
        assert results[0]["matched_tags"] == ["oncology"]

    def test_enabled_quality_builds_the_manager(self):
        _results, mgr = self._run(quality_enabled=True)
        mgr.assert_called_once()


class TestScorePapersGenerationSettings:
    """config.llm.temperature / max_tokens must reach the bmlib agent."""

    def test_settings_are_passed_to_the_relevance_agent(self):
        from unittest.mock import patch

        from bmnews.scoring.scorer import score_papers

        with patch("bmnews.scoring.scorer.RelevanceAgent") as agent_cls, \
                patch("bmnews.scoring.scorer.QualityManager"):
            agent_cls.return_value = _StubAgent()
            score_papers(
                papers=[], llm=None, model="ollama:x", template_engine=None,
                interests="", temperature=0.9, max_tokens=1234,
            )

        kwargs = agent_cls.call_args.kwargs
        assert kwargs["temperature"] == 0.9
        assert kwargs["max_tokens"] == 1234

    def test_defaults_match_bmlib_base_agent(self):
        import inspect

        from bmlib.agents import BaseAgent

        from bmnews.constants import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE

        params = inspect.signature(BaseAgent.__init__).parameters
        assert params["temperature"].default == DEFAULT_TEMPERATURE
        assert params["max_tokens"].default == DEFAULT_MAX_TOKENS
