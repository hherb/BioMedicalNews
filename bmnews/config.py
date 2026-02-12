"""Configuration loading and validation."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("~/.bmnews/config.toml").expanduser()


@dataclass
class SqliteConfig:
    path: str = "~/.bmnews/bmnews.db"

    @property
    def resolved_path(self) -> Path:
        return Path(self.path).expanduser()


@dataclass
class PostgresqlConfig:
    host: str = "localhost"
    port: int = 5432
    database: str = "bmnews"
    user: str = "bmnews"
    password: str = ""

    @property
    def url(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@dataclass
class DatabaseConfig:
    sqlite: SqliteConfig = field(default_factory=SqliteConfig)
    postgresql: PostgresqlConfig = field(default_factory=PostgresqlConfig)


@dataclass
class SourcesConfig:
    medrxiv: bool = True
    biorxiv: bool = True
    europepmc: bool = True
    lookback_days: int = 7


@dataclass
class ScoringConfig:
    min_relevance: float = 0.3
    min_quality: float = 0.2
    scorer: str = "keyword"  # "keyword" or "semantic"


@dataclass
class UserConfig:
    name: str = "Researcher"
    email: str = "user@example.com"
    interests: list[str] = field(default_factory=list)


@dataclass
class EmailConfig:
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = ""
    use_tls: bool = True


@dataclass
class AppConfig:
    database_backend: str = "sqlite"
    log_level: str = "INFO"
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    user: UserConfig = field(default_factory=UserConfig)
    email: EmailConfig = field(default_factory=EmailConfig)

    @property
    def database_url(self) -> str:
        if self.database_backend == "postgresql":
            return self.database.postgresql.url
        db_path = self.database.sqlite.resolved_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path}"


def _merge(dataclass_type, data: dict):
    """Create a dataclass instance from a dict, ignoring unknown keys."""
    valid = {f.name for f in dataclass_type.__dataclass_fields__.values()}
    return dataclass_type(**{k: v for k, v in data.items() if k in valid})


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from a TOML file.

    Falls back to defaults if the file doesn't exist.
    """
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        logger.warning("Config file %s not found, using defaults", path)
        return AppConfig()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    general = raw.get("general", {})
    db_raw = raw.get("database", {})
    database = DatabaseConfig(
        sqlite=_merge(SqliteConfig, db_raw.get("sqlite", {})),
        postgresql=_merge(PostgresqlConfig, db_raw.get("postgresql", {})),
    )

    return AppConfig(
        database_backend=general.get("database_backend", "sqlite"),
        log_level=general.get("log_level", "INFO"),
        database=database,
        sources=_merge(SourcesConfig, raw.get("sources", {})),
        scoring=_merge(ScoringConfig, raw.get("scoring", {})),
        user=_merge(UserConfig, raw.get("user", {})),
        email=_merge(EmailConfig, raw.get("email", {})),
    )
