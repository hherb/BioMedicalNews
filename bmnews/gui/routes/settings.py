"""Settings and template editor routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import Blueprint, current_app, render_template, request

from bmlib.llm import LLMClient
from bmlib.publications.fetchers import list_sources as bmlib_list_sources
from bmlib.publications.models import SourceDescriptor

from bmnews.config import AppConfig
from bmnews.pipeline import TEMPLATES_DIR, _LOCAL_SOURCES

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__)


def _available_sources() -> list[dict[str, str]]:
    """Build a list of all available sources (bmlib registry + local)."""
    sources = []
    for desc in bmlib_list_sources():
        sources.append({"name": desc.name, "display_name": desc.display_name})
    for name, display_name in _LOCAL_SOURCES.items():
        sources.append({"name": name, "display_name": display_name})
    return sources


@settings_bp.route("/settings")
def settings_page():
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    templates = sorted(TEMPLATES_DIR.glob("*.*"))
    template_names = [t.name for t in templates]
    available_sources = _available_sources()
    return render_template(
        "fragments/settings.html",
        config=config,
        template_names=template_names,
        available_sources=available_sources,
    )


@settings_bp.route("/settings/save", methods=["POST"])
def save_settings():
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]

    # Handle sources.enabled from multi-value checkboxes
    enabled_sources = request.form.getlist("sources.enabled")
    if enabled_sources is not None:
        config.sources.enabled = enabled_sources

    for key, value in request.form.items():
        if key == "sources.enabled":
            continue  # already handled above
        parts = key.split(".", 1)
        if len(parts) == 2:
            section_name, field_name = parts
            section = getattr(config, section_name, None)
            if section is not None and hasattr(section, field_name):
                if field_name in getattr(section, "__dataclass_fields__", {}):
                    field = section.__dataclass_fields__[field_name]
                    ftype = str(field.type)
                    if "bool" in ftype:
                        setattr(section, field_name, value.lower() in ("true", "1", "on", "yes"))
                    elif "int" in ftype:
                        setattr(section, field_name, int(value))
                    elif "float" in ftype:
                        setattr(section, field_name, float(value))
                    elif "list" in ftype:
                        setattr(section, field_name, [v.strip() for v in value.split(",") if v.strip()])
                    else:
                        setattr(section, field_name, value)
                else:
                    # Handle property setters (e.g. backward-compat booleans)
                    setattr(section, field_name, value)

    if not current_app.config.get("TESTING"):
        from bmnews.config import save_config
        save_config(config)

    return '<div class="flash success">Settings saved.</div>'


# ---------------------------------------------------------------------------
# Model list endpoint (auto-populate model selector)
# ---------------------------------------------------------------------------

_MODEL_CACHE_PATH = Path("~/.bmnews/model_cache.json").expanduser()


def _load_model_cache() -> dict[str, list[str]]:
    """Load cached model lists from disk."""
    if _MODEL_CACHE_PATH.exists():
        try:
            return json.loads(_MODEL_CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_model_cache(cache: dict[str, list[str]]) -> None:
    """Persist model cache to disk."""
    _MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MODEL_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


@settings_bp.route("/settings/models")
def list_models():
    """Return ``<option>`` elements for a provider's model ``<select>``.

    Query params:
        provider: provider name (default ``"ollama"``)
        refresh: ``"1"`` to bypass cache and re-fetch from API
        current: currently configured model name (to pre-select)
    """
    provider = request.args.get("provider", "ollama")
    refresh = request.args.get("refresh", "") == "1"
    current = request.args.get("current", "")
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]

    if not current:
        current = config.llm.model

    cache = _load_model_cache()

    if not refresh and provider in cache:
        model_ids = cache[provider]
    else:
        try:
            client = LLMClient(
                default_provider=provider,
                ollama_host=config.llm.ollama_host or None,
                anthropic_api_key=config.llm.anthropic_api_key or None,
                api_key=config.llm.api_key or None,
                base_url=config.llm.base_url or None,
            )
            raw = client.list_models(provider=provider)
            model_ids = [m if isinstance(m, str) else "" for m in raw]
            model_ids = [m for m in model_ids if m]
        except Exception as e:
            logger.warning("Failed to list models for %s: %s", provider, e)
            model_ids = []

        if model_ids:
            cache[provider] = model_ids
            _save_model_cache(cache)

    # Build <option> elements with current model pre-selected
    parts: list[str] = []
    found_current = False
    for mid in model_ids:
        selected = ""
        if mid == current:
            selected = " selected"
            found_current = True
        parts.append(f'<option value="{mid}"{selected}>{mid}</option>')
    # If current model not in list but is set, add it at top
    if current and not found_current:
        parts.insert(0, f'<option value="{current}" selected>{current}</option>')
    parts.append('<option value="__custom__">Custom...</option>')
    return "".join(parts)


@settings_bp.route("/settings/templates")
def template_list():
    templates = sorted(TEMPLATES_DIR.glob("*.*"))
    names = [t.name for t in templates]
    return render_template("fragments/template_editor.html", template_names=names, content="", current="")


@settings_bp.route("/settings/template/<name>")
def template_load(name: str):
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    user_dir = Path(config.template_dir).expanduser() if config.template_dir else None

    if user_dir and (user_dir / name).exists():
        content = (user_dir / name).read_text(encoding="utf-8")
    elif (TEMPLATES_DIR / name).exists():
        content = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    else:
        content = ""

    templates = sorted(TEMPLATES_DIR.glob("*.*"))
    names = [t.name for t in templates]
    return render_template("fragments/template_editor.html",
                           template_names=names, content=content, current=name)


@settings_bp.route("/settings/template/<name>", methods=["POST"])
def template_save(name: str):
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    content = request.form.get("content", "")

    user_dir = Path(config.template_dir).expanduser() if config.template_dir else Path("~/.bmnews/templates").expanduser()
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / name).write_text(content, encoding="utf-8")

    return '<div class="flash success">Template saved.</div>'


@settings_bp.route("/settings/template/<name>/reset", methods=["POST"])
def template_reset(name: str):
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    user_dir = Path(config.template_dir).expanduser() if config.template_dir else Path("~/.bmnews/templates").expanduser()

    override = user_dir / name
    if override.exists():
        override.unlink()

    return template_load(name)
