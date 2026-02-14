"""Settings and template editor routes."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, render_template, request

from bmnews.config import AppConfig
from bmnews.pipeline import TEMPLATES_DIR

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings")
def settings_page():
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]
    templates = sorted(TEMPLATES_DIR.glob("*.*"))
    template_names = [t.name for t in templates]
    return render_template("fragments/settings.html", config=config, template_names=template_names)


@settings_bp.route("/settings/save", methods=["POST"])
def save_settings():
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]

    for key, value in request.form.items():
        parts = key.split(".", 1)
        if len(parts) == 2:
            section_name, field_name = parts
            section = getattr(config, section_name, None)
            if section is not None and hasattr(section, field_name):
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

    if not current_app.config.get("TESTING"):
        from bmnews.config import save_config
        save_config(config)

    return '<div class="flash success">Settings saved.</div>'


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
