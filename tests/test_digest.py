"""Tests for bmnews.digest rendering."""

from __future__ import annotations

from pathlib import Path

from bmlib.templates import TemplateEngine

from bmnews.digest.renderer import render_digest

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _sample_papers():
    return [
        {
            "title": "Effect of Drug X on Condition Y",
            "url": "https://doi.org/10.1101/test1",
            "authors": "Smith J; Jones A",
            "published_date": "2024-01-15",
            "source": "medrxiv",
            "summary": "This study found that Drug X significantly reduces Condition Y symptoms.",
            "relevance_score": 0.85,
            "quality_tier": "TIER_4_EXPERIMENTAL",
            "study_design": "RCT",
        },
        {
            "title": "A Review of Treatment Approaches",
            "url": "https://doi.org/10.1101/test2",
            "authors": "Brown B",
            "published_date": "2024-01-16",
            "source": "europepmc",
            "summary": "Comprehensive review of treatment modalities.",
            "relevance_score": 0.72,
            "quality_tier": "TIER_5_SYNTHESIS",
            "study_design": "SYSTEMATIC_REVIEW",
        },
    ]


class TestRenderDigest:
    def test_html_render(self):
        engine = TemplateEngine(default_dir=TEMPLATES_DIR)
        html = render_digest(_sample_papers(), engine, fmt="html")
        assert "Effect of Drug X" in html
        assert "https://doi.org/10.1101/test1" in html
        assert "85%" in html
        assert "2 new publications" in html

    def test_text_render(self):
        engine = TemplateEngine(default_dir=TEMPLATES_DIR)
        text = render_digest(_sample_papers(), engine, fmt="text")
        assert "Effect of Drug X" in text
        assert "https://doi.org/10.1101/test1" in text
        assert "85%" in text

    def test_empty_papers(self):
        engine = TemplateEngine(default_dir=TEMPLATES_DIR)
        html = render_digest([], engine, fmt="html")
        assert "0 new publications" in html

    def test_custom_prefix(self):
        engine = TemplateEngine(default_dir=TEMPLATES_DIR)
        text = render_digest(_sample_papers(), engine, subject_prefix="[Custom]", fmt="text")
        assert "[Custom]" in text
