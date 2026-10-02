"""``@request.auth.<field>`` resolves against the authenticated record, like PocketBase.

Covers: ``collectionName``, plain auth record fields (bool, relation id), relation traversal
(``@request.auth.role.permissions`` on JSON, ``@request.auth.org.id``), fields missing on the
current auth collection (must be falsy, never a 500), and ``@request.body.<relation>.<field>``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

PASSWORD = "secret12345"


async def _create_collection(app_client: AsyncClient, admin_token: str, body: dict) -> dict:
    response = await app_client.post(
        "/api/collections", headers={"Authorization": admin_token}, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create(app_client: AsyncClient, token: str, collection: str, data: dict) -> dict:
    response = await app_client.post(
        f"/api/collections/{collection}/records", headers={"Authorization": token}, json=data
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _login(app_client: AsyncClient, collection: str, email: str) -> str:
    response = await app_client.post(
        f"/api/collections/{collection}/auth-with-password",
        json={"identity": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _titles(app_client: AsyncClient, token: str, collection: str) -> set[str]:
    response = await app_client.get(
        f"/api/collections/{collection}/records", headers={"Authorization": token}
    )
    assert response.status_code == 200, response.text
    return {item["title"] for item in response.json()["items"]}


@pytest.mark.asyncio
async def test_auth_record_fields_and_relations_in_rules(
    app_client: AsyncClient, admin_token: str
) -> None:
    s = uuid.uuid4().hex[:8]
    orgs = await _create_collection(app_client, admin_token, {
        "name": f"orgs_{s}", "type": "base",
        "schema": [{"name": "name", "type": "text"}],
    })
    roles = await _create_collection(app_client, admin_token, {
        "name": f"roles_{s}", "type": "base",
        "schema": [
            {"name": "system_key", "type": "text"},
            {"name": "permissions", "type": "json"},
        ],
    })
    members = f"members_{s}"
    await _create_collection(app_client, admin_token, {
        "name": members, "type": "auth",
        "schema": [
            {"name": "active", "type": "bool"},
            {"name": "org", "type": "relation",
             "options": {"collectionId": orgs["id"], "maxSelect": 1}},
            {"name": "role", "type": "relation",
             "options": {"collectionId": roles["id"], "maxSelect": 1}},
        ],
        "options": {"authRule": ""},
    })
    access = (
        f"@request.auth.collectionName = '{members}' && @request.auth.active = true"
        " && (@request.auth.role.system_key = 'owner' || @request.auth.role.permissions ~ 'docs.view')"
        " && @request.auth.org.id = org.id"
    )
    docs = f"docs_{s}"
    await _create_collection(app_client, admin_token, {
        "name": docs, "type": "base",
        "schema": [
            {"name": "title", "type": "text"},
            {"name": "org", "type": "relation",
             "options": {"collectionId": orgs["id"], "maxSelect": 1}},
        ],
        "listRule": access, "viewRule": access,
    })

    org1 = await _create(app_client, admin_token, orgs["name"], {"name": "o1"})
    org2 = await _create(app_client, admin_token, orgs["name"], {"name": "o2"})
    owner = await _create(app_client, admin_token, roles["name"], {"system_key": "owner", "permissions": []})
    viewer = await _create(app_client, admin_token, roles["name"], {"permissions": ["docs.view"]})
    nobody = await _create(app_client, admin_token, roles["name"], {"permissions": ["other"]})
    for title, org in (("d1", org1), ("d2", org2)):
        await _create(app_client, admin_token, docs, {"title": title, "org": org["id"]})

    async def member(name: str, org: dict, role: dict, active: bool = True) -> str:
        email = f"{name}_{s}@test.com"
        await _create(app_client, admin_token, members, {
            "email": email, "password": PASSWORD, "passwordConfirm": PASSWORD,
            "active": active, "org": org["id"], "role": role["id"],
        })
        return await _login(app_client, members, email)

    assert await _titles(app_client, await member("owner1", org1, owner), docs) == {"d1"}
    assert await _titles(app_client, await member("owner2", org2, owner), docs) == {"d2"}
    assert await _titles(app_client, await member("viewer1", org1, viewer), docs) == {"d1"}
    assert await _titles(app_client, await member("nobody1", org1, nobody), docs) == set()
    assert await _titles(app_client, await member("off1", org1, owner, active=False), docs) == set()

    # A token from another auth collection lacks `active`, `org`, `role`: empty result, no 500.
    email = f"plain_{s}@test.com"
    await _create(app_client, admin_token, "users", {
        "email": email, "password": PASSWORD, "passwordConfirm": PASSWORD,
    })
    assert await _titles(app_client, await _login(app_client, "users", email), docs) == set()


@pytest.mark.asyncio
async def test_body_relation_traversal_in_create_rule(
    app_client: AsyncClient, admin_token: str
) -> None:
    s = uuid.uuid4().hex[:8]
    users = await app_client.get("/api/collections/users", headers={"Authorization": admin_token})
    users_id = users.json()["id"]
    folders = await _create_collection(app_client, admin_token, {
        "name": f"folders_{s}", "type": "base",
        "schema": [
            {"name": "title", "type": "text"},
            {"name": "owner", "type": "relation",
             "options": {"collectionId": users_id, "maxSelect": 1}},
        ],
    })
    items = f"items_{s}"
    await _create_collection(app_client, admin_token, {
        "name": items, "type": "base",
        "schema": [
            {"name": "title", "type": "text"},
            {"name": "folder", "type": "relation",
             "options": {"collectionId": folders["id"], "maxSelect": 1}},
        ],
        "createRule": "@request.body.folder != '' && @request.body.folder.owner.id = @request.auth.id",
    })

    tokens = {}
    ids = {}
    for name in ("alice", "bob"):
        email = f"{name}_{s}@test.com"
        record = await _create(app_client, admin_token, "users", {
            "email": email, "password": PASSWORD, "passwordConfirm": PASSWORD,
        })
        ids[name] = record["id"]
        tokens[name] = await _login(app_client, "users", email)
    folder = await _create(app_client, admin_token, folders["name"], {"title": "f", "owner": ids["alice"]})

    ok = await app_client.post(f"/api/collections/{items}/records",
                               headers={"Authorization": tokens["alice"]},
                               json={"title": "i", "folder": folder["id"]})
    assert ok.status_code == 200, ok.text
    denied = await app_client.post(f"/api/collections/{items}/records",
                                   headers={"Authorization": tokens["bob"]},
                                   json={"title": "i", "folder": folder["id"]})
    assert denied.status_code in (400, 403), denied.text
