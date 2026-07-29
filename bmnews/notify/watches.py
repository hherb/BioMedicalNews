"""Watch and channel definitions, parsed from the ``[notifications]`` config.

A **watch** is a named set of criteria that, when a newly scored paper matches
them, delivers a notification over one or more **channels**. The criteria are
declarative typed fields rather than free text: they validate cleanly, test
without an LLM, and render as a GUI form.

This module is the validating boundary between the config file and the rest of
the notification stage. :func:`bmnews.config._apply_section` setattrs whatever
TOML holds without checking it, so a mistyped criterion key would otherwise sit
in the config doing nothing and the watch would quietly under- or over-match.
Everything here reports instead: an unknown key is warned about by name, and a
value that cannot mean anything raises :class:`WatchConfigError`.

Matching the criteria against a paper is :mod:`bmnews.notify.matcher`, which
stays a pure function so the criteria engine tests against literal dicts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bmlib.quality import QualityTier, StudyDesign

from bmnews.constants import DEFAULT_NOTIFY_MAX_PER_RUN

logger = logging.getLogger(__name__)

#: Channel kinds with a delivery adapter. The kind selects the adapter, so a
#: kind nobody implements is an error rather than a warning — a channel with a
#: typo'd kind would accept papers and deliver none of them.
CHANNEL_KINDS = ("email", "matrix")

#: Settings each channel kind understands, beyond ``kind`` itself. Used only to
#: warn about keys that will be ignored; adapters read what they need.
CHANNEL_SETTINGS: dict[str, frozenset[str]] = {
    "email": frozenset({"to_address", "subject_prefix"}),
    "matrix": frozenset({"homeserver", "access_token", "room"}),
}


class WatchConfigError(ValueError):
    """A watch or channel is configured in a way that cannot be acted on."""


@dataclass(frozen=True)
class Channel:
    """One delivery destination, named by its config table.

    Attributes:
        name: The table name under ``[notifications.channels]``, which is how
            a watch refers to it.
        kind: Which adapter delivers it — one of :data:`CHANNEL_KINDS`.
        settings: The remaining scalar keys from the table, passed to the
            adapter. Kept as a plain dict because the keys a kind needs are
            the adapter's business, not this module's.
    """

    name: str
    kind: str
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, name: str, data: dict[str, Any]) -> Channel:
        """Build a channel from one ``[notifications.channels.<name>]`` table.

        Args:
            name: The channel's config table name.
            data: Its raw key/value pairs.

        Returns:
            The parsed channel.

        Raises:
            WatchConfigError: If ``kind`` is missing or names no adapter.
        """
        kind = str(data.get("kind", "")).strip().lower()
        if not kind:
            raise WatchConfigError(f"channel {name!r} has no 'kind'")
        if kind not in CHANNEL_KINDS:
            known = ", ".join(CHANNEL_KINDS)
            raise WatchConfigError(f"channel {name!r} has unknown kind {kind!r} (known: {known})")

        settings = {key: value for key, value in data.items() if key != "kind"}
        _warn_unknown(f"channel {name!r}", settings, CHANNEL_SETTINGS[kind])
        return cls(name=name, kind=kind, settings=settings)


@dataclass(frozen=True)
class Watch:
    """A named set of criteria and the channels a match is delivered over.

    Every criterion is AND-combined, and an empty collection means "no
    constraint" — so a watch with only ``min_relevance`` set matches on
    relevance alone. Within a single list criterion the test is ``any``: two
    tags mean "either tag", not "both".

    Attributes:
        name: The table name under ``[notifications.watches]``.
        enabled: Whether the watch is evaluated at all.
        min_relevance: Floor on the LLM relevance score, 0.0–1.0.
        min_combined: Floor on the combined score, 0.0–1.0.
        min_quality_tier: A :class:`~bmlib.quality.QualityTier` name, or ``""``
            for no floor. ``UNCLASSIFIED`` papers survive a floor — unjudged is
            not judged-and-rejected, the same rule the digest applies.
        tags: Interest tags the scorer matched, any of which qualifies.
        keywords: Case-insensitive substrings sought in title or abstract.
        sources: Registry source names, any of which qualifies.
        journals: Journal names, compared case-insensitively.
        study_designs: :class:`~bmlib.quality.StudyDesign` values, e.g. ``rct``.
        channels: Names of channels under ``[notifications.channels]``. A name
            repeated in the config is dropped at parse time, since delivering
            to the same destination twice in one run is never what it meant.
        max_per_run: How many papers one run delivers for this watch. The rest
            stay in the derived queue rather than being dropped.
    """

    name: str
    enabled: bool = True
    min_relevance: float = 0.0
    min_combined: float = 0.0
    min_quality_tier: str = ""
    tags: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    journals: tuple[str, ...] = ()
    study_designs: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    max_per_run: int = DEFAULT_NOTIFY_MAX_PER_RUN

    @classmethod
    def from_config(cls, name: str, data: dict[str, Any]) -> Watch:
        """Build a watch from one ``[notifications.watches.<name>]`` table.

        Args:
            name: The watch's config table name.
            data: Its raw key/value pairs.

        Returns:
            The parsed watch.

        Raises:
            WatchConfigError: If a value cannot be interpreted — a
                non-numeric threshold, a score outside 0.0–1.0, a tier or
                study design bmlib does not define, a non-boolean ``enabled``,
                or ``max_per_run`` below 1.
        """
        _warn_unknown(f"watch {name!r}", data, _WATCH_FIELDS)

        return cls(
            name=name,
            enabled=_flag("enabled", data.get("enabled", True)),
            min_relevance=_score("min_relevance", data.get("min_relevance", 0.0)),
            min_combined=_score("min_combined", data.get("min_combined", 0.0)),
            min_quality_tier=_tier(data.get("min_quality_tier", "")),
            tags=_strings(data.get("tags")),
            keywords=_strings(data.get("keywords")),
            sources=_strings(data.get("sources")),
            journals=_strings(data.get("journals")),
            study_designs=_designs(data.get("study_designs")),
            channels=_channels(f"watch {name!r}", data.get("channels")),
            max_per_run=_max_per_run(data.get("max_per_run")),
        )


#: Criterion keys a watch table may carry. Derived from the dataclass so the
#: two cannot drift; ``name`` comes from the table heading, not a key.
_WATCH_FIELDS: frozenset[str] = frozenset(Watch.__dataclass_fields__) - {"name"}


def parse_channels(raw: dict[str, dict[str, Any]]) -> dict[str, Channel]:
    """Parse every ``[notifications.channels.*]`` table.

    A channel that cannot be parsed is logged at ERROR and skipped rather than
    raised: one malformed table must not take down delivery for the others,
    and the notification stage runs inside ``bmnews run``, where an exception
    would abort a pipeline that has already done its expensive work.

    Args:
        raw: ``NotificationsConfig.channels``.

    Returns:
        The usable channels, keyed by name.
    """
    return dict(_parse_each(raw, Channel.from_config, "channel"))


def parse_watches(raw: dict[str, dict[str, Any]]) -> dict[str, Watch]:
    """Parse every ``[notifications.watches.*]`` table.

    Malformed watches are skipped with an ERROR, for the reasons in
    :func:`parse_channels`.

    Args:
        raw: ``NotificationsConfig.watches``.

    Returns:
        The usable watches, keyed by name. Disabled watches are included —
        whether to act on one is the caller's decision, and ``bmnews notify
        --list`` reports them.
    """
    return dict(_parse_each(raw, Watch.from_config, "watch"))


def resolve_channels(watch: Watch, channels: dict[str, Channel]) -> list[Channel]:
    """Look up the channels *watch* delivers over.

    A name with no matching channel is logged at ERROR and skipped: it means
    the user believes they are being alerted and they are not, which is worth
    saying loudly even though it cannot be fixed here.

    Args:
        watch: The watch whose ``channels`` list is being resolved.
        channels: Every parsed channel, keyed by name.

    Returns:
        The resolved channels, in the order the watch lists them.
    """
    resolved = []
    for name in watch.channels:
        channel = channels.get(name)
        if channel is None:
            logger.error(
                "watch %r delivers to unknown channel %r — nothing will be sent there",
                watch.name,
                name,
            )
            continue
        resolved.append(channel)
    return resolved


# --- Field coercion ---------------------------------------------------------


def _parse_each(raw: dict[str, dict[str, Any]], build: Any, label: str) -> list[tuple[str, Any]]:
    """Apply *build* to each named table, skipping the ones that fail."""
    parsed = []
    for name, data in (raw or {}).items():
        if not isinstance(data, dict):
            logger.error("%s %r is not a table — ignoring it", label, name)
            continue
        try:
            parsed.append((name, build(name, data)))
        except WatchConfigError as exc:
            logger.error("ignoring %s %r: %s", label, name, exc)
    return parsed


def _warn_unknown(subject: str, data: dict[str, Any], known: frozenset[str]) -> None:
    """Warn about keys that will be ignored, naming each one.

    Silence here is the failure this module exists to prevent: a criterion
    misspelled in the config is a criterion not applied, and the watch goes on
    matching papers the user meant to exclude.
    """
    unknown = sorted(set(data) - known)
    if unknown:
        logger.warning(
            "%s has unrecognised key(s) %s — they are ignored", subject, ", ".join(unknown)
        )


def _flag(key: str, value: Any) -> bool:
    """Validate a boolean switch, rejecting anything that only looks like one.

    ``bool("false")`` is ``True``, so coercing here would turn a switch the
    user wrote off into one that is on — the exact silent misreading every
    other field in this module raises about. TOML has real booleans, so
    demanding one costs nothing.
    """
    if not isinstance(value, bool):
        raise WatchConfigError(f"{key} must be true or false, got {value!r}")
    return value


def _score(key: str, value: Any) -> float:
    """Coerce a 0.0–1.0 score threshold.

    ``bool`` is rejected rather than coerced: ``float(True)`` is ``1.0``, a
    threshold nothing realistic clears, and a config that says ``true`` here
    means something other than what it would get.
    """
    if isinstance(value, bool):
        raise WatchConfigError(f"{key} must be a number, got {value!r}")
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise WatchConfigError(f"{key} must be a number, got {value!r}") from None
    if not 0.0 <= score <= 1.0:
        raise WatchConfigError(f"{key} must be between 0.0 and 1.0, got {score}")
    return score


def _tier(value: Any) -> str:
    """Validate and normalise a quality-tier floor.

    Returns the canonical :class:`~bmlib.quality.QualityTier` name, so the
    matcher never has to re-parse what the config wrote.
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    try:
        return QualityTier[text].name
    except KeyError:
        known = ", ".join(tier.name for tier in QualityTier)
        raise WatchConfigError(f"unknown min_quality_tier {value!r} (known: {known})") from None


