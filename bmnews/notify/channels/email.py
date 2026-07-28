"""Email delivery for notifications, over the digest's SMTP settings."""

from __future__ import annotations

import logging

from bmnews.config import EmailConfig
from bmnews.digest.sender import send_email
from bmnews.notify.channels import ChannelError, Message

logger = logging.getLogger(__name__)


class EmailChannel:
    """Sends a notification as a multipart email.

    The SMTP settings come from ``[email]`` rather than being duplicated per
    channel: a user with a working digest already has them, and a second copy
    is a second thing to keep in step. Only the recipient and the subject
    prefix are per-channel, so one watch can alert a different address than the
    digest goes to.

    ``email.enabled`` is deliberately **not** consulted. It switches the
    *digest* email on and off, and a watch is a separate decision: "post me the
    weekly summary" and "tell me the moment an RCT lands" are different
    subscriptions, and someone who has turned the first off has not thereby
    asked for the second to stop. ``[notifications] enabled`` and the watch's
    own ``enabled`` are what govern this.
    """

    def __init__(
        self,
        *,
        name: str,
        to_address: str,
        subject_prefix: str,
        email: EmailConfig,
    ) -> None:
        """Configure the adapter.

        Args:
            name: The channel's config name, used in log messages.
            to_address: Resolved recipient.
            subject_prefix: Prefix applied to the message's bare subject.
            email: The ``[email]`` settings supplying the SMTP connection.
        """
        self.name = name
        self._to_address = to_address
        self._subject_prefix = subject_prefix
        self._email = email

    def send(self, message: Message, *, txn_key: str) -> None:
        """Send *message* by SMTP.

        Args:
            message: The rendered notification.
            txn_key: Ignored — SMTP has no idempotency key, so a duplicate
                after a crash between sending and recording is the accepted
                worst case here. Matrix, which does have one, uses it.

        Raises:
            ChannelError: If the message was not accepted for delivery.
        """
        subject = f"{self._subject_prefix} {message.subject}".strip()

        delivered = send_email(
            html_body=message.html,
            text_body=message.text,
            subject=subject,
            from_address=self._email.from_address,
            to_address=self._to_address,
            smtp_host=self._email.smtp_host,
            smtp_port=self._email.smtp_port,
            smtp_user=self._email.smtp_user,
            smtp_password=self._email.smtp_password,
            use_tls=self._email.use_tls,
        )

        # send_email logs the exception and returns False. Turning that back
        # into a raise is the point: the service records `sent` from a call
        # that returned, and a False treated as success would drop these
        # papers out of the derived queue without anyone having been told.
        if not delivered:
            raise ChannelError(f"channel {self.name!r} could not send to {self._to_address}")

        logger.info("Notification sent to %s over channel %r", self._to_address, self.name)
