"""Template helpers for the GUI."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from html import escape

from bmnews.markup import is_markdown_source, markdown_to_html

logger = logging.getLogger(__name__)

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

    bioRxiv, medRxiv and Europe PMC abstracts often arrive with
    ``<h4>Background</h4>`` style headings. This converts them into
    ``Background:`` so the downstream formatter can detect structured sections.
    All remaining HTML tags are stripped and the text is then HTML-escaped for
    safe rendering.
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


def format_abstract_html(text: str | None, sources: Iterable[str] | None = None) -> str:
    """Format abstract text as HTML with structured section labels bolded.

    The two shapes an abstract arrives in converge here. bioRxiv, medRxiv and
    Europe PMC send HTML, which :func:`_normalise_abstract` flattens to
    ``Label:`` lines; PubMed sends bmlib Markdown, whose markers become tags —
    but only when *sources* says so.

    The Markdown conversion runs *after* ``escape()``: that call leaves
    ``\\ ` * ~ ^`` alone, so the markers survive it intact and the tags
    :func:`~bmnews.markup.markdown_to_html` emits are the only unescaped HTML
    it introduces. The ``<p>`` and ``<strong>`` wrappers this function adds
    below are the others.

    Args:
        text: The stored abstract, in whichever shape its source sent.
        sources: The paper's ``sources``. Only a source
            :func:`~bmnews.markup.is_markdown_source` accepts has its markers
            converted; every other source's ``*``, ``~`` and ``^`` are content
            and are left as the reader's own text. Omitting the argument
            therefore converts nothing, which is the safe default and what a
            template override predating the gate still gets.

    Returns:
        Escaped HTML: structured abstracts as one
        ``<p><strong>Label:</strong> …</p>`` per section, plain ones as ``<p>``
        per line.
    """
    if not text:
        return ""

    try:
        return _format_abstract_html(text, sources)
    except Exception:
        # The reading pane renders this through ``|safe`` inside
        # ``render_template``, so an exception here is a Flask 500 — and HTMX
        # does not swap on a non-2xx, which leaves the *previous* paper's
        # title, DOI and abstract in the pane while the newly clicked card
        # takes the highlight. Wrong-paper attribution is the one failure this
        # module must not have, so a formatting bug degrades to the escaped
        # text instead. Logged with a traceback, never swallowed.
        logger.exception("Abstract formatting failed; falling back to escaped text")
        return f"<p>{escape(text)}</p>"


def _format_abstract_html(text: str, sources: Iterable[str] | None) -> str:
    """Do the formatting. See :func:`format_abstract_html`, which guards it."""
    normalised = _normalise_abstract(text)
    escaped = escape(normalised)
    if is_markdown_source(sources):
        escaped = markdown_to_html(escaped)

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
