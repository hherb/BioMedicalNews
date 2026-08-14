"""Tests for abstract formatting helpers."""

from html import escape

import pytest

from bmnews.gui.helpers import format_abstract_html
from bmnews.markup import is_markdown_source, markdown_to_text

PUBMED = ["pubmed"]
MEDRXIV = ["medrxiv"]


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

    Every case here passes ``PUBMED``; the conversion does not run otherwise.
    :class:`TestNonMarkdownSourcesAreLeftAlone` is the other half.
    """

    def test_markdown_section_label_becomes_a_heading(self):
        text = "**BACKGROUND:** Study context.\n\n**METHODS:** We did X."
        html = format_abstract_html(text, PUBMED)
        assert "<strong>BACKGROUND:</strong>" in html
        assert "<strong>METHODS:</strong>" in html
        assert "**" not in html

    def test_markdown_label_outside_the_legacy_allowlist_still_headings(self):
        """The label comes from PubMed's own Label/NlmCategory, not a fixed list.

        The assertion that earns the name is the *absence* of double wrapping:
        once ``**DESIGN…:**`` is a ``<strong>``, the line no longer starts with
        the label, so ``_SECTION_PATTERN`` must not also fire and produce a
        nested ``<strong>DESIGN…:</strong>:``.
        """
        text = "**DESIGN, SETTING, AND PARTICIPANTS:** A cohort of 40 adults."
        html = format_abstract_html(text, PUBMED)
        assert "<strong>DESIGN, SETTING, AND PARTICIPANTS:</strong> A cohort of 40 adults." in html
        assert "</strong>:" not in html

    def test_subscript_and_superscript_markers_render_as_tags(self):
        text = "Serum CO~2~ was measured over 12 m^2^ plots."
        html = format_abstract_html(text, PUBMED)
        assert "CO<sub>2</sub>" in html
        assert "m<sup>2</sup>" in html
        assert "~" not in html and "^" not in html

    def test_emphasis_markers_render_as_tags(self):
        text = "The *E. coli* load rose, and **doubled** by day 7."
        html = format_abstract_html(text, PUBMED)
        assert "<em>E. coli</em>" in html
        assert "<strong>doubled</strong>" in html

    def test_triple_asterisk_nests_rather_than_crossing_tags(self):
        """``<b><i>x</i></b>`` reaches us as ``***x***``.

        Without a rule of its own the strong pattern takes the first two
        asterisks and the emphasis pattern the third, emitting the crossed
        ``<strong><em>KRAS</strong></em>`` into a ``|safe`` context.
        """
        html = format_abstract_html("The ***KRAS*** gene drives growth.", PUBMED)
        assert "<strong><em>KRAS</em></strong>" in html
        assert "*" not in html

    def test_escaped_markers_are_literal_text_not_markup(self):
        """``CYP2C19 (\\*1, \\*2, \\*17)`` — star alleles, not emphasis."""
        text = r"Genotyping of CYP2C19 (\*1, \*2, \*17 alleles); AUC \~ 0.80."
        html = format_abstract_html(text, PUBMED)
        assert "(*1, *2, *17 alleles)" in html
        assert "AUC ~ 0.80" in html
        assert "<em>" not in html
        assert "\\" not in html

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (r"CYP2C19 \*1/\*2 genotype", "CYP2C19 *1/*2 genotype"),
            (r"the \*starred\* term", "the *starred* term"),
            (r"CO\~2\~ notation", "CO~2~ notation"),
            (r"a \^caret\^ pair", "a ^caret^ pair"),
        ],
    )
    def test_adjacent_escaped_markers_do_not_pair(self, text, expected):
        """The hold-aside, pinned where the flanking rule cannot stand in for it.

        Each pair here hugs non-whitespace on both sides, so nothing but
        holding the escaped characters out of marker matching prevents them
        pairing. Replacing the hold with a plain unescape passes every other
        test in this class and fails these.
        """
        html = format_abstract_html(text, PUBMED)
        assert expected in html
        for tag in ("<em>", "<strong>", "<sub>", "<sup>"):
            assert tag not in html

    def test_a_nul_byte_in_the_source_does_not_crash_the_pane(self):
        """The escape/restore sentinel is NUL; JSON sources may carry one.

        Carries a real escape too, so the sentinel a source supplies competes
        with one the converter allocates — the index-out-of-range path the NUL
        strip exists to close.
        """
        text = "Counted \x000\x00 times across ~12~ sites in CYP2C19 \\*1."
        html = format_abstract_html(text, PUBMED)
        assert "\x00" not in html
        assert "<sub>12</sub>" in html
        assert "CYP2C19 *1." in html

    def test_markdown_abstract_is_still_html_escaped(self):
        """Third-party metadata stays escaped — only bmlib's markers become tags."""
        text = "**RESULTS:** Dose <5mg & <script>alert(1)</script> outcomes."
        html = format_abstract_html(text, PUBMED)
        assert "&lt;5mg" in html
        assert "&amp;" in html
        assert "<script>" not in html
        # Pins the ordering rather than merely the escaping: converting before
        # escaping would leave this <strong> escaped into visible markup.
        assert "<strong>RESULTS:</strong>" in html


