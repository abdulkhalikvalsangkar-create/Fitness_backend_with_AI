"""Plain-text email over SMTP.

One small function so every feature that emails the user (export links,
password-reset codes) shares the same configuration and failure behaviour:
`send_email` never raises. Callers that must know the outcome read the bool.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from packages.config import get_settings

logger = logging.getLogger(__name__)


def configured() -> bool:
    return get_settings().mail.configured


def send_email(to_address: str, subject: str, body: str) -> bool:
    cfg = get_settings().mail
    if not (cfg.configured and to_address):
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg.smtp_from
    message["To"] = to_address
    message.set_content(body)
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as client:
            client.starttls()
            if cfg.smtp_user:
                client.login(cfg.smtp_user, cfg.smtp_password)
            client.send_message(message)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("email to %s failed: %s", to_address, exc)
        return False
