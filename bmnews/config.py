"""TOML-based configuration for bmnews.

Loads settings from a TOML file (default ``~/.bmnews/config.toml``) and
provides typed dataclass access to all configuration sections.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path("~/.bmnews").expanduser()
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"


def _source_toggle(source_name: str) -> property:
    """Build a bool property that adds/removes *source_name* in ``enabled``.

    Old TOML files configured sources as per-source booleans
    (``medrxiv = true``) rather than the current ``enabled`` list. These
    generated properties keep those files loading, and let the GUI address a
    single source by name.

    Args:
        source_name: The source's registry name.

    Returns:
        A property whose getter reports membership of ``enabled`` and whose
        setter adds or removes the source, preserving order and uniqueness.
    """

    def getter(self: SourcesConfig) -> bool:
        """Report whether this source is currently enabled."""
        return source_name in self.enabled

    def setter(self: SourcesConfig, value: bool) -> None:
        """Add or remove this source from ``enabled``."""
        if value and source_name not in self.enabled:
            self.enabled.append(source_name)
        elif not value and source_name in self.enabled:
            self.enabled.remove(source_name)

    getter.__name__ = source_name
    return property(getter, setter, doc=f"Whether {source_name} is in `enabled`.")


@dataclass
class DatabaseConfig:
    """Database connection settings (SQLite by default, PostgreSQL optional)."""

    backend: str = "sqlite"
    sqlite_path: str = "~/.bmnews/bmnews.db"
    pg_dsn: str = ""
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "bmnews"
    pg_user: str = "bmnews"
    pg_password: str = ""


@dataclass
class SourcesConfig:
    """Which publication sources to fetch from, and how far back."""

    enabled: list[str] = field(default_factory=lambda: ["medrxiv", "europepmc"])
    lookback_days: int = 7
    source_options: dict[str, dict[str, str]] = field(default_factory=dict)

    # -- Backward-compat properties for old TOML configs with per-source booleans --
    medrxiv = _source_toggle("medrxiv")
    biorxiv = _source_toggle("biorxiv")
    europepmc = _source_toggle("europepmc")
    pubmed = _source_toggle("pubmed")
    openalex = _source_toggle("openalex")

    @property
    def europepmc_query(self) -> str:
        """The custom Europe PMC query string, or ``""`` if unset."""
        return self.source_options.get("europepmc", {}).get("query", "")

    @europepmc_query.setter
    def europepmc_query(self, value: str) -> None:
        """Store the custom Europe PMC query in ``source_options``."""
        self.source_options.setdefault("europepmc", {})["query"] = value


@dataclass
class LLMConfig:
    """LLM provider, model selection and generation parameters."""

    provider: str = "ollama"
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    ollama_host: str = ""
    anthropic_api_key: str = ""
    api_key: str = ""
    base_url: str = ""
    concurrency: int = 1


@dataclass
class ScoringConfig:
    """Score thresholds controlling which papers reach the digest."""

    min_relevance: float = 0.5
    min_combined: float = 0.4


@dataclass
class QualityConfig:
    """Controls bmlib's tiered study-quality assessment."""

    enabled: bool = True
    default_tier: int = 2
    max_tier: int = 3
    min_quality_tier: str = "TIER_1_ANECDOTAL"


@dataclass
class TransparencyConfig:
    """Settings for exposing scoring rationale to the user."""

    enabled: bool = False
    min_score_threshold: float = 0.6


@dataclass
class UserConfig:
    """Identity and free-text research interests used for relevance scoring."""

    name: str = ""
    email: str = ""
    research_interests: str = ""


@dataclass
class EmailConfig:
    """SMTP delivery settings for the email digest."""

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    use_tls: bool = True
    from_address: str = ""
    to_address: str = ""
    subject_prefix: str = "[BioMedNews]"
    max_papers: int = 20


