"""Tests for watch parsing and the pure criteria matcher.

No database, no SMTP, no LLM: watches parse from literal dicts and the matcher
runs against literal paper dicts, which is the property that makes the criteria
engine cheap to extend.
"""

from __future__ import annotations

import logging

import pytest

from bmnews.constants import DEFAULT_NOTIFY_MAX_PER_RUN
from bmnews.notify.matcher import matches
from bmnews.notify.watches import (
    Channel,
    Watch,
    WatchConfigError,
    parse_channels,
    parse_watches,
    resolve_channels,
)


def _paper(**overrides):
    """A scored paper dict in the shape ``_row_to_paper`` produces."""
    paper = {
        "id": 1,
        "title": "Adjuvant immunotherapy in melanoma",
        "abstract": "A randomised trial of checkpoint blockade.",
        "sources": ["medrxiv"],
        "journal": "The Lancet",
        "relevance_score": 0.9,
        "quality_score": 0.8,
        "combined_score": 0.86,
        "study_design": "rct",
        "quality_tier": "TIER_4_EXPERIMENTAL",
        "tags": ["melanoma", "oncology"],
    }
    paper.update(overrides)
    return paper


def _watch(**criteria):
    """A watch built straight from criteria, bypassing the config parse."""
    return Watch(name="w", **criteria)


# ---------------------------------------------------------------------------
# Matching — one criterion at a time
# ---------------------------------------------------------------------------


class TestMatcherDefaults:
    def test_a_watch_with_no_criteria_matches_everything(self):
        assert matches(_paper(), _watch())

    def test_enabled_is_not_a_criterion(self):
        """Whether to evaluate a watch is the caller's call, not the matcher's."""
        assert matches(_paper(), _watch(enabled=False))


class TestScoreFloors:
    def test_relevance_floor_admits_and_excludes(self):
        assert matches(_paper(relevance_score=0.9), _watch(min_relevance=0.8))
        assert not matches(_paper(relevance_score=0.7), _watch(min_relevance=0.8))

    def test_the_floor_is_inclusive(self):
        assert matches(_paper(relevance_score=0.8), _watch(min_relevance=0.8))

    def test_combined_floor_is_applied_independently(self):
        paper = _paper(relevance_score=0.9, combined_score=0.3)
        assert not matches(paper, _watch(min_combined=0.5))
        assert matches(paper, _watch(min_relevance=0.5))

    def test_an_unscored_paper_fails_a_floor_rather_than_raising(self):
        paper = _paper(relevance_score=None, combined_score=None)
        assert not matches(paper, _watch(min_relevance=0.1))
        assert matches(paper, _watch())


class TestQualityTierFloor:
    def test_a_paper_at_the_floor_matches(self):
        paper = _paper(quality_tier="TIER_3_CONTROLLED")
        assert matches(paper, _watch(min_quality_tier="TIER_3_CONTROLLED"))

    def test_a_paper_below_the_floor_is_excluded(self):
        paper = _paper(quality_tier="TIER_1_ANECDOTAL")
        assert not matches(paper, _watch(min_quality_tier="TIER_3_CONTROLLED"))

    def test_a_paper_above_the_floor_matches(self):
        paper = _paper(quality_tier="TIER_5_SYNTHESIS")
        assert matches(paper, _watch(min_quality_tier="TIER_3_CONTROLLED"))

    def test_unclassified_survives_a_floor(self):
        """Unjudged is not judged-and-rejected — the rule the digest applies."""
        paper = _paper(quality_tier="UNCLASSIFIED")
        assert matches(paper, _watch(min_quality_tier="TIER_4_EXPERIMENTAL"))

    def test_a_paper_with_no_tier_at_all_survives_a_floor(self):
        assert matches(_paper(quality_tier=""), _watch(min_quality_tier="TIER_4_EXPERIMENTAL"))


class TestTags:
    def test_any_listed_tag_qualifies(self):
        assert matches(_paper(tags=["melanoma"]), _watch(tags=["melanoma", "glioma"]))

    def test_no_overlap_excludes(self):
        assert not matches(_paper(tags=["cardiology"]), _watch(tags=["melanoma"]))

    def test_a_paper_without_tags_fails_a_tag_criterion(self):
        paper = _paper()
        del paper["tags"]
        assert not matches(paper, _watch(tags=["melanoma"]))
        assert matches(paper, _watch())

    def test_comparison_ignores_case_and_surrounding_space(self):
        assert matches(_paper(tags=[" Melanoma "]), _watch(tags=["melanoma"]))


