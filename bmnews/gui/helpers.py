"""Template helpers for the GUI."""

from __future__ import annotations

import re
from html import escape

# Common section labels in structured abstracts
_SECTION_PATTERN = re.compile(
    r"^(Background|Objective|Purpose|Introduction|Methods|Study Design|"
    r"Setting|Participants|Interventions|Main Outcome Measures|"
    r"Results|Findings|Conclusions?|Discussion|Significance|"
    r"Context|Design|Measurements|Limitations|Interpretation)\s*:",
    re.IGNORECASE | re.MULTILINE,
)


def format_abstract_html(text: str | None) -> str:
    """Format abstract text as HTML with structured section labels bolded."""
    if not text:
        return ""

    escaped = escape(text)

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