class TestNonMarkdownSourcesAreLeftAlone:
    """Only PubMed prose is Markdown; every other source's ``*``/``~``/``^`` is content.

    bmlib escapes PubMed prose precisely because it declares the field
    Markdown. It escapes nothing for medRxiv, bioRxiv, Europe PMC or OpenAlex,
    so a converter pointed at them invents markup. Each string below is a shape
    that occurs in real abstracts and that the flanking rule does *not* stop —
    they hug non-whitespace on both sides, which is what pairing requires.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "CYP2C19*2 and CYP2C19*17 carriers were compared.",
            "HLA-B*57:01 and HLA-A*31:01 were genotyped.",
            "kcat/KM was 5.9*10(4) and 8.9*10(4) M(-1).",
            "Both 5*-UTR and 3*-UTR regions were sequenced.",
            "Density fell as 4.2^4^10 per cm^2 across sites.",
            "We enrolled ~50-~60 patients per arm.",
            "Doses of ~2~4 mg were used.",
            "Values differed (*P<0.05, **P<0.01) between arms.",
            "Change was significant*, and the effect* persisted.",
        ],
    )
    def test_medrxiv_prose_is_escaped_but_never_converted(self, text):
        html = format_abstract_html(text, MEDRXIV)
        assert escape(text) in html
        for tag in ("<em>", "<strong>", "<sub>", "<sup>"):
            assert tag not in html

    def test_omitting_sources_converts_nothing(self):
        """The safe default, which a template override predating the gate gets."""
        text = "CYP2C19*2 and CYP2C19*17 carriers."
        assert format_abstract_html(text, None) == format_abstract_html(text, MEDRXIV)

    def test_a_paper_pubmed_also_fed_is_treated_as_markdown(self):
        """A merged publication lists every source; the escaped half wins.

        An unconverted PubMed abstract shows backslashes on every escaped line;
        converting a medRxiv one only risks a pairing. Documented in
        :func:`is_markdown_source`, pinned here.
        """
        assert is_markdown_source(["medrxiv", "pubmed"])
        assert not is_markdown_source(["medrxiv", "biorxiv"])
        assert not is_markdown_source([])
        assert not is_markdown_source(None)
        assert is_markdown_source(["PubMed"]), "source names are matched case-insensitively"


class TestMarkdownTitle:
    """Titles are flattened to text, because no surface bmnews has renders tags in one.

    bmlib 0.8.0 made titles Markdown too — the same markers, and the same
    backslash escaping. Left raw they reach the reading pane, the card list,
    both email digests and both Matrix bodies with literal backslashes in them.
    """

    def test_emphasis_markers_are_dropped(self):
        assert markdown_to_text("Effect of *Escherichia coli* on growth") == (
            "Effect of Escherichia coli on growth"
        )

    def test_escaped_specials_survive_as_themselves(self):
        assert markdown_to_text(r"CYP2C19 \*2 carriers at AUC \~ 0.80") == (
            "CYP2C19 *2 carriers at AUC ~ 0.80"
        )

    def test_subscript_flattens_but_superscript_keeps_its_caret(self):
        """``CO~2~`` reads as ``CO2``; ``10^6^`` must not read as ``106``."""
        assert markdown_to_text("Serum CO~2~ at 10^6^ CFU/mL") == "Serum CO2 at 10^6 CFU/mL"


class TestFormattingFailureDegrades:
    """A formatting bug must not become a 500, because HTMX does not swap on one.

    The reading pane renders through ``|safe`` inside ``render_template``, and
    a non-2xx leaves the previously selected paper in the pane while the newly
    clicked card takes the highlight — wrong-paper attribution in a biomedical
    reader.
    """

    def test_a_raising_converter_falls_back_to_escaped_text(self, monkeypatch, caplog):
        import logging

        import bmnews.gui.helpers as helpers

        def _boom(_text):
            raise ValueError("converter bug")

        monkeypatch.setattr(helpers, "markdown_to_html", _boom)
        with caplog.at_level(logging.ERROR):
            html = helpers.format_abstract_html("Dose <5mg in *E. coli*", PUBMED)

        assert "&lt;5mg" in html, "the fallback must still escape"
        assert "<script" not in html
        assert "converter bug" in caplog.text, "the failure is logged, never swallowed"
