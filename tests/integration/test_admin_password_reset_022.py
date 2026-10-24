"""PocketBase 0.22 admin password reset routes.

``POST /api/admins/request-password-reset`` and ``confirm-password-reset``
(v0.22.34 apis/admin.go:21-22).  Expected statuses, messages and field errors
were measured on the official PocketBase 0.22.34 binary; PPBase keeps its own
error envelope key (``status`` instead of ``code``).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt as pyjwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

ADMIN_PASSWORD = "first-password-123"
VALIDATE_FORM = "An error occurred while validating the form."
GENERIC = "Something went wrong while processing your request."
BLANK = {"code": "validation_required", "message": "Cannot be blank."}
INVALID_TOKEN = {"code": "validation_invalid_token", "message": "Invalid or expired token."}


@pytest.fixture
def sent_mails(monkeypatch) -> list[dict[str, Any]]:
    """Capture the reset e-mails instead of talking to an SMTP server."""
    mails: list[dict[str, Any]] = []

    async def _capture(to, token, settings, *, base_url="", app_name=""):
        mails.append({"to": to, "token": token, "base_url": base_url})
        return True

    import ppbase.services.mail_service as mail_service

    monkeypatch.setattr(mail_service, "send_admin_password_reset_email", _capture)
    return mails


async def _create_admin(app_client: AsyncClient, admin_token: str) -> dict[str, Any]:
    email = f"reset_{uuid.uuid4().hex[:10]}@example.com"
    created = await app_client.post(
        "/api/admins",
        headers={"Authorization": admin_token},
        json={"email": email, "password": ADMIN_PASSWORD},
    )
    assert created.status_code == 200, created.text
    login = await app_client.post(
        "/api/admins/auth-with-password",
        json={"identity": email, "password": ADMIN_PASSWORD},
    )
    assert login.status_code == 200, login.text
    return {"id": created.json()["id"], "email": email, "token": login.json()["token"]}


async def _row(admin_id: str) -> Any:
    from ppbase.db.engine import get_engine

    async with get_engine().connect() as conn:
        result = await conn.execute(
            text("SELECT token_key, last_reset_sent_at FROM _superusers WHERE id = :id"),
            {"id": admin_id},
        )
        return result.mappings().first()


async def _reset_secret() -> str:
    from ppbase.db.engine import get_engine

    async with get_engine().connect() as conn:
        result = await conn.execute(
            text(
                "SELECT options -> 'passwordResetToken' ->> 'secret' FROM _collections "
                "WHERE name = '_superusers'"
            )
        )
        return result.scalar_one()


def _error(response) -> tuple[str, dict[str, Any]]:
    body = response.json()
    return body["message"], body["data"]


async def test_request_validation_matches_pocketbase(app_client: AsyncClient, sent_mails) -> None:
    """Required, then Length(1, 255) in bytes, then EmailFormat, first error only.

    forms/admin_password_reset_request.go:47-55; measured on 0.22.34: 255
    bytes pass, 256 bytes and 139 two-byte runes (269 bytes) fail on length,
    a 300-byte non-address fails on length, not on format.
    """
    length = {
        "code": "validation_length_out_of_range",
        "message": "The length must be between 1 and 255.",
    }
    labels = ".".join(["b" * 60] * 3)
    for body, field_error in (
        ({}, BLANK),
        ({"email": ""}, BLANK),
        (
            {"email": "not-an-email"},
            {"code": "validation_is_email", "message": "Must be a valid email address."},
        ),
        ({"email": "a" * 64 + "@" + labels + "." + "c" * 8}, length),
        ({"email": "x" * 300}, length),
        ({"email": "é" * 130 + "@c11.test"}, length),
    ):
        resp = await app_client.post("/api/admins/request-password-reset", json=body)
        assert resp.status_code == 400, resp.text
        assert _error(resp) == (VALIDATE_FORM, {"email": field_error})
    assert sent_mails == []

    # 255 bytes (and 109 two-byte runes, 209 bytes) are valid: 204, nothing sent.
    for email in ("a" * 64 + "@" + labels + "." + "c" * 7, "é" * 100 + "@c11.test"):
        resp = await app_client.post("/api/admins/request-password-reset", json={"email": email})
        assert resp.status_code == 204, resp.text
    assert sent_mails == []


async def test_request_answers_204_without_revealing_admins(
    app_client: AsyncClient, admin_token: str, sent_mails
) -> None:
    admin = await _create_admin(app_client, admin_token)
    unknown = await app_client.post(
        "/api/admins/request-password-reset", json={"email": "nobody.c11@example.com"}
    )
    assert unknown.status_code == 204, unknown.text
    upper = await app_client.post(
        "/api/admins/request-password-reset", json={"email": admin["email"].upper()}
    )
    assert upper.status_code == 204, upper.text
    assert sent_mails == []

    known = await app_client.post(
        "/api/admins/request-password-reset", json={"email": admin["email"]}
    )
    assert known.status_code == 204, known.text
    assert known.content == b""
    assert [mail["to"] for mail in sent_mails] == [admin["email"]]
    claims = pyjwt.decode(sent_mails[0]["token"], options={"verify_signature": False})
    # tokens/admin.go:19-26 claims (PPBase adds iat, ignored by PocketBase).
    assert {key: claims[key] for key in ("id", "type", "email")} == {
        "id": admin["id"],
        "type": "admin",
        "email": admin["email"],
    }
    assert (await _row(admin["id"]))["last_reset_sent_at"] is not None

    # Within 120 seconds nothing else is sent (forms/admin_password_reset_request.go:73-77).
    again = await app_client.post(
        "/api/admins/request-password-reset", json={"email": admin["email"]}
    )
    assert again.status_code == 204
    assert len(sent_mails) == 1


async def test_confirm_validation_matches_pocketbase(
    app_client: AsyncClient, admin_token: str, sent_mails
) -> None:
    admin = await _create_admin(app_client, admin_token)
    await app_client.post("/api/admins/request-password-reset", json={"email": admin["email"]})
    token = sent_mails[-1]["token"]
    length = {
        "code": "validation_length_out_of_range",
        "message": "The length must be between 10 and 72.",
    }
    cases = [
        ({}, {"token": BLANK, "password": BLANK, "passwordConfirm": BLANK}),
        (
            {
                "token": "garbage",
                "password": "new-password-123",
                "passwordConfirm": "new-password-123",
            },
            {"token": INVALID_TOKEN},
        ),
        # A session token is signed with the session secret, not the reset secret.
        (
            {
                "token": admin["token"],
                "password": "new-password-123",
                "passwordConfirm": "new-password-123",
            },
            {"token": INVALID_TOKEN},
        ),
        ({"token": token, "password": "short", "passwordConfirm": "short"}, {"password": length}),
        ({"token": token, "password": "x" * 73, "passwordConfirm": "x" * 73}, {"password": length}),
        (
            {"token": token, "password": "new-password-123", "passwordConfirm": "other-password-1"},
            {
                "passwordConfirm": {
                    "code": "validation_values_mismatch",
                    "message": "Values don't match.",
                }
            },
        ),
    ]
    for body, errors in cases:
        resp = await app_client.post("/api/admins/confirm-password-reset", json=body)
        assert resp.status_code == 400, resp.text
        assert _error(resp) == (GENERIC, errors)
    # Nothing changed.
    assert (
        await app_client.get("/api/admins", headers={"Authorization": admin["token"]})
    ).status_code == 200


async def test_confirm_resets_the_password_and_revokes_sessions(
    app_client: AsyncClient, admin_token: str, sent_mails
) -> None:
    admin = await _create_admin(app_client, admin_token)
    await app_client.post("/api/admins/request-password-reset", json={"email": admin["email"]})
    token = sent_mails[-1]["token"]
    before = await _row(admin["id"])

    resp = await app_client.post(
        "/api/admins/confirm-password-reset",
        json={
            "token": token,
            "password": "new-password-123",
            "passwordConfirm": "new-password-123",
        },
    )
    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    after = await _row(admin["id"])
    assert after["token_key"] != before["token_key"]
    # Admin.SetPassword clears the resend throttle (models/admin.go:42-61).
    assert after["last_reset_sent_at"] is None

    old_session = await app_client.get("/api/admins", headers={"Authorization": admin["token"]})
    assert old_session.status_code == 401
    old_password = await app_client.post(
        "/api/admins/auth-with-password",
        json={"identity": admin["email"], "password": ADMIN_PASSWORD},
    )
    assert old_password.status_code == 400
    new_password = await app_client.post(
        "/api/admins/auth-with-password",
        json={"identity": admin["email"], "password": "new-password-123"},
    )
    assert new_password.status_code == 200, new_password.text

    reused = await app_client.post(
        "/api/admins/confirm-password-reset",
        json={
            "token": token,
            "password": "new-password-456",
            "passwordConfirm": "new-password-456",
        },
    )
    assert reused.status_code == 400
    assert _error(reused) == (GENERIC, {"token": INVALID_TOKEN})

    # The cleared throttle lets a new request through, as measured on 0.22.34.
    request = await app_client.post(
        "/api/admins/request-password-reset", json={"email": admin["email"]}
    )
    assert request.status_code == 204
    assert len(sent_mails) == 2


async def test_link_emailed_by_pocketbase_022_before_the_migration_works(
    app_client: AsyncClient, admin_token: str
) -> None:
    """tokens/admin.go:19-26: exp, id, type=admin, email; tokenKey + reset secret."""
    admin = await _create_admin(app_client, admin_token)
    row = await _row(admin["id"])
    token = pyjwt.encode(
        {
            "exp": int(time.time()) + 1800,
            "id": admin["id"],
            "type": "admin",
            "email": admin["email"],
        },
        row["token_key"] + await _reset_secret(),
        algorithm="HS256",
    )
    resp = await app_client.post(
        "/api/admins/confirm-password-reset",
        json={
            "token": token,
            "password": "migrated-pass-123",
            "passwordConfirm": "migrated-pass-123",
        },
    )
    assert resp.status_code == 204, resp.text
    login = await app_client.post(
        "/api/admins/auth-with-password",
        json={"identity": admin["email"], "password": "migrated-pass-123"},
    )
    assert login.status_code == 200, login.text


async def test_password_change_by_patch_also_clears_the_throttle(
    app_client: AsyncClient, admin_token: str, sent_mails
) -> None:
    admin = await _create_admin(app_client, admin_token)
    await app_client.post("/api/admins/request-password-reset", json={"email": admin["email"]})
    assert (await _row(admin["id"]))["last_reset_sent_at"] is not None
    patched = await app_client.patch(
        f"/api/admins/{admin['id']}",
        headers={"Authorization": admin_token},
        json={"password": "patched-pass-123"},
    )
    assert patched.status_code == 200, patched.text
    assert (await _row(admin["id"]))["last_reset_sent_at"] is None
