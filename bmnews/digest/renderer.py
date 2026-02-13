"""Renders email digests from scored papers using Jinja2 templates."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from bmlib.templates import TemplateEngine

logger = logging.getLogger(__name__)


def render_digest(
    papers: list[dict],
    template_engine: TemplateEngine,
    subject_prefix: str = "[BioMedNews]",
    fmt: str = "html",
) -> str:
    """Render a digest from scored papers.

    Args:
        papers: List of paper dicts with scoring data.
        template_engine: Template engine instance.
        subject_prefix: Prefix for the digest header.
        fmt: Output format — "html" or "text".

    Returns:
        Rendered digest string.
    """
    template_name = "digest_email.html" if fmt == "html" else "digest_text.txt"

    return template_engine.render(
        template_name,
        papers=papers,
        paper_count=len(papers),
        subject_prefix=subject_prefix,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
