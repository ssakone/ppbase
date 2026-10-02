"""Client ``filter``/``sort``/``fields`` cannot reach auth secrets or hidden fields (PocketBase parity).

Without the guard, a user allowed to list an auth collection could recover password hashes and token
keys with ``password_hash ~ '$2'`` style filters; unknown fields produced a 500 instead of a 400.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

PASSWORD = "secret12345"


@pytest.mark.asyncio
async def test_client_queries_cannot_reach_secrets_or_hidden_fields(
    app_client: AsyncClient, admin_token: str
) -> None:
    s = uuid.uuid4().hex[:8]
    users = (await app_client.get("/api/collections/users", headers={"Authorization": admin_token})).json()
    notes = f"notes_{s}"
    response = await app_client.post("/api/collections", headers={"Authorization": admin_token}, json={
        "name": notes, "type": "base",
        "schema": [
            {"name": "title", "type": "text"},
            {"name": "secret", "type": "text", "hidden": True},
            {"name": "owner", "type": "relation", "options": {"collectionId": users["id"], "maxSelect": 1}},
        ],
        "listRule": "@request.auth.id != ''", "viewRule": "@request.auth.id != ''",
    })
    assert response.status_code == 200, response.text

    email = f"reader_{s}@test.com"
    created = await app_client.post("/api/collections/users/records", json={
        "email": email, "password": PASSWORD, "passwordConfirm": PASSWORD,
    })
    assert created.status_code == 200, created.text
    login = await app_client.post("/api/collections/users/auth-with-password",
                                  json={"identity": email, "password": PASSWORD})
    token = login.json()["token"]
    await app_client.post(f"/api/collections/{notes}/records", headers={"Authorization": admin_token},
                          json={"title": "t", "secret": "s3cr3t", "owner": created.json()["id"]})

    async def status(params: dict, auth: str = token, collection: str = notes) -> int:
        r = await app_client.get(f"/api/collections/{collection}/records",
                                 headers={"Authorization": auth}, params=params)
        return r.status_code

    # Auth secrets: never, not even for superusers.
    for params in ({"filter": "owner.password_hash ~ '$2'"}, {"filter": "owner.token_key != ''"},
                   {"filter": f"@collection.users.password_hash ~ '$2'"}, {"sort": "owner.token_key"}):
        assert await status(params) == 400, params
        assert await status(params, admin_token) == 400, params
    assert await status({"filter": "password_hash ~ '$2'"}, collection="users") == 400
    # Hidden field: superusers only.
    assert await status({"filter": "secret = 's3cr3t'"}) == 400
    assert await status({"sort": "secret"}) == 400
    assert await status({"filter": "secret = 's3cr3t'"}, admin_token) == 200
    # Unknown field: 400, not 500.
    assert await status({"filter": "nope = 1"}) == 400
    # Legitimate queries still work, through relations too.
    assert await status({"filter": "title = 't' && owner.email != ''", "sort": "-created,title"}) == 200

    # `fields` never returns a hidden field to a non-superuser, even when requested.
    r = await app_client.get(f"/api/collections/{notes}/records", headers={"Authorization": token},
                             params={"fields": "id,title,secret"})
    assert "secret" not in r.json()["items"][0]
    r = await app_client.get(f"/api/collections/{notes}/records", headers={"Authorization": admin_token},
                             params={"fields": "id,title,secret"})
    assert r.json()["items"][0]["secret"] == "s3cr3t"
