"""Tests for bmnews.config."""

from __future__ import annotations

from pathlib import Path

from bmnews.config import AppConfig, load_config, write_default_config


class TestDefaults:
    def test_default_config_values(self):
        config = AppConfig()
        assert config.database.backend == "sqlite"
        assert config.sources.medrxiv is True
        assert config.llm.provider == "ollama"
        assert config.scoring.min_relevance == 0.5
        assert config.quality.enabled is True
        assert config.email.enabled is False
        assert config.log_level == "INFO"


class TestLoadConfig:
    def test_load_missing_file(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.toml")
        assert config.database.backend == "sqlite"

    def test_load_valid_config(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("""\
[general]
log_level = "DEBUG"

[database]
backend = "postgresql"
pg_host = "db.example.com"

[sources]
medrxiv = false
biorxiv = true
lookback_days = 14

[llm]
provider = "anthropic"
model = "anthropic:claude-3-haiku"
concurrency = 4

[user]
name = "Dr. Test"
email = "test@example.com"
research_interests = ["genomics", "CRISPR"]
""")
        config = load_config(cfg)
        assert config.log_level == "DEBUG"
        assert config.database.backend == "postgresql"
        assert config.database.pg_host == "db.example.com"
        assert config.sources.medrxiv is False
        assert config.sources.biorxiv is True
        assert config.sources.lookback_days == 14
        assert config.llm.provider == "anthropic"
        assert config.llm.concurrency == 4
        assert config.user.name == "Dr. Test"
        assert config.user.research_interests == ["genomics", "CRISPR"]


class TestWriteDefault:
    def test_creates_config_file(self, tmp_path):
        path = write_default_config(tmp_path / "config.toml")
        assert path.exists()
        text = path.read_text()
        assert "[database]" in text
        assert "[llm]" in text

    def test_does_not_overwrite(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("custom")
        write_default_config(cfg)
        assert cfg.read_text() == "custom"
