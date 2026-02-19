from __future__ import annotations

import pytest
from httpx import AsyncClient

from ppbase.api import settings as settings_api

pytestmark = pytest.mark.asyncio


async def _save_smtp_settings(
    app_client: AsyncClient,
    admin_token: str,
    *,
    host: str,
    port: int = 587,
    username: str = "",
    password: str = "",
    tls: bool = True,
) -> None:
    response = await app_client.patch(
        "/api/settings",
        headers={"Authorization": admin_token},
        json={
            "meta": {
                "appName": "PPBase Tests",
                "senderName": "PPBase QA",
                "senderAddress": "noreply@test.local",
            },
            "smtp": {
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "tls": tls,
            },
        },
    )
    assert response.status_code == 200, response.text


async def test_send_test_email_requires_admin_auth(app_client: AsyncClient) -> None:
    response = await app_client.post(
        "/api/settings/test/email",
        json={"email": "qa@example.com", "template": "verification"},
    )
    assert response.status_code == 401


async def test_send_test_email_validates_body_fields(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    response = await app_client.post(
        "/api/settings/test/email",
        headers={"Authorization": admin_token},
        json={},
    )
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["message"] == "Failed to send the test email."
    assert "email" in payload["data"]
    assert "template" in payload["data"]


async def test_send_test_email_requires_configured_smtp_host(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    await _save_smtp_settings(app_client, admin_token, host="")

    response = await app_client.post(
        "/api/settings/test/email",
        headers={"Authorization": admin_token},
        json={"email": "qa@example.com", "template": "verification"},
    )
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["data"]["smtp"]["code"] == "validation_required"


async def test_send_test_email_dispatches_message_with_current_settings(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _save_smtp_settings(
        app_client,
        admin_token,
        host="smtp.test.local",
        port=2525,
        username="smtp-user",
        password="smtp-pass",
        tls=True,
    )

    sent: dict[str, object] = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port
            sent["timeout"] = timeout
            sent["ehlo"] = 0
            sent["starttls"] = 0
            sent["login"] = None
            sent["message"] = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def ehlo(self):
            sent["ehlo"] = int(sent["ehlo"]) + 1

        def starttls(self):
            sent["starttls"] = int(sent["starttls"]) + 1

        def login(self, username, password):
            sent["login"] = (username, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setattr(settings_api.smtplib, "SMTP", _FakeSMTP)

    response = await app_client.post(
        "/api/settings/test/email",
        headers={"Authorization": admin_token},
        json={
            "email": "qa@example.com",
            "template": "verification",
            "collection": "users",
        },
    )
    assert response.status_code == 204

    assert sent["host"] == "smtp.test.local"
    assert sent["port"] == 2525
    assert sent["starttls"] == 1
    assert sent["login"] == ("smtp-user", "smtp-pass")
    message = sent["message"]
    assert message is not None
    assert message["To"] == "qa@example.com"
    assert "verification" in str(message["Subject"]).lower()
    assert "Collection: users" in message.get_content()


async def test_send_test_email_surfaces_smtp_errors(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _save_smtp_settings(app_client, admin_token, host="smtp.test.local")

    class _BoomSMTP:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("smtp offline")

    monkeypatch.setattr(settings_api.smtplib, "SMTP", _BoomSMTP)

    response = await app_client.post(
        "/api/settings/test/email",
        headers={"Authorization": admin_token},
        json={"email": "qa@example.com", "template": "password-reset"},
    )
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["data"]["smtp"]["code"] == "smtp_send_failed"
    assert "smtp offline" in payload["data"]["smtp"]["message"]
