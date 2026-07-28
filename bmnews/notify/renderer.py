"""Renders a watch's matching papers into the four notification templates.

Two media, two formats each. The digest's templates are deliberately not
reused: they are CSS-heavy, and Matrix's HTML subset supports no CSS at all,
with per-client sanitisation that varies most around tables. So a Matrix
message stays structurally simple — headings, lists and links — while the email
can look like the digest it sits beside.

Templates resolve through :class:`bmlib.templates.TemplateEngine`, so a user
can override any of them from ``~/.bmnews/templates/`` exactly as they can the
digest.
"""

from __future__ import annotations

import logging
from datetime import datetime

from bmlib.templates import TemplateEngine

logger = logging.getLogger(__name__)

#: Media with a template pair, i.e. what ``medium`` may be.
MEDIA = ("email", "matrix")


def notification_subject(watch_name: str, paper_count: int) -> str:
    """Build the bare subject line for a batch.

    No prefix: that is a per-channel setting, so the adapter applies its own.

    Args:
        watch_name: The watch that matched.
        paper_count: How many papers are in this batch.

    Returns:
        A subject such as ``"melanoma-trials: 3 new papers"``.
    """
    plural = "s" if paper_count != 1 else ""
    return f"{watch_name}: {paper_count} new paper{plural}"


def render_notification(
    papers: list[dict],
    *,
    watch_name: str,
    templates: TemplateEngine,
    medium: str,
    fmt: str,
    remaining: int = 0,
) -> str:
    """Render one notification body.

    Args:
        papers: The matching papers, best combined score first.
        watch_name: The watch that matched, named in the message so a reader
            with several watches can tell which one fired.
        templates: The engine resolving user overrides before packaged
            templates.
        medium: ``email`` or ``matrix``.
        fmt: ``html`` or ``text``.
        remaining: How many further matches are still queued for this watch.
            Rendered only when non-zero — the point of paging is that nothing
            was dropped, which is only worth saying when something is left.

    Returns:
        The rendered body.

    Raises:
        ValueError: If *medium* names no template pair.
    """
    if medium not in MEDIA:
        raise ValueError(f"unknown notification medium {medium!r} (known: {', '.join(MEDIA)})")

    suffix = "html" if fmt == "html" else "txt"
    return templates.render(
        f"notify_{medium}.{suffix}",
        papers=papers,
        paper_count=len(papers),
        watch_name=watch_name,
        remaining=remaining,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
