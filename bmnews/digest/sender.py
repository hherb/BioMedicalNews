"""SMTP email delivery for digests."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_email(
    *,
    html_body: str,
    text_body: str,
    subject: str,
    from_address: str,
    to_address: str,
    smtp_host: str,
    smtp_port: int = 587,
    smtp_user: str = "",
    smtp_password: str = "",
    use_tls: bool = True,
) -> bool:
    """Send an email with HTML and plain-text alternatives.

    Args:
        html_body: HTML version of the message.
        text_body: Plain-text alternative.
        subject: Message subject line.
        from_address: Envelope and header sender address.
        to_address: Recipient address.
        smtp_host: SMTP server hostname.
        smtp_port: SMTP server port.
        smtp_user: Username for authentication; skipped when empty.
        smtp_password: Password for authentication; skipped when empty.
        use_tls: Whether to upgrade the connection with STARTTLS.

    Returns:
        True on success, False if delivery failed for any reason.
    """
    if not to_address:
        logger.error("No recipient address configured — digest not sent")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        # The context manager closes the socket even when login or sendmail
        # raises — the previous code leaked the connection on any failure.
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if use_tls:
                server.ehlo()
                server.starttls()
                server.ehlo()

            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)

            server.sendmail(from_address, [to_address], msg.as_string())

        logger.info("Digest email sent to %s", to_address)
        return True

    except Exception:
        logger.exception("Failed to send digest email")
        return False
