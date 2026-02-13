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

    Returns True on success, False on failure.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port)
            server.ehlo()
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port)

        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)

        server.sendmail(from_address, [to_address], msg.as_string())
        server.quit()
        logger.info("Digest email sent to %s", to_address)
        return True

    except Exception:
        logger.exception("Failed to send digest email")
        return False
