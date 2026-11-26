"""PocketBase (>= 0.23) record auth tokens remain valid after a migration.

PocketBase signs record tokens with ``type: "auth"`` and the key
``token_key + collection authToken secret``. A database migrated with its
token keys and collection secrets must keep these sessions working.
"""

from __future__ import annotations

import time
import uuid

import jwt as pyjwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def _create_user(app_client: AsyncClient, collection: str) -> dict:
    email = f"pbtok_{uuid.uuid4().hex[:8]}@example.com"
    resp = await app_client.post(
        f"/api/collections/{collection}/records",
        json={"email": email, "password": "securepass123", "passwordConfirm": "securepass123"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _signing_material(admin_token: str, app_client: AsyncClient, collection: str, record_id: str):
    from ppbase.db.engine import get_engine

    resp = await app_client.get(f"/api/collections/{collection}", headers={"Authorization": admin_token})
    assert resp.status_code == 200, resp.text
    async with get_engine().connect() as conn:
        coll = (await conn.execute(
            text("SELECT id, options FROM _collections WHERE name = :n"), {"n": collection}
        )).mappings().one()
        token_key = (await conn.execute(
            text(f'SELECT token_key FROM "{collection}" WHERE id = :i'), {"i": record_id}
        )).scalar_one()
    return coll["id"], token_key + coll["options"]["authToken"]["secret"]


def _pocketbase_token(record_id: str, collection_id: str, secret: str, **extra) -> str:
    now = int(time.time())
    claims = {"collectionId": collection_id, "exp": now + 3600, "id": record_id,
              "refreshable": True, "type": "auth", **extra}
    return pyjwt.encode(claims, secret, algorithm="HS256")


async def _auth_collection(app_client: AsyncClient, admin_token: str) -> str:
    name = f"agents_{uuid.uuid4().hex[:8]}"
    resp = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={"name": name, "type": "auth", "schema": [], "createRule": "", "listRule": None,
              "viewRule": "id = @request.auth.id", "updateRule": None, "deleteRule": None},
    )
    assert resp.status_code == 200, resp.text
    return name


async def test_pocketbase_token_authenticates_requests(app_client: AsyncClient, admin_token: str):
    collection = await _auth_collection(app_client, admin_token)
    user = await _create_user(app_client, collection)
    collection_id, secret = await _signing_material(admin_token, app_client, collection, user["id"])
    token = _pocketbase_token(user["id"], collection_id, secret)

    anonymous = await app_client.get(f"/api/collections/{collection}/records/{user['id']}")
    resp = await app_client.get(
        f"/api/collections/{collection}/records/{user['id']}", headers={"Authorization": token}
    )
    assert anonymous.status_code == 404
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == user["id"]


async def test_pocketbase_token_refreshes_into_ppbase_token(app_client: AsyncClient, admin_token: str):
    collection = await _auth_collection(app_client, admin_token)
    user = await _create_user(app_client, collection)
    collection_id, secret = await _signing_material(admin_token, app_client, collection, user["id"])
    token = _pocketbase_token(user["id"], collection_id, secret)

    resp = await app_client.post(
        f"/api/collections/{collection}/auth-refresh", headers={"Authorization": token}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["record"]["id"] == user["id"]
    claims = pyjwt.decode(body["token"], options={"verify_signature": False})
    assert claims["type"] == "authRecord"

    again = await app_client.get(
        f"/api/collections/{collection}/records/{user['id']}", headers={"Authorization": body["token"]}
    )
    assert again.status_code == 200, again.text


async def test_pocketbase_token_with_wrong_secret_is_rejected(app_client: AsyncClient, admin_token: str):
    collection = await _auth_collection(app_client, admin_token)
    user = await _create_user(app_client, collection)
    collection_id, _ = await _signing_material(admin_token, app_client, collection, user["id"])
    forged = _pocketbase_token(user["id"], collection_id, "attacker-secret-that-is-long-enough-for-hs256")

    view = await app_client.get(
        f"/api/collections/{collection}/records/{user['id']}", headers={"Authorization": forged}
    )
    refresh = await app_client.post(
        f"/api/collections/{collection}/auth-refresh", headers={"Authorization": forged}
    )
    assert view.status_code == 404
    assert refresh.status_code == 401


async def test_pocketbase_token_is_bound_to_its_collection(app_client: AsyncClient, admin_token: str):
    collection_a = await _auth_collection(app_client, admin_token)
    collection_b = await _auth_collection(app_client, admin_token)
    user = await _create_user(app_client, collection_a)
    collection_id, secret = await _signing_material(admin_token, app_client, collection_a, user["id"])
    token = _pocketbase_token(user["id"], collection_id, secret)

    resp = await app_client.post(
        f"/api/collections/{collection_b}/auth-refresh", headers={"Authorization": token}
    )
    assert resp.status_code == 401


async def test_pocketbase_token_after_password_change_is_rejected(app_client: AsyncClient, admin_token: str):
    """A rotated token_key (password change) still invalidates old PocketBase tokens."""
    from ppbase.db.engine import get_engine

    collection = await _auth_collection(app_client, admin_token)
    user = await _create_user(app_client, collection)
    collection_id, secret = await _signing_material(admin_token, app_client, collection, user["id"])
    token = _pocketbase_token(user["id"], collection_id, secret)
    async with get_engine().begin() as conn:
        await conn.execute(text(f'UPDATE "{collection}" SET token_key = :k WHERE id = :i'),
                           {"k": uuid.uuid4().hex, "i": user["id"]})

    resp = await app_client.post(
        f"/api/collections/{collection}/auth-refresh", headers={"Authorization": token}
    )
    assert resp.status_code == 401