class TestKeywords:
    def test_a_keyword_matches_the_title(self):
        assert matches(_paper(), _watch(keywords=["adjuvant"]))

    def test_a_keyword_matches_the_abstract(self):
        assert matches(_paper(), _watch(keywords=["checkpoint"]))

    def test_matching_is_case_insensitive(self):
        assert matches(_paper(), _watch(keywords=["IMMUNOTHERAPY"]))

    def test_it_is_a_substring_not_a_word(self):
        """The user's own search terms: "melanoma" should find "melanomas"."""
        assert matches(_paper(title="Melanomas of the skin"), _watch(keywords=["melanoma"]))

    def test_any_keyword_is_enough(self):
        assert matches(_paper(), _watch(keywords=["nothing-here", "melanoma"]))

    def test_no_keyword_matching_excludes(self):
        assert not matches(_paper(), _watch(keywords=["nephrology"]))

    def test_missing_text_does_not_raise(self):
        assert not matches(_paper(title=None, abstract=None), _watch(keywords=["melanoma"]))


class TestSourcesJournalAndDesign:
    def test_source_matches_inside_the_list(self):
        paper = _paper(sources=["medrxiv", "pubmed"])
        assert matches(paper, _watch(sources=["pubmed"]))
        assert not matches(paper, _watch(sources=["biorxiv"]))

    def test_journal_is_compared_case_insensitively(self):
        assert matches(_paper(journal="the lancet"), _watch(journals=["The Lancet"]))
        assert not matches(_paper(journal="Nature"), _watch(journals=["The Lancet"]))

    def test_study_design_matches_bmlibs_value_spelling(self):
        assert matches(_paper(study_design="rct"), _watch(study_designs=["rct"]))
        assert not matches(_paper(study_design="case_series"), _watch(study_designs=["rct"]))


class TestCriteriaCombine:
    def test_criteria_are_anded_not_ored(self):
        watch = _watch(min_relevance=0.8, tags=["melanoma"], sources=["medrxiv"])
        assert matches(_paper(), watch)
        # Each one alone is enough to reject.
        assert not matches(_paper(relevance_score=0.1), watch)
        assert not matches(_paper(tags=["cardiology"]), watch)
        assert not matches(_paper(sources=["pubmed"]), watch)


# ---------------------------------------------------------------------------
# Parsing watches and channels from config dicts
# ---------------------------------------------------------------------------


class TestWatchParsing:
    def test_a_full_watch_round_trips_from_config(self):
        watch = Watch.from_config(
            "melanoma-trials",
            {
                "enabled": True,
                "min_relevance": 0.8,
                "min_combined": 0.5,
                "min_quality_tier": "TIER_4_EXPERIMENTAL",
                "tags": ["melanoma", "immunotherapy"],
                "keywords": ["checkpoint"],
                "sources": ["medrxiv"],
                "journals": ["The Lancet"],
                "study_designs": ["rct"],
                "channels": ["matrix", "mail"],
                "max_per_run": 10,
            },
        )
        assert watch.name == "melanoma-trials"
        assert watch.min_relevance == 0.8
        assert watch.min_quality_tier == "TIER_4_EXPERIMENTAL"
        assert watch.tags == ("melanoma", "immunotherapy")
        assert watch.channels == ("matrix", "mail")
        assert watch.max_per_run == 10

    def test_an_empty_watch_takes_defaults(self):
        watch = Watch.from_config("bare", {})
        assert watch.enabled is True
        assert watch.min_relevance == 0.0
        assert watch.min_quality_tier == ""
        assert watch.tags == ()
        assert watch.max_per_run == DEFAULT_NOTIFY_MAX_PER_RUN

    def test_a_tier_name_is_normalised(self):
        watch = Watch.from_config("w", {"min_quality_tier": " tier_3_controlled "})
        assert watch.min_quality_tier == "TIER_3_CONTROLLED"

    def test_a_bare_string_becomes_a_one_item_list(self):
        assert Watch.from_config("w", {"tags": "melanoma"}).tags == ("melanoma",)

    def test_blank_list_entries_are_dropped(self):
        assert Watch.from_config("w", {"tags": ["melanoma", "", "  "]}).tags == ("melanoma",)

    def test_an_unknown_criterion_is_warned_about_by_name(self, caplog):
        """A misspelled criterion is a criterion not applied — say so loudly."""
        with caplog.at_level(logging.WARNING):
            Watch.from_config("w", {"min_relevence": 0.8})
        assert "min_relevence" in caplog.text

    def test_a_known_criterion_produces_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            Watch.from_config("w", {"min_relevance": 0.8})
        assert caplog.text == ""

    @pytest.mark.parametrize(
        "data",
        [
            {"min_relevance": "high"},
            {"min_relevance": 1.5},
            {"min_relevance": -0.1},
            {"min_combined": "0.5x"},
            {"min_quality_tier": "TIER_9_IMAGINARY"},
            {"study_designs": ["randomised_controlled_trial"]},
            {"max_per_run": 0},
            {"max_per_run": "lots"},
        ],
    )
    def test_a_value_that_cannot_mean_anything_is_rejected(self, data):
        with pytest.raises(WatchConfigError):
            Watch.from_config("w", data)


