"""Email service for verification and password reset emails.

Messages go through the SMTP server of ``PPBASE_SMTP_*`` (``settings.smtp_host``).
When it is not configured nothing is sent.  The message (link and token
included) is then logged only in the explicit development mode
(``PPBASE_DEV``); otherwise a warning without recipient, link or token is
logged, because these links grant access to the account (password reset,
e-mail change) and logs are kept and shared far more widely than mailboxes.
PocketBase never logs a message body.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _smtp_configured(settings: Any) -> bool:
    return bool(getattr(settings, "smtp_host", ""))


def _dev_mode(settings: Any) -> bool:
    return getattr(settings, "dev", False) is True


def _smtp_send(to: str, subject: str, body_text: str, settings: Any) -> None:
    """Hand one plain-text message to the configured SMTP server (raises)."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body_text)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.ehlo()
        if settings.smtp_port != 25:
            server.starttls()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)


async def _send_email(
    to: str,
    subject: str,
    body_text: str,
    settings: Any,
) -> None:
    """Send an email via SMTP if configured; else log it in dev mode only."""
    if not _smtp_configured(settings):
        if not _dev_mode(settings):
            logger.warning(
                "SMTP not configured (PPBASE_SMTP_HOST): %r email not sent.", subject
            )
            return
        logger.info("SMTP not configured — email not sent.")
        logger.info("  To:      %s", to)
        logger.info("  Subject: %s", subject)
        logger.info("  Body:\n%s", body_text)
        return

    try:
        _smtp_send(to, subject, body_text, settings)
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

    if not _smtp_configured(settings) and _dev_mode(settings):
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

    if not _smtp_configured(settings) and _dev_mode(settings):
        logger.info("[DEV] Password-reset token for %s: %s", to, token)

    await _send_email(to, subject, body, settings)


async def send_confirm_email_change_email(
    to: str,
    token: str,
    settings: Any,
    *,
    base_url: str = "",
) -> None:
    """Send (or log) an email change confirmation email."""
    url = f"{base_url}/_/#/auth/confirm-email-change/{token}"
    subject = "Confirm your new email address"
    body = (
        f"Hello,\n\n"
        f"Click the link below to confirm your new email address.\n\n"
        f"{url}\n\n"
        f"If you did not request this email you can safely ignore it."
    )

    if not _smtp_configured(settings) and _dev_mode(settings):
        logger.info("[DEV] Email-change token for %s: %s", to, token)

    await _send_email(to, subject, body, settings)


async def send_otp_email(
    to: str,
    otp_id: str,
    otp_code: str,
    settings: Any,
) -> None:
    """Send (or log) an OTP authentication code."""
    subject = "Your one-time password"
    body = (
        f"Hello,\n\n"
        f"Your one-time password is:\n\n"
        f"{otp_code}\n\n"
        f"If you did not request this email you can safely ignore it."
    )

    if not _smtp_configured(settings) and _dev_mode(settings):
        logger.info("[DEV] OTP for %s (otpId=%s): %s", to, otp_id, otp_code)

    await _send_email(to, subject, body, settings)


async def send_admin_password_reset_email(
    to: str,
    token: str,
    settings: Any,
    *,
    base_url: str = "",
    app_name: str = "",
) -> bool:
    """Send the PocketBase 0.22 admin password reset email.

    Same subject and link as PocketBase 0.22.34 (mails/admin.go:22-26 and :59,
    mails/templates/admin_password_reset.go:12-21).  Unlike the record flows,
    neither the link nor the token is ever logged: an admin reset token gives
    superuser access.  Returns whether the SMTP server accepted the message.
    """
    if not _smtp_configured(settings):
        logger.warning("SMTP not configured: admin password reset email not sent.")
        return False

    url = f"{base_url}/_/#/confirm-password-reset/{token}"
    app_label = f" for {app_name}" if app_name else ""
    body = (
        f"Hello,\n\n"
        f"Follow this link to reset your admin password{app_label}.\n\n"
        f"{url}\n\n"
        f"If you did not request to reset your password, please ignore this email "
        f"and the link will expire on its own."
    )
    try:
        await asyncio.to_thread(_smtp_send, to, "Reset admin password", body, settings)
    except Exception as exc:
        logger.error(
            "Failed to send the admin password reset email: %s", type(exc).__name__
        )
        return False
    return True
