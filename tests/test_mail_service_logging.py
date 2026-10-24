"""Without SMTP, messages, links and tokens are logged in dev mode only.

A password reset or e-mail change link grants access to the account, and an
OTP code signs the user in: outside the explicit development mode
(``PPBASE_DEV``) PPBase logs a warning without recipient, link or code.
PocketBase never logs a message body.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from ppbase.services import mail_service

TOKEN = "eyJhbGciOiJIUzI1NiJ9.c3ludGhldGlj.c2lnbmF0dXJl"
TO = "owner@example.com"
APP_URL = "https://app.example"

LINK_MAILS = (
    mail_service.send_verification_email,
    mail_service.send_password_reset_email,
    mail_service.send_confirm_email_change_email,
    mail_service.send_admin_password_reset_email,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("send", LINK_MAILS, ids=lambda send: send.__name__)
async def test_links_are_not_logged_outside_dev_mode(caplog, send) -> None:
    settings = SimpleNamespace(smtp_host="", dev=False)
    with caplog.at_level(logging.DEBUG, logger=mail_service.__name__):
        await send(TO, TOKEN, settings, base_url=APP_URL)
    assert caplog.records, "a warning must say that nothing was sent"
    assert TOKEN not in caplog.text
    assert APP_URL not in caplog.text
    assert TO not in caplog.text
    assert "not sent" in caplog.text


@pytest.mark.asyncio
async def test_otp_code_is_not_logged_outside_dev_mode(caplog) -> None:
    settings = SimpleNamespace(smtp_host="", dev=False)
    with caplog.at_level(logging.DEBUG, logger=mail_service.__name__):
        await mail_service.send_otp_email(TO, "otp00000000001", "739104", settings)
    assert "739104" not in caplog.text
    assert TO not in caplog.text
    assert "not sent" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("send", LINK_MAILS[:3], ids=lambda send: send.__name__)
async def test_dev_mode_still_logs_the_record_links(caplog, send) -> None:
    """The local development helper is kept, behind ``PPBASE_DEV`` only."""
    settings = SimpleNamespace(smtp_host="", dev=True)
    with caplog.at_level(logging.DEBUG, logger=mail_service.__name__):
        await send(TO, TOKEN, settings, base_url=APP_URL)
    assert TOKEN in caplog.text
    assert f"{APP_URL}/_/#/auth/" in caplog.text


@pytest.mark.asyncio
async def test_admin_reset_link_is_never_logged_even_in_dev_mode(caplog) -> None:
    settings = SimpleNamespace(smtp_host="", dev=True)
    with caplog.at_level(logging.DEBUG, logger=mail_service.__name__):
        sent = await mail_service.send_admin_password_reset_email(
            TO, TOKEN, settings, base_url=APP_URL
        )
    assert sent is False
    assert TOKEN not in caplog.text
