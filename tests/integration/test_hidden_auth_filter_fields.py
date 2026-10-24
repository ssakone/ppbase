"""Hidden auth fields must not be filterable/sortable (PocketBase 0.22.34).

PocketBase resolves only base-model fields, the schema fields and, for auth
collections, ``username``/``verified``/``emailVisibility``/``email``
(``resolvableSystemFieldNames``, resolvers/record_field_resolve_runner.go
l.718-732).  A filter/sort on ``password``, ``passwordHash``/``password_hash``,
``tokenKey``/``token_key`` or ``email_visibility`` is an "unknown field" 400
for every caller, so the hidden bcrypt hash, token key and masked emails can
not be probed.  ``email`` resolves but, for a non-admin list or a realtime
client filter, is restricted to ``emailVisibility = TRUE`` (AfterBuild guard,
l.356-374).  The values here are paired with the 0.22.34 binary by
tests/pocketbase_parity/hidden_fields_probe.py.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from ppbase.db.engine import get_engine
from ppbase.services.realtime_service import (
    SubscriptionManager,
    broadcast_record_change,
)
from ppbase.services.record_service import resolve_collection

pytestmark = pytest.mark.asyncio

_OPAQUE_400 = "Something went wrong while processing your request."
_HIDDEN_FIELDS = (
    "password",
    "passwordHash",
    "password_hash",
    "tokenKey",
    "token_key",
    "email_visibility",
    "lastResetSentAt",
)


async def _admin(app_client: AsyncClient, admin_token: str, body: dict) -> dict:
    response = await app_client.post(
        "/api/collections", headers={"Authorization": admin_token}, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create(
    app_client: AsyncClient, name: str, payload: dict, token: str
) -> dict:
    response = await app_client.post(
        f"/api/collections/{name}/records",
        headers={"Authorization": token},
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _names(app_client: AsyncClient, name: str, token: str, **params) -> list[str]:
    response = await app_client.get(
        f"/api/collections/{name}/records",
        headers={"Authorization": token} if token else {},
        params={"perPage": 200, "sort": "name", **params},
    )
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


@pytest.fixture(scope="module")
async def hidden_env(app_client: AsyncClient, admin_token: str) -> dict:
    suffix = uuid.uuid4().hex[:8]
    users = await _admin(
        app_client,
        admin_token,
        {
            "name": f"hf_users_{suffix}",
            "type": "auth",
            "schema": [{"name": "name", "type": "text"}],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "options": {"allowUsernameAuth": True, "minPasswordLength": 6},
        },
    )
    name = users["name"]
    ids: dict[str, str] = {}
    pwd = "hidden-Pw-123456"
    for user, visible in (("vis", True), ("hid", False)):
        record = await _create(
            app_client,
            name,
            {
                "email": f"{user}_{suffix}@example.invalid",
                "emailVisibility": visible,
                "password": pwd,
                "passwordConfirm": pwd,
                "name": user,
            },
            admin_token,
        )
        ids[user] = record["id"]
    user_token = (
        await app_client.post(
            f"/api/collections/{name}/auth-with-password",
            json={"identity": f"hid_{suffix}@example.invalid", "password": pwd},
        )
    ).json()["token"]
    return {
        "name": name,
        "id": users["id"],
        "ids": ids,
        "admin": admin_token,
        "user": user_token,
        "suffix": suffix,
    }


async def test_hidden_fields_are_not_filterable_by_anyone(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    name = hidden_env["name"]
    for token in ("", hidden_env["user"], hidden_env["admin"]):
        headers = {"Authorization": token} if token else {}
        for field in _HIDDEN_FIELDS:
            response = await app_client.get(
                f"/api/collections/{name}/records",
                headers=headers,
                params={"filter": f'{field} != ""'},
            )
            assert response.status_code == 400, (field, response.text)
            body = response.json()
            assert body["message"] == _OPAQUE_400
            assert body["data"] == {}
        # ``:length`` on a hidden field is rejected too.
        response = await app_client.get(
            f"/api/collections/{name}/records",
            headers=headers,
            params={"filter": "token_key:length > 0"},
        )
        assert response.status_code == 400


async def test_hidden_fields_are_not_sortable_by_anyone(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    name = hidden_env["name"]
    for token in ("", hidden_env["user"], hidden_env["admin"]):
        headers = {"Authorization": token} if token else {}
        for field in ("token_key", "tokenKey", "password_hash", "email_visibility"):
            response = await app_client.get(
                f"/api/collections/{name}/records",
                headers=headers,
                params={"sort": field},
            )
            assert response.status_code == 400, (field, response.text)
            assert response.json()["message"] == _OPAQUE_400


async def test_email_filter_is_restricted_to_visible_emails_for_non_admins(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    name, suffix = hidden_env["name"], hidden_env["suffix"]
    # Non-admin: only emailVisibility=TRUE records answer an email filter.
    for token in ("", hidden_env["user"]):
        assert await _names(app_client, name, token, filter='email != ""') == ["vis"]
        assert (
            await _names(
                app_client, name, token, filter=f'email = "hid_{suffix}@example.invalid"'
            )
            == []
        )
        assert await _names(app_client, name, token, filter="emailVisibility = true") == [
            "vis"
        ]
    # Admin: no guard.
    assert sorted(
        await _names(app_client, name, hidden_env["admin"], filter='email != ""')
    ) == ["hid", "vis"]
    assert await _names(
        app_client, name, hidden_env["admin"], filter=f'email = "hid_{suffix}@example.invalid"'
    ) == ["hid"]


async def test_email_sort_resolves_without_a_visibility_guard(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    # Sorting by email is allowed for everyone and returns every record (the
    # guard is a filter AfterBuild, not applied to ORDER BY).
    for token in ("", hidden_env["user"], hidden_env["admin"]):
        assert sorted(await _names(app_client, hidden_env["name"], token, sort="email")) == [
            "hid",
            "vis",
        ]


async def _subscribe(manager, topic, auth_payload=None, **options):
    client = manager.register_client()
    await manager.add_subscription(
        client, topic, auth_payload=auth_payload, options_query=options or {}
    )
    return client


async def _received(manager, client) -> list[str]:
    session = manager.get_session(client)
    names: list[str] = []
    while session is not None and not session.response_queue.empty():
        event = await asyncio.wait_for(session.response_queue.get(), timeout=1.0)
        names.append(event["data"]["record"].get("name"))
    return sorted(names)


async def test_realtime_subscription_filters_follow_hidden_field_rules(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    name, ids, suffix = hidden_env["name"], hidden_env["ids"], hidden_env["suffix"]
    engine = get_engine()
    collection = await resolve_collection(engine, name)
    assert collection is not None
    manager = SubscriptionManager()
    auth = {"id": ids["hid"], "type": "authRecord", "collectionName": name}
    admin_auth = {"id": "admin", "type": "admin", "collectionName": "_superusers"}

    plain = await _subscribe(manager, f"{name}/*", auth, filter='name != ""')
    hidden = await _subscribe(manager, f"{name}/*", auth, filter='token_key != ""')
    email_vis = await _subscribe(
        manager, f"{name}/*", auth, filter=f'email = "vis_{suffix}@example.invalid"'
    )
    email_hid = await _subscribe(
        manager, f"{name}/*", auth, filter=f'email = "hid_{suffix}@example.invalid"'
    )
    coll_guest = await _subscribe(
        manager, f"{name}/*", auth, filter="@collection." + name + ".id ?= id"
    )
    coll_admin = await _subscribe(
        manager, f"{name}/*", admin_auth, filter="@collection." + name + ".id ?= id"
    )
    sort_only = await _subscribe(manager, f"{name}/*", auth, sort="@request.method")
    sort_req = await _subscribe(
        manager, f"{name}/*", auth, filter='name != ""', sort="@request.method"
    )

    for user in ("vis", "hid"):
        await broadcast_record_change(
            manager,
            name,
            ids[user],
            "update",
            {"id": ids[user], "name": user},
            engine=engine,
            collection=collection,
        )

    # name!="" matches both; a hidden-field filter denies every event.
    assert await _received(manager, plain) == ["hid", "vis"]
    assert await _received(manager, hidden) == []
    # The email client filter is restricted to the visible record.
    assert await _received(manager, email_vis) == ["vis"]
    assert await _received(manager, email_hid) == []
    # @collection in a client filter: denied for a record caller, allowed for
    # an admin.
    assert await _received(manager, coll_guest) == []
    assert await _received(manager, coll_admin) == ["hid", "vis"]
    # @request.* in sort alone (no filter) is not checked; with a filter it is.
    assert await _received(manager, sort_only) == ["hid", "vis"]
    assert await _received(manager, sort_req) == []
