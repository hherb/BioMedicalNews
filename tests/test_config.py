"""Tests for bmnews.config."""

from __future__ import annotations

from bmnews.config import AppConfig, load_config, save_config, write_default_config


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
        assert config.user.research_interests == "genomics, CRISPR"

    def test_load_string_interests(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("""\
[user]
research_interests = "I am interested in AI in medicine and emergency care."
""")
        config = load_config(cfg)
        assert config.user.research_interests == (
            "I am interested in AI in medicine and emergency care."
        )

    def test_load_legacy_list_interests(self, tmp_path):
        """Old TOML files with list format should be migrated to string."""
        cfg = tmp_path / "config.toml"
        cfg.write_text("""\
[user]
research_interests = ["genomics", "CRISPR"]
""")
        config = load_config(cfg)
        assert isinstance(config.user.research_interests, str)
        assert "genomics" in config.user.research_interests
        assert "CRISPR" in config.user.research_interests


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


class TestSaveConfig:
    """Regression tests for TOML serialization in save_config."""

    def test_roundtrips_special_characters(self, tmp_path):
        """Quotes, backslashes and newlines must survive a save/load cycle.

        Unescaped free text used to produce a file tomllib could not parse,
        destroying the user's whole configuration on the next load.
        """
        cfg = AppConfig()
        cfg.user.research_interests = 'Oncology "immunotherapy"\nand CAR-T\\cells'
        cfg.user.name = "Dr. O'Brien\ttab"
        cfg.email.smtp_password = 'p@ss"word\\'

        path = save_config(cfg, tmp_path / "config.toml")
        loaded = load_config(path)

        assert loaded.user.research_interests == cfg.user.research_interests
        assert loaded.user.name == cfg.user.name
        assert loaded.email.smtp_password == cfg.email.smtp_password

    def test_preserves_source_options(self, tmp_path):
        """Per-source options are a dict and used to be dropped on save."""
        cfg = AppConfig()
        cfg.sources.enabled = ["europepmc", "openalex"]
        cfg.sources.source_options = {
            "europepmc": {"query": 'CANCER AND (SRC:"MED")'},
            "openalex": {"email": "user@example.org"},
        }

        path = save_config(cfg, tmp_path / "config.toml")
        loaded = load_config(path)

        assert loaded.sources.source_options == cfg.sources.source_options
        assert loaded.sources.europepmc_query == 'CANCER AND (SRC:"MED")'

    def test_preserves_scalars_and_lists(self, tmp_path):
        cfg = AppConfig()
        cfg.sources.enabled = ["medrxiv", "biorxiv"]
        cfg.sources.lookback_days = 3
        cfg.scoring.min_combined = 0.55
        cfg.email.enabled = True

        loaded = load_config(save_config(cfg, tmp_path / "config.toml"))

        assert loaded.sources.enabled == ["medrxiv", "biorxiv"]
        assert loaded.sources.lookback_days == 3
        assert loaded.scoring.min_combined == 0.55
        assert loaded.email.enabled is True
