"""Tests for the scoring modules."""

from __future__ import annotations

from datetime import date

from bmnews.fetchers.base import FetchedPaper
from bmnews.scoring.quality import score_quality
from bmnews.scoring.relevance import KeywordRelevanceScorer


def _make_paper(**kwargs) -> FetchedPaper:
    defaults = dict(
        doi="10.1101/test",
        title="Test Paper",
        authors=["Alice", "Bob"],
        abstract="",
        url="https://example.com",
        source="medrxiv",
        published_date=date.today(),
    )
    defaults.update(kwargs)
    return FetchedPaper(**defaults)


class TestKeywordRelevanceScorer:
    def setup_method(self):
        self.scorer = KeywordRelevanceScorer()

    def test_exact_title_match(self):
        paper = _make_paper(title="Machine learning in healthcare outcomes")
        score = self.scorer.score(paper, ["machine learning in healthcare"])
        assert score > 0.5

    def test_exact_abstract_match(self):
        paper = _make_paper(
            title="A new study",
            abstract="This paper explores clinical trials methodology in depth.",
        )
        score = self.scorer.score(paper, ["clinical trials methodology"])
        assert score > 0.2

    def test_no_match(self):
        paper = _make_paper(
            title="Quantum entanglement in photonic systems",
            abstract="We present a novel approach to quantum computing.",
        )
        score = self.scorer.score(paper, ["epidemiology", "genomics"])
        assert score < 0.1

    def test_partial_token_match(self):
        paper = _make_paper(
            title="Genomic analysis of cancer biomarkers",
            abstract="We analysed genomic data from multiple cohorts.",
        )
        score = self.scorer.score(paper, ["genomics", "cancer biomarkers"])
        assert score > 0.2

    def test_empty_interests(self):
        paper = _make_paper(title="Any paper")
        assert self.scorer.score(paper, []) == 0.0

    def test_compute_embedding_returns_none(self):
        assert self.scorer.compute_embedding("text") is None


class TestQualityScoring:
    def test_well_structured_abstract(self):
        abstract = (
            "Background: Cardiovascular disease remains the leading cause of death. "
            "Methods: We conducted a randomized controlled trial with 500 participants. "
            "Results: The intervention group showed a hazard ratio of 0.75 (95% CI 0.60-0.93, p<0.01). "
            "Conclusions: The treatment significantly reduced mortality."
        )
        paper = _make_paper(abstract=abstract, authors=["A", "B", "C", "D", "E"])
        score = score_quality(paper)
        assert score > 0.6

    def test_minimal_abstract(self):
        paper = _make_paper(abstract="A short note.", authors=["A"])
        score = score_quality(paper)
        assert score < 0.4

    def test_no_abstract(self):
        paper = _make_paper(abstract="")
        score = score_quality(paper)
        assert score <= 0.1

    def test_methodology_keywords_boost(self):
        abstract = (
            "This systematic review and meta-analysis included 50 studies. "
            "We performed sensitivity analysis and subgroup analysis. "
            "Cox proportional hazards regression was used."
        )
        paper = _make_paper(abstract=abstract, authors=["A", "B", "C"])
        score = score_quality(paper)
        assert score > 0.3
