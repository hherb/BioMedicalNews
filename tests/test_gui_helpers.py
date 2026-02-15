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
