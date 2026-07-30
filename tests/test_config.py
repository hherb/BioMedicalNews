"""Tests for bmnews.config."""

from __future__ import annotations

import logging
import tomllib

import pytest

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

    def test_preserves_watches_and_channels(self, tmp_path):
        """The serializer traps that dictated the notifications config shape.

        ``_toml_value`` renders a list by stringifying each element, so an
        array-of-tables would come back as TOML strings of Python dict reprs;
        and ``_write_section`` emits three table levels, so anything deeper is
        dropped on every GUI save. Keying by name avoids both — this asserts it
        stays that way.
        """
        cfg = AppConfig()
        cfg.notifications.enabled = True
        cfg.notifications.channels = {
            "mail": {"kind": "email", "to_address": "me@example.org"},
            "matrix": {
                "kind": "matrix",
                "homeserver": "https://matrix.example.org",
                "access_token": "syt_secret",
                "room": "#bmnews-alerts:example.org",
            },
        }
        cfg.notifications.watches = {
            "melanoma-trials": {
                "enabled": True,
                "min_relevance": 0.8,
                "min_quality_tier": "TIER_4_EXPERIMENTAL",
                "tags": ["melanoma", "immunotherapy"],
                "channels": ["matrix", "mail"],
                "max_per_run": 5,
            }
        }

        loaded = load_config(save_config(cfg, tmp_path / "config.toml"))

        assert loaded.notifications.enabled is True
        assert loaded.notifications.channels == cfg.notifications.channels
        assert loaded.notifications.watches == cfg.notifications.watches

    def test_a_saved_watch_still_parses(self, tmp_path):
        """The round-trip has to survive the validating parse, not just tomllib."""
        from bmnews.notify import parse_channels, parse_watches

        cfg = AppConfig()
        cfg.notifications.channels = {"mail": {"kind": "email", "to_address": "me@example.org"}}
        cfg.notifications.watches = {"w": {"min_relevance": 0.8, "channels": ["mail"]}}

        loaded = load_config(save_config(cfg, tmp_path / "config.toml"))

        watches = parse_watches(loaded.notifications.watches)
        channels = parse_channels(loaded.notifications.channels)
        assert watches["w"].min_relevance == 0.8
        assert watches["w"].channels == ("mail",)
        assert channels["mail"].kind == "email"

    @pytest.mark.parametrize(
        "name",
        [
            "melanoma-trials",  # a bare key, emitted unquoted
            "my melanoma watch",  # a space makes the header unparseable
            "sepsis.trials",  # a dot reads as two nested tables
            'he said "hi"',  # quotes have to survive escaping
            "",  # an empty name is not a key at all
        ],
    )
    def test_a_watch_name_survives_the_round_trip_whatever_it_is(self, tmp_path, name):
        """Watch names are user-authored, so the serializer has to quote them.

        Unquoted, a name carrying a space wrote a header ``tomllib`` refuses,
        and a name carrying a dot wrote a *valid* one meaning something else —
        ``a.b`` came back as ``{"a": {"b": ...}}``. Either way saving destroyed
        the configuration it was asked to preserve.
        """
        cfg = AppConfig()
        cfg.notifications.watches = {name: {"min_relevance": 0.8}}

        loaded = load_config(save_config(cfg, tmp_path / "config.toml"))

        assert loaded.notifications.watches == {name: {"min_relevance": 0.8}}

    def test_an_odd_key_inside_a_watch_survives_too(self, tmp_path):
        """TOML permits quoted keys, so a watch table can hold one."""
        cfg = AppConfig()
        cfg.notifications.watches = {"w": {"odd key": 1, "min_relevance": 0.8}}

        loaded = load_config(save_config(cfg, tmp_path / "config.toml"))

        assert loaded.notifications.watches["w"] == {"odd key": 1, "min_relevance": 0.8}

    def test_bare_names_are_left_unquoted(self, tmp_path):
        """Quoting only what needs it keeps existing files from churning."""
        cfg = AppConfig()
        cfg.notifications.watches = {"melanoma-trials": {"min_relevance": 0.8}}

        text = save_config(cfg, tmp_path / "config.toml").read_text()

        assert "[notifications.watches.melanoma-trials]" in text


def test_transparency_defaults():
    config = AppConfig()
    assert config.transparency.enabled is False
    assert config.transparency.min_combined_score == 0.6
    assert config.transparency.score_threshold == 40
    assert config.transparency.concurrency == 3


def test_renamed_min_score_threshold_carries_forward(tmp_path, caplog):
    """The old key's value is the user's; a rename must not revert it."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[transparency]\nenabled = true\nmin_score_threshold = 0.85\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        config = load_config(path)

    assert config.transparency.min_combined_score == 0.85
    assert "min_score_threshold" in caplog.text
    assert "min_combined_score" in caplog.text


def test_new_key_wins_when_both_are_present(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text(
        "[transparency]\nmin_score_threshold = 0.85\nmin_combined_score = 0.4\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        config = load_config(path)

    assert config.transparency.min_combined_score == 0.4
    assert "obsolete" in caplog.text


def test_transparency_round_trips_through_save(tmp_path):
    """save_config must emit the new key, so the old one disappears on save."""
    config = AppConfig()
    config.transparency.enabled = True
    config.transparency.min_combined_score = 0.75
    config.transparency.score_threshold = 55
    config.transparency.concurrency = 2

    path = save_config(config, tmp_path / "config.toml")
    text = path.read_text(encoding="utf-8")
    assert "min_combined_score = 0.75" in text
    assert "min_score_threshold" not in text

    reloaded = load_config(path)
    assert reloaded.transparency.min_combined_score == 0.75
    assert reloaded.transparency.score_threshold == 55
    assert reloaded.transparency.concurrency == 2


def test_default_config_template_parses_with_the_new_key():
    from bmnews.config import DEFAULT_CONFIG_TOML

    raw = tomllib.loads(DEFAULT_CONFIG_TOML)
    assert "min_score_threshold" not in raw["transparency"]
    assert raw["transparency"]["min_combined_score"] == 0.6
    assert raw["transparency"]["score_threshold"] == 40
