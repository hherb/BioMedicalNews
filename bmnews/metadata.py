"""Helpers for the ``paper_extras.metadata_json`` blob.

The fields a source reports that bmlib's ``publications`` schema has no column
for — Europe PMC's ``cited_by``, say — are stored as a JSON object.  Decoding
it defensively belongs in one place: the column holds data written by earlier
versions of the app and by every fetcher, so a malformed or unexpectedly
shaped value must degrade to "no metadata" rather than raise.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["parse_metadata"]


def parse_metadata(raw: Any) -> dict:
    """Decode a stored ``metadata_json`` value into a dict.

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
        meta = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return meta if isinstance(meta, dict) else {}
