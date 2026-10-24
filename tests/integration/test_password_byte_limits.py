"""Passwords over bcrypt's 72 bytes never answer 500.

bcrypt 5 refuses to hash more than 72 bytes (``ValueError``), which a password
of at most 71 characters reaches with multi-byte characters.  Expected answers
for the PocketBase 0.22 routes were measured on the official 0.22.34 binary
(synthetic accounts): its admin form applies ``Length(10, 72)`` in bytes
(forms/admin_upsert.go:75-79) and its record reset accepts up to 100 bytes
(forms/record_password_reset_confirm.go:48), then fails in ``SetPassword``
with an empty error (``400``, ``data: {}``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

GENERIC = "Something went wrong while processing your request."
LENGTH_10_72 = {
    "code": "validation_length_out_of_range",
    "message": "The length must be between 10 and 72.",
}
LONG = "é" * 37  # 37 characters, 74 bytes


def _error(response) -> tuple[str, dict[str, Any]]:
    body = response.json()
    return body["message"], body["data"]


async def test_admin_create_and_update_count_bytes(
    app_client: AsyncClient, admin_token: str
) -> None:
    auth = {"Authorization": admin_token}

    def email() -> str:
        return f"bytes_{uuid.uuid4().hex[:10]}@example.com"

    for password in (LONG, "a" * 9):
        resp = await app_client.post(
            "/api/admins", headers=auth, json={"email": email(), "password": password}
        )
        assert resp.status_code == 400, resp.text
        assert _error(resp) == (GENERIC, {"password": LENGTH_10_72})
    created = await app_client.post(
        "/api/admins", headers=auth, json={"email": email(), "password": "é" * 5}
    )
    assert created.status_code == 200, created.text
    admin_id = created.json()["id"]

    for password in (LONG, "short"):
        resp = await app_client.patch(
            f"/api/admins/{admin_id}", headers=auth, json={"password": password}
        )
        assert resp.status_code == 400, resp.text
        assert _error(resp) == (GENERIC, {"password": LENGTH_10_72})
    # An unknown admin is a 404 first, as in 0.22.34.
    missing = await app_client.patch(
        "/api/admins/unknownid123456", headers=auth, json={"password": LONG}
    )
    assert missing.status_code == 404, missing.text
    patched = await app_client.patch(
        f"/api/admins/{admin_id}", headers=auth, json={"password": "é" * 6}
    )
    assert patched.status_code == 200, patched.text
    login = await app_client.post(
        "/api/admins/auth-with-password",
        json={"identity": created.json()["email"], "password": "é" * 6},
    )
    assert login.status_code == 200, login.text
