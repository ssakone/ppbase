"""Email service for verification and password reset emails.

When SMTP is not configured (``settings.smtp_host`` is empty), the token
URL is logged to stdout so developers can still complete flows locally.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _smtp_configured(settings: Any) -> bool:
    return bool(getattr(settings, "smtp_host", ""))


async def _send_email(
    to: str,
    subject: str,
    body_text: str,
    settings: Any,
) -> None:
    """Send an email via SMTP if configured, else log it."""
    if not _smtp_configured(settings):
        logger.info("SMTP not configured — email not sent.")
        logger.info("  To:      %s", to)
        logger.info("  Subject: %s", subject)
        logger.info("  Body:\n%s", body_text)
        return

    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body_text)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.ehlo()
            if settings.smtp_port != 25:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
        logger.info("Sent email to %s: %s", to, subject)
    except Exception:
        logger.exception("Failed to send email to %s", to)


async def send_verification_email(
    to: str,
    token: str,
    settings: Any,
    *,
    base_url: str = "",
) -> None:
    """Send (or log) a verification email."""
    url = f"{base_url}/_/#/auth/confirm-verification/{token}"
    subject = "Verify your email"
    body = (
        f"Hello,\n\n"
        f"Click the link below to verify your email address.\n\n"
        f"{url}\n\n"
        f"If you did not request this email you can safely ignore it."
    )

    if not _smtp_configured(settings):
        logger.info("[DEV] Verification token for %s: %s", to, token)

    await _send_email(to, subject, body, settings)


async def send_password_reset_email(
    to: str,
    token: str,
    settings: Any,
    *,
    base_url: str = "",
) -> None:
    """Send (or log) a password reset email."""
    url = f"{base_url}/_/#/auth/confirm-password-reset/{token}"
    subject = "Reset your password"
    body = (
        f"Hello,\n\n"
        f"Click the link below to reset your password.\n\n"
        f"{url}\n\n"
        f"If you did not request this email you can safely ignore it."
    )

    if not _smtp_configured(settings):
        logger.info("[DEV] Password-reset token for %s: %s", to, token)

    await _send_email(to, subject, body, settings)
