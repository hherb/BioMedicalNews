"""Where bmnews's Jinja2 templates come from.

Its own module rather than a corner of :mod:`bmnews.pipeline` because three
unrelated things need it — the digest, the notification stage and the GUI's
template editor — and routing them all through the pipeline made the GUI import
the orchestrator to learn a directory path, and the notification stage import
the module that calls it.
"""

from __future__ import annotations

from pathlib import Path

from bmlib.templates import TemplateEngine

from bmnews.config import AppConfig

#: The templates packaged with bmnews, used when the user has overridden none.
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def build_template_engine(config: AppConfig) -> TemplateEngine:
    """Build a TemplateEngine from config, with package defaults as fallback.

    Args:
        config: Application config; ``template_dir`` names a directory whose
            templates take precedence over the packaged ones.

    Returns:
        An engine resolving user overrides first, then package defaults.
    """
    user_dir = Path(config.template_dir).expanduser() if config.template_dir else None
    return TemplateEngine(user_dir=user_dir, default_dir=TEMPLATES_DIR)
