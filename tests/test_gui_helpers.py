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


class TestMarkdownAbstract:
    """bmlib >= 0.8.0 delivers PubMed abstracts as Markdown, not HTML.

    Sections arrive as ``**LABEL:** text`` separated by a blank line, inline
    markup as ``**strong**`` / ``*em*`` / ``^sup^`` / ``~sub~``, and prose taken
    from the document is backslash-escaped over ``\\ ` * ~ ^``. Rendered raw,
    the reader saw the markers themselves.
    """

    def test_markdown_section_label_becomes_a_heading(self):
        text = "**BACKGROUND:** Study context.\n\n**METHODS:** We did X."
        html = format_abstract_html(text)
        assert "<strong>BACKGROUND:</strong>" in html
        assert "<strong>METHODS:</strong>" in html
        assert "**" not in html

    def test_markdown_label_outside_the_legacy_allowlist_still_headings(self):
        """The label comes from PubMed's own Label/NlmCategory, not a fixed list."""
        text = "**DESIGN, SETTING, AND PARTICIPANTS:** A cohort of 40 adults."
        html = format_abstract_html(text)
        assert "<strong>DESIGN, SETTING, AND PARTICIPANTS:</strong>" in html
        assert "A cohort of 40 adults." in html

    def test_subscript_and_superscript_markers_render_as_tags(self):
        text = "Serum CO~2~ was measured over 12 m^2^ plots."
        html = format_abstract_html(text)
        assert "CO<sub>2</sub>" in html
        assert "m<sup>2</sup>" in html
        assert "~" not in html and "^" not in html

    def test_emphasis_markers_render_as_tags(self):
        text = "The *E. coli* load rose, and **doubled** by day 7."
        html = format_abstract_html(text)
        assert "<em>E. coli</em>" in html
        assert "<strong>doubled</strong>" in html

    def test_escaped_markers_are_literal_text_not_markup(self):
        """``CYP2C19 (\\*1, \\*2, \\*17)`` — star alleles, not emphasis."""
        text = r"Genotyping of CYP2C19 (\*1, \*2, \*17 alleles); AUC \~ 0.80."
        html = format_abstract_html(text)
        assert "(*1, *2, *17 alleles)" in html
        assert "AUC ~ 0.80" in html
        assert "<em>" not in html
        assert "\\" not in html

    def test_approximately_tildes_are_not_a_subscript(self):
        """Only PubMed abstracts are Markdown; bioRxiv uses ``~`` for "about"."""
        text = "We enrolled ~50 to ~60 patients per arm."
        html = format_abstract_html(text)
        assert "~50 to ~60" in html
        assert "<sub>" not in html

    def test_a_trailing_significance_asterisk_is_not_emphasis(self):
        """A delimiter must hug non-whitespace, as bmlib's emitter guarantees."""
        text = "Change was significant* and sustained* over 12 weeks."
        html = format_abstract_html(text)
        assert "significant* and sustained*" in html
        assert "<em>" not in html

    def test_a_nul_byte_in_the_source_does_not_crash_the_pane(self):
        """The escape/restore sentinel is NUL; JSON sources may carry one."""
        text = "Sections were counted \x000\x00 times across ~12~ sites."
        html = format_abstract_html(text)
        assert "Sections were counted" in html
        assert "<sub>12</sub>" in html

    def test_markdown_abstract_is_still_html_escaped(self):
        """Third-party metadata stays escaped — only bmlib's markers become tags."""
        text = "**RESULTS:** Dose <5mg & <script>alert(1)</script> outcomes."
        html = format_abstract_html(text)
        assert "&lt;5mg" in html
        assert "&amp;" in html
        assert "<script>" not in html