def _designs(value: Any) -> tuple[str, ...]:
    """Validate study-design values against bmlib's vocabulary.

    Scores store ``StudyDesign.value`` (``"rct"``, not ``"RCT"``), so a
    mismatched spelling would match nothing at all — worth rejecting rather
    than letting the watch look configured and stay silent.
    """
    designs = _strings(value)
    known = {design.value for design in StudyDesign}
    unknown = [design for design in designs if design.lower() not in known]
    if unknown:
        raise WatchConfigError(f"unknown study design(s): {', '.join(sorted(unknown))}")
    return tuple(design.lower() for design in designs)


def _channels(subject: str, value: Any) -> tuple[str, ...]:
    """Coerce the channel list, dropping repeats and naming what it dropped.

    A repeated name is always a mistake — ``["mail", "mail"]`` can mean nothing
    other than ``["mail"]`` — and acting on one delivers twice. Both callers
    iterate :func:`resolve_channels`, so ``run_notify()`` sends a second batch
    to the same destination in the same run (the queue is re-derived, so it is
    the *next* batch, which silently doubles ``max_per_run``) and
    ``pending_counts()`` reports the pair twice, which the GUI pane renders as
    two identical rows and sums into its total.

    Corrected here rather than in :func:`resolve_channels` so that every caller
    sees the corrected list without having to remember to de-duplicate it.
    """
    names = _strings(value)
    unique = tuple(dict.fromkeys(names))
    if len(unique) != len(names):
        repeated = sorted(name for name in unique if names.count(name) > 1)
        logger.warning(
            "%s repeats channel(s) %s — each is delivered to once",
            subject,
            ", ".join(repeated),
        )
    return unique


def _strings(value: Any) -> tuple[str, ...]:
    """Coerce a list criterion, tolerating a bare string for one entry."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    return tuple(str(item).strip() for item in value if str(item).strip())


def _max_per_run(value: Any) -> int:
    """Coerce the per-run delivery cap, which must leave something to send."""
    if value is None:
        return DEFAULT_NOTIFY_MAX_PER_RUN
    if isinstance(value, bool):
        raise WatchConfigError(f"max_per_run must be a whole number, got {value!r}")
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise WatchConfigError(f"max_per_run must be a whole number, got {value!r}") from None
    if count < 1:
        raise WatchConfigError(f"max_per_run must be at least 1, got {count}")
    return count