@dataclass
class NotificationsConfig:
    """Watches that alert on a matching paper, and where they deliver.

    Both sub-tables are dicts of dicts — ``{name: {field: value}}``, the same
    shape as ``SourcesConfig.source_options`` — and that is a constraint, not a
    preference. :func:`save_config` renders a list by stringifying each
    element, so an array-of-tables (``[[notifications.watch]]``) would
    round-trip as TOML strings of Python dict reprs; and it emits at most three
    table levels, so anything nested deeper is dropped on every GUI save.
    Keying by name keeps both shapes inside what the serializer handles.

    The values stay raw dicts here because :func:`_apply_section` setattrs
    whatever the TOML holds without validating it. ``bmnews.notify.watches``
    parses them into :class:`~bmnews.notify.watches.Watch` and
    :class:`~bmnews.notify.watches.Channel`, where a typo'd criterion is
    reported rather than silently ignored.
    """

    enabled: bool = False
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    watches: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class AppConfig:
    """Top-level application configuration, one attribute per TOML section."""

    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    transparency: TransparencyConfig = field(default_factory=TransparencyConfig)
    user: UserConfig = field(default_factory=UserConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    log_level: str = "INFO"
    template_dir: str = ""


def _apply_section(dc: Any, data: dict) -> None:
    """Apply dict values onto a dataclass, ignoring unknown keys."""
    for key, value in data.items():
        if hasattr(dc, key):
            # Backward compat: old configs have research_interests as a list
            if key == "research_interests" and isinstance(value, list):
                value = ", ".join(value)
            setattr(dc, key, value)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration from a TOML file.

    Falls back to defaults if the file doesn't exist.
    """
    config = AppConfig()

    if path is None:
        path = DEFAULT_CONFIG_PATH
    path = Path(path).expanduser()

    if not path.exists():
        logger.info("Config file not found at %s — using defaults", path)
        return config

    logger.info("Loading config from %s", path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    if "general" in raw:
        if "log_level" in raw["general"]:
            config.log_level = raw["general"]["log_level"]
        if "template_dir" in raw["general"]:
            config.template_dir = raw["general"]["template_dir"]

    section_map = {
        "database": config.database,
        "sources": config.sources,
        "llm": config.llm,
        "scoring": config.scoring,
        "quality": config.quality,
        "transparency": config.transparency,
        "user": config.user,
        "email": config.email,
        "notifications": config.notifications,
    }

    for section_name, dc in section_map.items():
        if section_name in raw:
            _apply_section(dc, raw[section_name])

    return config


def write_default_config(path: str | Path | None = None) -> Path:
    """Write a default config file if one doesn't exist. Returns the path."""
    if path is None:
        path = DEFAULT_CONFIG_PATH
    path = Path(path).expanduser()

    if path.exists():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    logger.info("Created default config: %s", path)
    return path


_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _toml_str(value: str) -> str:
    """Render *value* as a quoted, escaped TOML basic string.

    Free-text fields such as ``research_interests`` routinely contain quotes
    and newlines; emitting them raw produces a file that ``tomllib`` cannot
    parse back, which would silently destroy the user's configuration.
    """
    out = []
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


#: Characters TOML allows in a bare key. Anything else has to be quoted.
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    """Render *key* as a TOML key, quoting it when it cannot stand bare.

    Sub-table names come from the user — a watch is named by its config table,
    and the GUI lets that name be typed. A name carrying a space or a quote
    would emit an unparseable header and a name carrying a dot would emit a
    *valid* one that reads as two nested tables, so ``a.b`` would silently come
    back as ``{"a": {"b": ...}}``. Either way :func:`save_config` destroys the
    configuration it was asked to preserve.

    Bare keys are left bare so existing files keep their shape rather than
    churning every table heading on the next save.
    """
    return key if _BARE_KEY.match(key) else _toml_str(key)


def _toml_value(value: Any) -> str | None:
    """Render a scalar or list config value as TOML, or None if unsupported."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_str(str(v)) for v in value) + "]"
    return None


def save_config(config: AppConfig, path: str | Path | None = None) -> Path:
    """Write current config values back to a TOML file.

    Args:
        config: The configuration to serialize.
        path: Destination file. Defaults to ``~/.bmnews/config.toml``.

    Returns:
        The path that was written.
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH
    path = Path(path).expanduser()

    lines: list[str] = []
    lines.append("[general]")
    lines.append(f"log_level = {_toml_str(config.log_level)}")
    if config.template_dir:
        lines.append(f"template_dir = {_toml_str(config.template_dir)}")
    lines.append("")

    def _write_section(name: str, dc: Any) -> None:
        """Append one ``[name]`` table, plus any nested sub-tables it holds."""
        # Nested tables must be emitted after every scalar key of their parent
        # table, otherwise the remaining keys are parsed into the sub-table.
        sub_tables: list[tuple[str, dict]] = []
        lines.append(f"[{name}]")
        for field_name in dc.__dataclass_fields__:
            value = getattr(dc, field_name)
            if isinstance(value, dict):
                sub_tables.append((field_name, value))
                continue
            rendered = _toml_value(value)
            if rendered is not None:
                lines.append(f"{field_name} = {rendered}")
        lines.append("")

        for field_name, table in sub_tables:
            scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
            nested = {k: v for k, v in table.items() if isinstance(v, dict)}
            if scalars:
                lines.append(f"[{name}.{field_name}]")
                for key, sub_value in scalars.items():
                    rendered = _toml_value(sub_value)
                    if rendered is not None:
                        lines.append(f"{_toml_key(key)} = {rendered}")
                lines.append("")
            for key, sub_table in nested.items():
                lines.append(f"[{name}.{field_name}.{_toml_key(key)}]")
                for sub_key, leaf in sub_table.items():
                    rendered = _toml_value(leaf)
                    if rendered is not None:
                        lines.append(f"{_toml_key(sub_key)} = {rendered}")
                lines.append("")

    _write_section("database", config.database)
    _write_section("sources", config.sources)
    _write_section("llm", config.llm)
    _write_section("scoring", config.scoring)
    _write_section("quality", config.quality)
    _write_section("transparency", config.transparency)
    _write_section("user", config.user)
    _write_section("email", config.email)
    _write_section("notifications", config.notifications)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


DEFAULT_CONFIG_TOML = """\
[general]
log_level = "INFO"
# template_dir = "~/.bmnews/templates"

[database]
backend = "sqlite"
sqlite_path = "~/.bmnews/bmnews.db"
# pg_dsn = ""

[sources]
enabled = ["medrxiv", "europepmc"]
lookback_days = 7
# To enable more sources, add them to the list above:
# enabled = ["medrxiv", "biorxiv", "europepmc", "pubmed", "openalex"]

[llm]
provider = "ollama"
# model = "ollama:medgemma4B_it_q8"
temperature = 0.3
max_tokens = 4096
# ollama_host = "http://localhost:11434"
# anthropic_api_key = ""
# api_key = ""
# base_url = ""
concurrency = 1

[scoring]
min_relevance = 0.5
min_combined = 0.4

[quality]
enabled = true
default_tier = 2
max_tier = 3
min_quality_tier = "TIER_1_ANECDOTAL"

[transparency]
enabled = false
min_score_threshold = 0.6

[user]
name = "Your Name"
email = "your@email.com"
research_interests = "I am interested in clinical trials and oncology research."

[email]
enabled = false
smtp_host = "smtp.gmail.com"
smtp_port = 587
smtp_user = ""
smtp_password = ""
use_tls = true
from_address = ""
to_address = ""
subject_prefix = "[BioMedNews]"
max_papers = 20

[notifications]
enabled = false

# Watches alert you about a matching paper as it is scored, separately from
# the periodic digest — a notified paper still appears in the next digest.
# Each watch AND-combines its criteria; an empty list means "no constraint".
# Uncomment and adapt:
#
# [notifications.channels.mail]
# kind = "email"                      # reuses the [email] SMTP settings above
# to_address = "me@example.org"
#
# [notifications.channels.matrix]
# kind = "matrix"
# homeserver = "https://matrix.example.org"
# access_token = ""
# room = "#bmnews-alerts:example.org"
#
# [notifications.watches.melanoma-trials]
# enabled = true
# min_relevance = 0.8
# min_combined = 0.0
# min_quality_tier = "TIER_4_EXPERIMENTAL"
# tags = ["melanoma", "immunotherapy"]
# keywords = []
# sources = []
# journals = []
# study_designs = []
# channels = ["mail", "matrix"]
# max_per_run = 5
"""
