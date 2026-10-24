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


# ---------------------------------------------------------------------------
# Measured on 0.22.34: byte lengths, e-mail format and request bodies
# ---------------------------------------------------------------------------

LENGTH_10_72 = {
    "code": "validation_length_out_of_range",
    "message": "The length must be between 10 and 72.",
}
IS_EMAIL = {"code": "validation_is_email", "message": "Must be a valid email address."}
LOADING = "An error occurred while loading the submitted data."


async def _legacy_reset_token(admin: dict[str, Any]) -> str:
    """A reset link of PocketBase 0.22.34 (tokens/admin.go:19-26) for the current token key."""
    row = await _row(admin["id"])
    claims = {
        "exp": int(time.time()) + 600,
        "id": admin["id"],
        "type": "admin",
        "email": admin["email"],
    }
    return pyjwt.encode(claims, row["token_key"] + await _reset_secret(), algorithm="HS256")


async def test_confirm_password_length_counts_bytes_like_pocketbase(
    app_client: AsyncClient, admin_token: str
) -> None:
    """forms/admin_password_reset_confirm.go:45 Length(10, 72) counts bytes.

    Measured on 0.22.34 with a valid 0.22 token and with an invalid one:
    five two-byte characters (10 bytes) pass, 37 (74 bytes) fail; the former
    PPBase rule counted characters, refused the first and answered 500 (bcrypt
    limit) for the second.
    """
    admin = await _create_admin(app_client, admin_token)
    cases = [
        ("é" * 4 + "a", False),  # 9 bytes
        ("é" * 5, True),  # 10 bytes
        ("é" * 9, True),
        ("é" * 36, True),  # 72 bytes
        ("é" * 37, False),  # 74 bytes
        ("é" * 40, False),
        ("a" * 9, False),
        ("a" * 10, True),
        ("a" * 72, True),
        ("a" * 73, False),
        ("\U0001f600" * 18, True),  # 72 bytes
        ("\U0001f600" * 19, False),  # 76 bytes
    ]
    for password, valid in cases:
        garbage = await app_client.post(
            "/api/admins/confirm-password-reset",
            json={"token": "garbage", "password": password, "passwordConfirm": password},
        )
        expected = (
            {"token": INVALID_TOKEN}
            if valid
            else {"password": LENGTH_10_72, "token": INVALID_TOKEN}
        )
        assert garbage.status_code == 400, garbage.text
        assert _error(garbage) == (GENERIC, expected)

        resp = await app_client.post(
            "/api/admins/confirm-password-reset",
            json={
                "token": await _legacy_reset_token(admin),
                "password": password,
                "passwordConfirm": password,
            },
        )
        if valid:
            assert resp.status_code == 204, resp.text
            login = await app_client.post(
                "/api/admins/auth-with-password",
                json={"identity": admin["email"], "password": password},
            )
            assert login.status_code == 200, login.text
        else:
            assert resp.status_code == 400, resp.text
            assert _error(resp) == (GENERIC, {"password": LENGTH_10_72})


