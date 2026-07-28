"""Delivery adapters, dispatched by a channel's ``kind``.

A channel says *where* a notification goes; the adapter knows *how* to put it
there. The split is what keeps :mod:`bmnews.notify.service` free of transport
detail — it renders one :class:`Message` per watch and hands it to each
adapter, which either returns or raises :class:`ChannelError`.

Adapters raise rather than returning a boolean deliberately. The service
records ``sent`` or ``failed`` per channel from that outcome, and a delivery
that silently reported success would mark papers as notified and drop them out
of the derived queue forever.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from bmnews.config import AppConfig
from bmnews.notify.watches import Channel


class ChannelError(RuntimeError):
    """A channel could not be built, or a delivery over it did not happen."""


@dataclass(frozen=True)
class Message:
    """One rendered notification, in the forms every adapter might need.

    Attributes:
        subject: Bare subject line, without any prefix — a channel that wants
            one applies its own, since the prefix is a per-channel setting.
        html: Rich rendering. Matrix's HTML subset has no CSS at all, so this
            is rendered per medium rather than shared with the digest.
        text: Plain-text alternative. Required, not optional: it is the email's
            second MIME part and Matrix's mandatory ``body`` beside a
            ``formatted_body``.
    """

    subject: str
    html: str
    text: str


class ChannelAdapter(Protocol):
    """What the service needs from any delivery destination."""

    name: str

    def send(self, message: Message, *, txn_key: str) -> None:
        """Deliver *message*, raising :class:`ChannelError` if it did not go."""
        ...


def transaction_key(watch: str, channel: str, paper_ids: Sequence[int]) -> str:
    """Derive a stable idempotency key for one batch on one channel.

    Matrix treats a repeat PUT carrying a transaction id it has already seen as
    a retransmission and returns the original event rather than posting twice.
    That closes the one window no database can: message sent, row not yet
    written, process dies. On the retry the send is a server-side no-op and the
    row is written properly.

    So this must be derived, never random — and it must not depend on the order
    the papers happened to come back in.

    Args:
        watch: Name of the watch delivering.
        channel: Name of the channel it is delivering over.
        paper_ids: The papers in this batch, in any order.

    Returns:
        A URL-safe hex digest, usable directly as a path segment.
    """
    material = "\x1f".join([watch, channel, *(str(pid) for pid in sorted(paper_ids))])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_adapter(channel: Channel, config: AppConfig) -> ChannelAdapter:
    """Build the adapter that delivers over *channel*.

    Args:
        channel: The parsed channel definition.
        config: Application config, which supplies the SMTP settings an email
            channel reuses from ``[email]``.

    Returns:
        A ready adapter.

    Raises:
        ChannelError: If the kind has no adapter, or the settings it needs are
            missing. Failing here rather than at send time means the problem is
            reported once per run instead of once per paper.
    """
    if channel.kind == "email":
        return _build_email(channel, config)
    if channel.kind == "matrix":
        return _build_matrix(channel)
    raise ChannelError(f"channel {channel.name!r} has no adapter for kind {channel.kind!r}")


def _build_email(channel: Channel, config: AppConfig) -> ChannelAdapter:
    """Build an email adapter, reusing the ``[email]`` SMTP settings."""
    from bmnews.notify.channels.email import EmailChannel

    if not config.email.smtp_host:
        raise ChannelError(f"channel {channel.name!r} needs smtp_host in the [email] section")

    recipient = _setting(channel, "to_address") or config.email.to_address or config.user.email
    if not recipient:
        raise ChannelError(f"channel {channel.name!r} has no recipient address")

    return EmailChannel(
        name=channel.name,
        to_address=recipient,
        subject_prefix=_setting(channel, "subject_prefix") or config.email.subject_prefix,
        email=config.email,
    )


def _build_matrix(channel: Channel) -> ChannelAdapter:
    """Build a Matrix adapter, which needs all three of its settings."""
    from bmnews.notify.channels.matrix import MatrixChannel

    values = {}
    for key in ("homeserver", "access_token", "room"):
        value = _setting(channel, key)
        if not value:
            raise ChannelError(f"channel {channel.name!r} is missing {key}")
        values[key] = value

    return MatrixChannel(name=channel.name, **values)


def _setting(channel: Channel, key: str) -> str:
    """Read one channel setting as stripped text, or ``""`` when unset."""
    value: Any = channel.settings.get(key, "")
    return str(value).strip()
