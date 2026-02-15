"""TOML-based configuration for bmnews.

Loads settings from a TOML file (default ``~/.bmnews/config.toml``) and
provides typed dataclass access to all configuration sections.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path("~/.bmnews").expanduser()
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"


@dataclass
class DatabaseConfig:
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
    enabled: list[str] = field(default_factory=lambda: ["medrxiv", "europepmc"])
    lookback_days: int = 7
    source_options: dict[str, dict[str, str]] = field(default_factory=dict)

    # -- Backward-compat properties for old TOML configs with per-source booleans --

    @property
    def medrxiv(self) -> bool:
        return "medrxiv" in self.enabled

    @medrxiv.setter
    def medrxiv(self, value: bool) -> None:
        if value and "medrxiv" not in self.enabled:
            self.enabled.append("medrxiv")
        elif not value and "medrxiv" in self.enabled:
            self.enabled.remove("medrxiv")

    @property
    def biorxiv(self) -> bool:
        return "biorxiv" in self.enabled

    @biorxiv.setter
    def biorxiv(self, value: bool) -> None:
        if value and "biorxiv" not in self.enabled:
            self.enabled.append("biorxiv")
        elif not value and "biorxiv" in self.enabled:
            self.enabled.remove("biorxiv")

    @property
    def europepmc(self) -> bool:
        return "europepmc" in self.enabled

    @europepmc.setter
    def europepmc(self, value: bool) -> None:
        if value and "europepmc" not in self.enabled:
            self.enabled.append("europepmc")
        elif not value and "europepmc" in self.enabled:
            self.enabled.remove("europepmc")

    @property
    def pubmed(self) -> bool:
        return "pubmed" in self.enabled

    @pubmed.setter
    def pubmed(self, value: bool) -> None:
        if value and "pubmed" not in self.enabled:
            self.enabled.append("pubmed")
        elif not value and "pubmed" in self.enabled:
            self.enabled.remove("pubmed")

    @property
    def openalex(self) -> bool:
        return "openalex" in self.enabled

    @openalex.setter
    def openalex(self, value: bool) -> None:
        if value and "openalex" not in self.enabled:
            self.enabled.append("openalex")
        elif not value and "openalex" in self.enabled:
            self.enabled.remove("openalex")

    @property
    def europepmc_query(self) -> str:
        return self.source_options.get("europepmc", {}).get("query", "")

    @europepmc_query.setter
    def europepmc_query(self, value: str) -> None:
        self.source_options.setdefault("europepmc", {})["query"] = value


@dataclass
class LLMConfig:
    provider: str = "ollama"
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    ollama_host: str = ""
    anthropic_api_key: str = ""
    concurrency: int = 1


@dataclass
class ScoringConfig:
    min_relevance: float = 0.5
    min_combined: float = 0.4


@dataclass
class QualityConfig:
    enabled: bool = True
    default_tier: int = 2
    max_tier: int = 3
    min_quality_tier: str = "TIER_1_ANECDOTAL"


@dataclass
class TransparencyConfig:
    enabled: bool = False
    min_score_threshold: float = 0.6


@dataclass
class UserConfig:
    name: str = ""
    email: str = ""
    research_interests: str = ""


@dataclass
class EmailConfig:
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
class AppConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    transparency: TransparencyConfig = field(default_factory=TransparencyConfig)
    user: UserConfig = field(default_factory=UserConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
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


def save_config(config: AppConfig, path: str | Path | None = None) -> Path:
    """Write current config values back to TOML file."""
    if path is None:
        path = DEFAULT_CONFIG_PATH
    path = Path(path).expanduser()

    lines = []
    lines.append("[general]")
    lines.append(f'log_level = "{config.log_level}"')
    if config.template_dir:
        lines.append(f'template_dir = "{config.template_dir}"')
    lines.append("")

    def _write_section(name: str, dc) -> None:
        lines.append(f"[{name}]")
        for field_name in dc.__dataclass_fields__:
            value = getattr(dc, field_name)
            if isinstance(value, bool):
                lines.append(f"{field_name} = {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                lines.append(f"{field_name} = {value}")
            elif isinstance(value, list):
                items = ", ".join(f'"{v}"' for v in value)
                lines.append(f"{field_name} = [{items}]")
            elif isinstance(value, dict):
                pass  # dicts are written as sub-tables below
            elif isinstance(value, str):
                lines.append(f'{field_name} = "{value}"')
        lines.append("")

    _write_section("database", config.database)
    _write_section("sources", config.sources)
    _write_section("llm", config.llm)
    _write_section("scoring", config.scoring)
    _write_section("quality", config.quality)
    _write_section("transparency", config.transparency)
    _write_section("user", config.user)
    _write_section("email", config.email)

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
"""
