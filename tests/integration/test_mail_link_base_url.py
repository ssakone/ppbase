"""E-mailed links use the stored application URL, never the request headers.

PocketBase builds every action URL on the application URL setting alone
(v0.22.34 ``mails/admin.go:22-26`` and ``mails/record.go:175-184``, v0.40.6
``mails/record.go:265-266``), whose default is ``http://localhost:8090``
(v0.22.34 ``models/settings/settings.go:77``).  Measured on the 0.22.34
binary: forged ``Host`` and ``X-Forwarded-*`` headers change neither the
admin password reset link nor the record verification link.

A link built on those headers would let anyone who knows an e-mail address
have the real server send a link to their own site, which reads the token in
the URL fragment when the account owner clicks it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

PASSWORD = "first-password-123"
DEFAULT_APP_URL = "http://localhost:8090"
FORGED_HEADERS = {
    "Host": "attacker-host.invalid",
    "X-Forwarded-Host": "attacker.invalid",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-Port": "8443",
}


@pytest.fixture
def sent_links(monkeypatch) -> list[dict[str, Any]]:
    """Capture the base URL handed to every mail function."""
    import ppbase.services.mail_service as mail_service

    captured: list[dict[str, Any]] = []

    def _capture(kind: str):
        async def _send(to, token, settings, *, base_url="", **_kwargs):
            captured.append({"kind": kind, "to": to, "base_url": base_url})
            return True

        return _send

    for kind, name in (
        ("admin_reset", "send_admin_password_reset_email"),
        ("verification", "send_verification_email"),
        ("password_reset", "send_password_reset_email"),
        ("email_change", "send_confirm_email_change_email"),
    ):
        monkeypatch.setattr(mail_service, name, _capture(kind))
    return captured


async def _settings_row() -> Any:
    from ppbase.db.engine import get_engine

    async with get_engine().connect() as conn:
        result = await conn.execute(text("SELECT id, value FROM _params WHERE key = 'settings'"))
        return result.mappings().first()


async def _set_settings(state: str, original: Any) -> None:
    """``absent``: no settings row; ``empty``: appURL ""; ``configured``: set."""
    from ppbase.db.engine import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM _params WHERE key = 'settings'"))
        if state == "absent":
            return
        value = json.loads(json.dumps(original["value"])) if original and original["value"] else {}
        meta = value.setdefault("meta", {})
        meta["appURL"] = "" if state == "empty" else "https://buma.example/"
        await conn.execute(
            text(
                "INSERT INTO _params (id, key, value) "
                "VALUES (:id, 'settings', CAST(:value AS jsonb))"
            ),
            {
                "id": original["id"] if original else uuid.uuid4().hex[:15],
                "value": json.dumps(value),
            },
        )


async def _restore(original: Any) -> None:
    from ppbase.db.engine import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM _params WHERE key = 'settings'"))
        if original is not None:
            await conn.execute(
                text(
                    "INSERT INTO _params (id, key, value) "
                    "VALUES (:id, 'settings', CAST(:value AS jsonb))"
                ),
                {"id": original["id"], "value": json.dumps(original["value"])},
            )


EXPECTED = {
    "absent": DEFAULT_APP_URL,
    "empty": DEFAULT_APP_URL,
    "configured": "https://buma.example",
}


async def test_admin_reset_link_ignores_forged_headers(
    app_client: AsyncClient, admin_token: str, sent_links
) -> None:
    original = await _settings_row()
    try:
        for state, expected in EXPECTED.items():
            # A fresh admin per state: the resend threshold is 120 seconds.
            email = f"link_{uuid.uuid4().hex[:10]}@example.com"
            created = await app_client.post(
                "/api/admins",
                headers={"Authorization": admin_token},
                json={"email": email, "password": PASSWORD},
            )
            assert created.status_code == 200, created.text
            await _set_settings(state, original)

            resp = await app_client.post(
                "/api/admins/request-password-reset",
                json={"email": email},
                headers=FORGED_HEADERS,
            )
            assert resp.status_code == 204, resp.text
            assert sent_links[-1] == {"kind": "admin_reset", "to": email, "base_url": expected}, (
                state
            )
    finally:
        await _restore(original)
    assert all("attacker" not in link["base_url"] for link in sent_links)


async def test_record_links_ignore_forged_headers(
    app_client: AsyncClient, auth_collection: dict, sent_links
) -> None:
    original = await _settings_row()
    try:
        for state, expected in EXPECTED.items():
            email = f"link_{uuid.uuid4().hex[:10]}@example.com"
            created = await app_client.post(
                "/api/collections/users/records",
                json={"email": email, "password": PASSWORD, "passwordConfirm": PASSWORD},
            )
            assert created.status_code == 200, created.text
            login = await app_client.post(
                "/api/collections/users/auth-with-password",
                json={"identity": email, "password": PASSWORD},
            )
            assert login.status_code == 200, login.text
            await _set_settings(state, original)

            for path, body, headers in (
                ("request-verification", {"email": email}, {}),
                ("request-password-reset", {"email": email}, {}),
                (
                    "request-email-change",
                    {"newEmail": f"new_{uuid.uuid4().hex[:10]}@example.com"},
                    {"Authorization": login.json()["token"]},
                ),
            ):
                before = len(sent_links)
                resp = await app_client.post(
                    f"/api/collections/users/{path}",
                    json=body,
                    headers={**FORGED_HEADERS, **headers},
                )
                assert resp.status_code == 204, (state, path, resp.text)
                assert len(sent_links) == before + 1, (state, path)
                assert sent_links[-1]["base_url"] == expected, (state, path)
    finally:
        await _restore(original)
    assert all("attacker" not in link["base_url"] for link in sent_links)
