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
        label = m.group(2).strip()
        # Only convert if the heading text is a known section label
        if re.match(rf"^({_SECTION_LABELS})$", label, re.IGNORECASE):
            return f"\n{label}:"
        return label

    text = _HTML_HEADING_RE.sub(_heading_to_label, text)
    text = _HTML_TAG_RE.sub("", text)
    return text


def format_abstract_html(text: str | None) -> str:
    """Format abstract text as HTML with structured section labels bolded."""
    if not text:
        return ""

    normalised = _normalise_abstract(text)
    escaped = escape(normalised)

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
