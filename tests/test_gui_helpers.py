"""Tests for abstract formatting helpers."""

from bmnews.gui.helpers import format_abstract_html


class TestFormatAbstractHTML:
    def test_structured_abstract(self):
        text = "Background: Study context.\nMethods: We did X.\nResults: Found Y."
        html = format_abstract_html(text)
        assert "<strong>Background:</strong>" in html
        assert "<strong>Methods:</strong>" in html
        assert "Study context." in html

    def test_plain_abstract(self):
        text = "This is a plain abstract with no sections."
        html = format_abstract_html(text)
        assert "<p>" in html
        assert "plain abstract" in html

    def test_empty(self):
        assert format_abstract_html("") == ""
        assert format_abstract_html(None) == ""

    def test_html_escaping(self):
        text = "We used <5mg dose & measured >10 outcomes."
        html = format_abstract_html(text)
        assert "&lt;5mg" in html
        assert "&amp;" in html

    def test_html_tagged_abstract(self):
        """PubMed abstracts with <h4> section headings are parsed correctly."""
        text = (
            "<h4>Background</h4>Suicide is the second-leading cause."
            "<h4>Methods</h4>Multiple experts reviewed records."
            "<h4>Results</h4>Detection performance improved."
        )
        html = format_abstract_html(text)
        assert "<strong>Background:</strong>" in html
        assert "<strong>Methods:</strong>" in html
        assert "<strong>Results:</strong>" in html
        assert "<h4>" not in html
        assert "Suicide is the second-leading cause." in html

    def test_html_tagged_abstract_mixed_tags(self):
        """Abstracts with <b>, <strong>, etc. headings are also handled."""
        text = "<b>Objective</b>To compare methods.<b>Results</b>We found improvements."
        html = format_abstract_html(text)
        assert "<strong>Objective:</strong>" in html
        assert "<strong>Results:</strong>" in html
        assert "<b>" not in html

    def test_html_tagged_abstract_preserves_non_section_text(self):
        """Non-section HTML tags are stripped but text content preserved."""
        text = "<h4>Background</h4>We used <i>in vitro</i> assays."
        html = format_abstract_html(text)
        assert "<strong>Background:</strong>" in html
        assert "in vitro" in html
        assert "<i>" not in html