async def test_confirm_reads_lone_surrogates_like_go(
    app_client: AsyncClient, admin_token: str
) -> None:
    """Go's JSON decoder reads each lone surrogate escape as U+FFFD (3 bytes).

    Measured on 0.22.34: ten of them (30 bytes) are a valid password and the
    same JSON then signs in; three plus "a" (10 bytes) pass the length rule.
    """
    admin = await _create_admin(app_client, admin_token)
    lone = b'"' + b"\\ud800" * 10 + b'"'
    token = (await _legacy_reset_token(admin)).encode()
    body = (
        b'{"token": "' + token + b'", "password": ' + lone + b', "passwordConfirm": ' + lone + b"}"
    )
    resp = await app_client.post(
        "/api/admins/confirm-password-reset",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 204, resp.text
    login = await app_client.post(
        "/api/admins/auth-with-password",
        content=b'{"identity": "' + admin["email"].encode() + b'", "password": ' + lone + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert login.status_code == 200, login.text

    short = b'"' + b"\\ud800" * 3 + b'a"'
    garbage = b'{"token": "garbage", "password": ' + short + b', "passwordConfirm": ' + short + b"}"
    resp = await app_client.post(
        "/api/admins/confirm-password-reset",
        content=garbage,
        headers={"Content-Type": "application/json"},
    )
    assert _error(resp) == (GENERIC, {"token": INVALID_TOKEN})


async def test_request_email_format_is_govalidator(app_client: AsyncClient, sent_mails) -> None:
    """is.EmailFormat (forms/admin_password_reset_request.go:53), measured on 0.22.34.

    The three addresses of the review were accepted by the former PPBase
    pattern; govalidator also accepts a few odd quoted local parts.
    """
    for email in (
        "someone@[127.0.0.1]",
        "us..er@example.com",
        ".user@example.com",
        '"john"@c11.test',
    ):
        resp = await app_client.post("/api/admins/request-password-reset", json={"email": email})
        assert resp.status_code == 400, (email, resp.text)
        assert _error(resp) == (VALIDATE_FORM, {"email": IS_EMAIL})
    for email in ('"j"@c11.test', "user@c11.test.", "١@c11.test", "a" * 65 + "@c11.test"):
        resp = await app_client.post("/api/admins/request-password-reset", json={"email": email})
        assert resp.status_code == 204, (email, resp.text)
    assert sent_mails == []


async def test_bodies_are_bound_like_pocketbase(
    app_client: AsyncClient, admin_token: str, sent_mails
) -> None:
    """c.Bind of 0.22.34 reads JSON, urlencoded and multipart bodies (measured)."""
    admin = await _create_admin(app_client, admin_token)
    path = "/api/admins/request-password-reset"
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    for kwargs, status in (
        ({"content": b"email=nobody%40c11.test", "headers": form}, 204),
        ({"files": {"email": (None, "nobody@c11.test")}}, 204),
        ({"content": b"EMAIL=nobody%40c11.test", "headers": form}, 204),
        ({"json": {"EMAIL": "nobody@c11.test"}}, 204),
        (
            {"content": b'{"email": "nobody@c11.test"}', "headers": {"Content-Type": "text/plain"}},
            400,
        ),
        ({"content": b"x=1", "headers": form, "params": {"email": "nobody@c11.test"}}, 204),
        ({"json": {}, "params": {"email": "nobody@c11.test"}}, 400),
    ):
        resp = await app_client.post(path, **kwargs)
        assert resp.status_code == status, (kwargs, resp.text)
    text_plain = await app_client.post(
        path, content=b'{"email": "a@b.co"}', headers={"Content-Type": "text/plain"}
    )
    assert _error(text_plain) == (LOADING, {})
    assert sent_mails == []

    # A urlencoded and a multipart confirmation both reset the password.
    token = await _legacy_reset_token(admin)
    body = f"token={token}&password=form-password-1&passwordConfirm=form-password-1".encode()
    resp = await app_client.post("/api/admins/confirm-password-reset", content=body, headers=form)
    assert resp.status_code == 204, resp.text
    resp = await app_client.post(
        "/api/admins/confirm-password-reset",
        files={
            "token": (None, await _legacy_reset_token(admin)),
            "password": (None, "multipart-pass-1"),
            "passwordConfirm": (None, "multipart-pass-1"),
        },
    )
    assert resp.status_code == 204, resp.text
    login = await app_client.post(
        "/api/admins/auth-with-password",
        json={"identity": admin["email"], "password": "multipart-pass-1"},
    )
    assert login.status_code == 200, login.text


async def test_reset_email_refreshes_updated(
    app_client: AsyncClient, admin_token: str, sent_mails
) -> None:
    """SaveAdmin refreshes ``updated`` with ``lastResetSentAt`` (measured on 0.22.34)."""
    from ppbase.db.engine import get_engine

    admin = await _create_admin(app_client, admin_token)

    async def updated_and_sent() -> Any:
        async with get_engine().connect() as conn:
            result = await conn.execute(
                text("SELECT updated, last_reset_sent_at FROM _superusers WHERE id = :id"),
                {"id": admin["id"]},
            )
            return result.mappings().first()

    before = await updated_and_sent()
    resp = await app_client.post(
        "/api/admins/request-password-reset", json={"email": admin["email"]}
    )
    assert resp.status_code == 204
    assert len(sent_mails) == 1
    after = await updated_and_sent()
    assert after["last_reset_sent_at"] is not None
    assert after["updated"] == after["last_reset_sent_at"]
    assert after["updated"] > before["updated"]
    listed = await app_client.get(
        f"/api/admins/{admin['id']}", headers={"Authorization": admin_token}
    )
    assert listed.json()["updated"] != listed.json()["created"]
