"""Settings and template editor routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from bmlib.llm import LLMClient
from bmlib.publications import list_sources
from flask import Blueprint, abort, current_app, render_template, request
from markupsafe import escape

from bmnews.config import DEFAULT_CONFIG_DIR, AppConfig

# Importing the pipeline pulls in bmnews.fetchers, which registers the
# bmnews-supplied sources so they appear in the registry listing below.
from bmnews.pipeline import TEMPLATES_DIR

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__)


def _available_sources() -> list[dict[str, str]]:
    """List every source registered with bmlib, bmnews-supplied ones included."""
    return [
        {"name": desc.name, "display_name": desc.display_name}
        for desc in sorted(list_sources(), key=lambda d: d.display_name.lower())
    ]


@settings_bp.route("/settings")
def settings_page() -> str:
    """Render the settings form with the current configuration.

    Returns:
        The ``settings`` HTMX fragment.
    """
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    templates = sorted(TEMPLATES_DIR.glob("*.*"))
    template_names = [t.name for t in templates]
    available_sources = _available_sources()
    # Pre-fetch model list so the dropdown is populated on first render
    initial_models = _get_model_options(
        config,
        provider=config.llm.provider,
        current=config.llm.model,
    )
    return render_template(
        "fragments/settings.html",
        config=config,
        template_names=template_names,
        available_sources=available_sources,
        initial_model_options=initial_models,
    )


_TRUTHY = ("true", "1", "on", "yes")


def _coerce_form_value(field_type: str, value: str) -> Any:
    """Coerce a form string to the type declared on a config dataclass field.

    Args:
        field_type: The field's annotation, as a string (``from __future__
            import annotations`` means annotations are never evaluated).
        value: The raw form value.

    Returns:
        The coerced value.

    Raises:
        ValueError: If *value* is not valid for a numeric field.
    """
    if "bool" in field_type:
        return value.lower() in _TRUTHY
    if "int" in field_type:
        return int(value)
    if "float" in field_type:
        return float(value)
    if "list" in field_type:
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


@settings_bp.route("/settings/save", methods=["POST"])
def save_settings() -> str:
    """Apply posted settings to the live config and persist them to TOML.

    Form field names use ``section.field`` notation (e.g. ``llm.provider``).
    Invalid numeric values are reported back rather than raising a 500, and
    nothing is persisted unless every field parsed cleanly.

    Returns:
        A flash-message HTML fragment.
    """
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]

    # Handle sources.enabled from multi-value checkboxes. Unchecked boxes are
    # absent from the POST entirely, so a hidden marker distinguishes
    # "deliberately cleared" from "this form never had a sources section" —
    # without it, any partial form post would disable every source.
    if request.form.get("sources.enabled_submitted"):
        config.sources.enabled = [s for s in request.form.getlist("sources.enabled") if s.strip()]

    errors: list[str] = []
    for key, value in request.form.items():
        if key in ("sources.enabled", "sources.enabled_submitted"):
            continue  # already handled above
        section_name, _, field_name = key.partition(".")
        if not field_name:
            continue
        section = getattr(config, section_name, None)
        if section is None or not hasattr(section, field_name):
            continue

        dataclass_fields = getattr(section, "__dataclass_fields__", {})
        if field_name in dataclass_fields:
            field_type = str(dataclass_fields[field_name].type)
            try:
                setattr(section, field_name, _coerce_form_value(field_type, value))
            except ValueError:
                errors.append(f"{key}: {value!r} is not a valid number")
        else:
            # Property setters (e.g. the backward-compat source booleans)
            # expect a real bool, not the raw "on"/"false" string.
            setattr(section, field_name, value.lower() in _TRUTHY)

    if errors:
        detail = escape("; ".join(errors))
        return f'<div class="flash error">Not saved — {detail}</div>'

    if not current_app.config.get("TESTING"):
        from bmnews.config import save_config

        try:
            save_config(config)
        except OSError as e:
            logger.exception("Could not write config file")
            return f'<div class="flash error">Could not save: {escape(str(e))}</div>'

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


def _get_model_options(
    config: AppConfig,
    *,
    provider: str = "ollama",
    current: str = "",
    refresh: bool = False,
) -> str:
    """Build ``<option>`` HTML for a provider's model list.

    Args:
        config: App configuration (for API keys / hosts).
        provider: Provider name.
        current: Currently configured model name (to pre-select).
        refresh: If True, bypass cache and re-fetch from API.

    Returns:
        Concatenated ``<option>`` elements as an HTML string.
    """
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

    # Build <option> elements with current model pre-selected. Both the model
    # ids (from a remote API) and `current` (a query parameter) are escaped —
    # neither is trusted enough to interpolate into markup raw.
    parts: list[str] = []
    found_current = False
    for mid in model_ids:
        selected = ""
        if mid == current:
            selected = " selected"
            found_current = True
        safe = escape(mid)
        parts.append(f'<option value="{safe}"{selected}>{safe}</option>')
    # If current model not in list but is set, add it at top
    if current and not found_current:
        safe = escape(current)
        parts.insert(0, f'<option value="{safe}" selected>{safe}</option>')
    parts.append('<option value="__custom__">Custom...</option>')
    return "".join(parts)


@settings_bp.route("/settings/models")
def list_models() -> str:
    """Return ``<option>`` elements for a provider's model ``<select>``.

    Query params:
        provider: provider name (default ``"ollama"``)
        refresh: ``"1"`` to bypass cache and re-fetch from API
        current: currently configured model name (to pre-select)
    """
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    return _get_model_options(
        config,
        provider=request.args.get("provider", "ollama"),
        current=request.args.get("current", ""),
        refresh=request.args.get("refresh", "") == "1",
    )


def _template_names() -> list[str]:
    """Return the sorted names of the packaged digest and prompt templates."""
    return [t.name for t in sorted(TEMPLATES_DIR.glob("*.*"))]


def _user_template_dir(config: AppConfig) -> Path:
    """Return the directory holding user overrides of packaged templates."""
    if config.template_dir:
        return Path(config.template_dir).expanduser()
    return DEFAULT_CONFIG_DIR / "templates"


def _validate_template_name(name: str) -> str | None:
    """Return *name* if it identifies a packaged template, else None.

    Restricting writes to names that already ship with the package keeps the
    editor from being used to create or overwrite arbitrary files, and rules
    out path components like ``..`` outright.
    """
    return name if name in _template_names() else None


@settings_bp.route("/settings/templates")
def template_list() -> str:
    """Render the template editor with no template selected.

    Returns:
        The ``template_editor`` HTMX fragment.
    """
    return render_template(
        "fragments/template_editor.html",
        template_names=_template_names(),
        content="",
        current="",
    )


@settings_bp.route("/settings/template/<name>")
def template_load(name: str) -> str:
    """Render the template editor loaded with a template's current content.

    The user's override is preferred over the packaged default.

    Args:
        name: File name of a packaged template.

    Returns:
        The ``template_editor`` HTMX fragment.

    Raises:
        werkzeug.exceptions.NotFound: If *name* is not a packaged template.
    """
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    if _validate_template_name(name) is None:
        abort(404)

    override = _user_template_dir(config) / name
    if override.exists():
        content = override.read_text(encoding="utf-8")
    else:
        content = (TEMPLATES_DIR / name).read_text(encoding="utf-8")

    return render_template(
        "fragments/template_editor.html",
        template_names=_template_names(),
        content=content,
        current=name,
    )


@settings_bp.route("/settings/template/<name>", methods=["POST"])
def template_save(name: str) -> str:
    """Save an edited template as a user override.

    Args:
        name: File name of a packaged template.

    Returns:
        A flash-message HTML fragment.

    Raises:
        werkzeug.exceptions.NotFound: If *name* is not a packaged template.
    """
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    if _validate_template_name(name) is None:
        abort(404)

    content = request.form.get("content", "")
    user_dir = _user_template_dir(config)
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / name).write_text(content, encoding="utf-8")
    except OSError as e:
        logger.exception("Could not write template %s", name)
        return f'<div class="flash error">Could not save: {escape(str(e))}</div>'

    return '<div class="flash success">Template saved.</div>'


@settings_bp.route("/settings/template/<name>/reset", methods=["POST"])
def template_reset(name: str) -> str:
    """Delete a user override so the packaged template takes effect again.

    Args:
        name: File name of a packaged template.

    Returns:
        The ``template_editor`` fragment reloaded with the packaged content.

    Raises:
        werkzeug.exceptions.NotFound: If *name* is not a packaged template.
    """
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    if _validate_template_name(name) is None:
        abort(404)

    override = _user_template_dir(config) / name
    if override.exists():
        override.unlink()

    return template_load(name)
