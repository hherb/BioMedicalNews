"""Template helpers for the GUI."""

from __future__ import annotations

import re
from html import escape

# Section heading tags used by PubMed/bioRxiv abstracts (e.g. <h4>Background</h4>)
_HTML_HEADING_RE = re.compile(
    r"<(h[1-6]|b|strong|i|em)>(.*?)</\1>",
    re.IGNORECASE,
)

# Proper HTML tags for stripping (requires a letter after <, avoids matching "<5mg")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\b[^>]*>")

# --- bmlib >= 0.8.0 Markdown abstracts ------------------------------------
# PubMed abstracts arrive as Markdown rather than HTML: sections as
# ``**LABEL:** text`` separated by a blank line, inline markup as
# ``**strong**`` / ``*em*`` / ``^sup^`` / ``~sub~``, and prose taken from the
# document backslash-escaped over ``\ ` * ~ ^``.
_MD_ESCAPED_RE = re.compile(r"\\([\\`*~^])")

# A delimiter must hug non-whitespace, which is CommonMark's flanking rule and
# exactly what bmlib's emitter guarantees — it moves a run's edge whitespace
# *outside* the markers for this reason. Without it the conversion corrupts the
# sources that are not Markdown at all: medRxiv's "~50 to ~60 patients" pairs
# into a subscript, and a pair of significance asterisks italicises the clause
# between them. Sub/sup go further and forbid inner whitespace, since they
# render a single token (``CO~2~``, ``m^2^``) and never a phrase.
# Strong precedes emphasis, or ``**x**`` matches as an empty ``<em>``.
_MD_INLINE_RULES = (
    (re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"\*(?!\s)(.+?)(?<!\s)\*"), r"<em>\1</em>"),
    (re.compile(r"~([^\s~]+)~"), r"<sub>\1</sub>"),
    (re.compile(r"\^([^\s^]+)\^"), r"<sup>\1</sup>"),
)

# Common section labels in structured abstracts
_SECTION_LABELS = (
    "Background|Objective|Purpose|Introduction|Methods|Study Design|"
    "Setting|Participants|Interventions|Main Outcome Measures|"
    "Results|Findings|Conclusions?|Discussion|Significance|"
    "Context|Design|Measurements|Limitations|Interpretation"
)
_SECTION_PATTERN = re.compile(
    rf"^({_SECTION_LABELS})\s*:",
    re.IGNORECASE | re.MULTILINE,
)


def _normalise_abstract(text: str) -> str:
    """Convert HTML-tagged abstracts into plain-text with section labels.

    PubMed abstracts often arrive with ``<h4>Background</h4>`` style headings.
    This converts them into ``Background:`` so the downstream formatter can
    detect structured sections.  All remaining HTML tags are stripped and the
    text is then HTML-escaped for safe rendering.
    """

    def _heading_to_label(m: re.Match) -> str:
        """Turn a recognised heading tag into a plain ``Label:`` line."""
        label = m.group(2).strip()
        # Only convert if the heading text is a known section label
        if re.match(rf"^({_SECTION_LABELS})$", label, re.IGNORECASE):
            return f"\n{label}:"
        return label

    text = _HTML_HEADING_RE.sub(_heading_to_label, text)
    text = _HTML_TAG_RE.sub("", text)
    return text


def _markdown_to_html(text: str) -> str:
    """Convert bmlib's Markdown subset to the tags the reading pane speaks.

    Runs on already-HTML-escaped text: ``escape()`` leaves ``\\ ` * ~ ^``
    alone, so the markers survive it intact and the tags emitted here are the
    only unescaped HTML in the result.

    Backslash-escaped specials are held aside first. They are prose the
    document actually carried — the star alleles of ``CYP2C19 (\\*1, \\*2)`` —
    so letting them pair into a marker would italicise exactly the values
    bmlib escaped them to protect.

    NUL is the sentinel those go behind, so any already in the text is dropped
    first: it is not displayable either way, and left in place a source
    carrying one — JSON permits ``\\u0000``, unlike the XML PubMed arrives as —
    could be read back as a sentinel and index out of the list.
    """
    text = text.replace("\x00", "")
    held: list[str] = []

    def _hold(match: re.Match) -> str:
        held.append(match.group(1))
        return f"\x00{len(held) - 1}\x00"

    text = _MD_ESCAPED_RE.sub(_hold, text)
    for pattern, replacement in _MD_INLINE_RULES:
        text = pattern.sub(replacement, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], text)


def format_abstract_html(text: str | None) -> str:
    """Format abstract text as HTML with structured section labels bolded."""
    if not text:
        return ""

    normalised = _normalise_abstract(text)
    escaped = _markdown_to_html(escape(normalised))

    # Try structured abstract (has labeled sections)
    parts = _SECTION_PATTERN.split(escaped)
    if len(parts) > 1:
        # parts alternates: [pre-label text, label1, text1, label2, text2, ...]
        html_parts = []
        if parts[0].strip():
            html_parts.append(f"<p>{parts[0].strip()}</p>")
        for i in range(1, len(parts), 2):
            label = parts[i]
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            html_parts.append(f"<p><strong>{label}:</strong> {content}</p>")
        return "\n".join(html_parts)

    # Plain abstract — just wrap in <p>
    paragraphs = [p.strip() for p in escaped.split("\n") if p.strip()]
    return "\n".join(f"<p>{p}</p>" for p in paragraphs)