class TestChannelParsing:
    def test_settings_are_kept_and_kind_is_not(self):
        channel = Channel.from_config(
            "matrix",
            {"kind": "matrix", "homeserver": "https://m.example.org", "room": "#a:example.org"},
        )
        assert channel.kind == "matrix"
        assert channel.settings == {
            "homeserver": "https://m.example.org",
            "room": "#a:example.org",
        }

    def test_a_missing_kind_is_rejected(self):
        with pytest.raises(WatchConfigError):
            Channel.from_config("c", {"to_address": "me@example.org"})

    def test_a_kind_with_no_adapter_is_rejected(self):
        """A typo here would accept papers and deliver none of them."""
        with pytest.raises(WatchConfigError):
            Channel.from_config("c", {"kind": "carrier-pigeon"})

    def test_an_unknown_setting_is_warned_about(self, caplog):
        with caplog.at_level(logging.WARNING):
            Channel.from_config("c", {"kind": "email", "to_adress": "me@example.org"})
        assert "to_adress" in caplog.text


class TestParsingCollections:
    def test_one_broken_watch_does_not_take_down_the_others(self, caplog):
        with caplog.at_level(logging.ERROR):
            watches = parse_watches(
                {
                    "good": {"min_relevance": 0.8},
                    "broken": {"min_quality_tier": "TIER_9_IMAGINARY"},
                    "also-good": {"tags": ["melanoma"]},
                }
            )
        assert set(watches) == {"good", "also-good"}
        assert "broken" in caplog.text

    def test_disabled_watches_are_still_returned(self):
        """Acting on one is the caller's decision; --list has to show them."""
        watches = parse_watches({"off": {"enabled": False}})
        assert watches["off"].enabled is False

    def test_a_non_table_entry_is_reported_not_crashed_on(self, caplog):
        with caplog.at_level(logging.ERROR):
            assert parse_watches({"oops": "not-a-table"}) == {}
        assert "oops" in caplog.text

    def test_missing_sections_parse_to_nothing(self):
        assert parse_watches({}) == {}
        assert parse_channels({}) == {}

    def test_channels_resolve_in_the_order_the_watch_lists_them(self):
        channels = parse_channels(
            {
                "mail": {"kind": "email", "to_address": "me@example.org"},
                "matrix": {"kind": "matrix", "room": "#a:example.org"},
            }
        )
        watch = _watch(channels=("matrix", "mail"))
        assert [c.name for c in resolve_channels(watch, channels)] == ["matrix", "mail"]

    def test_an_unresolvable_channel_is_reported_and_skipped(self, caplog):
        """The user believes they are being alerted and they are not."""
        channels = parse_channels({"mail": {"kind": "email"}})
        with caplog.at_level(logging.ERROR):
            resolved = resolve_channels(_watch(channels=("mail", "gone")), channels)
        assert [c.name for c in resolved] == ["mail"]
        assert "gone" in caplog.text
