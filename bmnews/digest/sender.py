"""Send digest emails via SMTP."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bmnews.config import EmailConfig

logger = logging.getLogger(__name__)


def send_digest(
    *,
    config: EmailConfig,
    to_address: str,
    subject: str,
    html_body: str,
    text_body: str,
) -> bool:
    """Send a multipart (HTML + plain text) email.

    Returns True on success, False on failure.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.from_address or config.smtp_user
    msg["To"] = to_address

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if config.use_tls:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)

        if config.smtp_user and config.smtp_password:
            server.login(config.smtp_user, config.smtp_password)

        server.sendmail(msg["From"], [to_address], msg.as_string())
        server.quit()
        logger.info("Digest email sent to %s", to_address)
        return True
    except Exception:
        logger.exception("Failed to send digest email to %s", to_address)
        return False
