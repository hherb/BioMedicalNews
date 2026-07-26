"""Watch-based notifications: alert on a matching paper as it is scored.

The digest is a periodic roundup of everything that crossed a threshold since
last time. A **watch** is the other half: named criteria that, when a newly
scored paper satisfies them, deliver an alert now, over email or Matrix.

A notified paper still appears in the next digest — a notification is "now",
the digest is the record. That falls out of keeping deliveries in their own
``notifications`` table: ``get_papers_for_digest`` excludes papers present in
``digest_papers`` and nothing else.

This package currently holds the two pure pieces:

- :mod:`bmnews.notify.watches` — ``Watch`` and ``Channel``, parsed and
  validated from the ``[notifications]`` config section.
- :mod:`bmnews.notify.matcher` — ``matches(paper, watch)``, a predicate over
  the paper dict with no I/O of any kind.

Delivery adapters and the ``run_notify()`` stage that selects, pages and
records are not implemented yet; see
``docs/plans/2026-07-26-notification-service-design.md``.
"""

from __future__ import annotations

from bmnews.notify.matcher import matches
from bmnews.notify.watches import (
    CHANNEL_KINDS,
    Channel,
    Watch,
    WatchConfigError,
    parse_channels,
    parse_watches,
    resolve_channels,
)

__all__ = [
    "CHANNEL_KINDS",
    "Channel",
    "Watch",
    "WatchConfigError",
    "matches",
    "parse_channels",
    "parse_watches",
    "resolve_channels",
]
