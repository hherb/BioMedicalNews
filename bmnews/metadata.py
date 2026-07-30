"""Helpers for the JSON-object columns bmnews stores.

The fields a source reports that bmlib's ``publications`` schema has no column
for — Europe PMC's ``cited_by``, say — are stored as a JSON object. Decoding
it defensively belongs in one place: the column holds data written by earlier
versions of the app and by every fetcher, so a malformed or unexpectedly
shaped value must degrade to "no metadata" rather than raise.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["parse_json_object", "parse_metadata", "parse_transparency"]


def parse_json_object(raw: Any) -> dict:
    """Decode a stored JSON-object column into a dict.

    Args:
        raw: The stored value — a JSON string, an already-decoded dict, or None.

    Returns:
        The decoded mapping, or an empty dict when it is missing, malformed, or
        not a JSON object.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def parse_metadata(raw: Any) -> dict:
    """Decode a stored ``paper_extras.metadata_json`` value into a dict.

    The column holds data written by every fetcher and by earlier versions of
    the app, so a malformed or unexpectedly shaped value degrades to "no
    metadata" rather than raising.
    """
    return parse_json_object(raw)


def parse_transparency(raw: Any) -> dict:
    """Decode a stored ``transparency.result_json`` value into a dict.

    Deliberately **not** ``bmlib.transparency.TransparencyResult.from_dict()``.
    That classmethod raises on an ``unknown_reason`` value it does not
    recognise, which is right for bmlib and wrong here: a newer bmlib writing a
    member this one has not heard of must not stop a paper's page rendering.
    The display surfaces read plain keys, so a dict is all they need.
    """
    return parse_json_object(raw)
