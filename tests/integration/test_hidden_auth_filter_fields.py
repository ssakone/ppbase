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
                app_client,
                name,
                token,
                filter=f'email = "hid_{suffix}@example.invalid"',
            )
            == []
        )
        assert await _names(
            app_client, name, token, filter="emailVisibility = true"
        ) == ["vis"]
    # Admin: no guard.
    assert sorted(
        await _names(app_client, name, hidden_env["admin"], filter='email != ""')
    ) == ["hid", "vis"]
    assert await _names(
        app_client,
        name,
        hidden_env["admin"],
        filter=f'email = "hid_{suffix}@example.invalid"',
    ) == ["hid"]


async def test_email_sort_resolves_without_a_visibility_guard(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    # Sorting by email is allowed for everyone and returns every record (the
    # guard is a filter AfterBuild, not applied to ORDER BY).
    for token in ("", hidden_env["user"], hidden_env["admin"]):
        assert sorted(
            await _names(app_client, hidden_env["name"], token, sort="email")
        ) == [
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


async def _status(app_client: AsyncClient, name: str, token: str, **params) -> int:
    response = await app_client.get(
        f"/api/collections/{name}/records",
        headers={"Authorization": token} if token else {},
        params={"perPage": 200, **params},
    )
    if response.status_code == 400:
        assert response.json()["message"] == _OPAQUE_400, response.text
    return response.status_code


async def test_qualified_paths_and_modified_auth_fields_are_rejected(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    # A table-qualified self reference used to reach the raw column
    # (``users.password_hash`` -> 200 prefix oracle) and a modifier used to
    # drop the email visibility guard; PocketBase 0.22.34 answers 400.
    name, suffix = hidden_env["name"], hidden_env["suffix"]
    filters = (
        f'{name}.password_hash ~ "$2"',
        f'{name}.token_key != ""',
        f'{name}.email = "hid_{suffix}@example.invalid"',
        f'{name}.name != ""',
        f'email:lower = "hid_{suffix}@example.invalid"',
        'email:lower ~ "hid"',
        "email:length > 3",
        "email:isset = true",
        'email:each = "x"',
        "verified:isset = true",
        "id:length > 0",
        "password_hash.x = 1",
        'name.x = "a"',
        'username = "a"',  # no physical username column on this collection
    )
    for token in ("", hidden_env["user"], hidden_env["admin"]):
        for value in filters:
            assert await _status(app_client, name, token, filter=value) == 400, value
        assert await _status(app_client, name, token, sort="username") == 400
        assert await _status(app_client, name, token, sort=f"{name}.email") == 400
    # Another collection's table is not in FROM: 400, never a 500.
    other = await _admin(
        app_client,
        hidden_env["admin"],
        {
            "name": f"hf_other_{suffix}",
            "schema": [{"name": "name", "type": "text"}],
            "listRule": "",
        },
    )
    assert (
        await _status(
            app_client, other["name"], "", filter=f'{name}.password_hash != ""'
        )
        == 400
    )


async def test_realtime_email_modifier_filter_is_denied(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    name, ids = hidden_env["name"], hidden_env["ids"]
    engine = get_engine()
    collection = await resolve_collection(engine, name)
    manager = SubscriptionManager()
    guest = await _subscribe(manager, f"{name}/*", None, filter='email:lower ~ "hid"')
    qualified = await _subscribe(
        manager, f"{name}/*", None, filter=f'{name}.password_hash ~ "$2"'
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
    assert await _received(manager, guest) == []
    assert await _received(manager, qualified) == []


async def _emails(manager, client) -> dict[str, object]:
    session = manager.get_session(client)
    seen: dict[str, object] = {}
    while session is not None and not session.response_queue.empty():
        event = await asyncio.wait_for(session.response_queue.get(), timeout=1.0)
        record = event["data"]["record"]
        seen[record.get("name")] = record.get("email", "<absent>")
    return seen


async def test_realtime_payload_hides_masked_emails_like_pocketbase(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    # PocketBase 0.22.34 sends a hidden email only to the record owner whose
    # manageRule check passes (apis/realtime.go l.439-447): not to guests,
    # other users or admins, and not to the owner when manageRule is null.
    from ppbase.services.record_service import get_record

    name, ids, suffix = hidden_env["name"], hidden_env["ids"], hidden_env["suffix"]
    engine = get_engine()
    owner = {
        "id": ids["hid"],
        "type": "authRecord",
        "collectionName": name,
        "collectionId": hidden_env["id"],
    }
    admin_auth = {"id": "admin", "type": "admin"}

    async def round_(manage_rule):
        response = await app_client.patch(
            f"/api/collections/{name}",
            headers={"Authorization": hidden_env["admin"]},
            json={
                "options": {
                    "manageRule": manage_rule,
                    "allowUsernameAuth": True,
                    "minPasswordLength": 6,
                }
            },
        )
        assert response.status_code == 200, response.text
        collection = await resolve_collection(engine, name)
        manager = SubscriptionManager()
        clients = {
            "guest": await _subscribe(manager, f"{name}/*", None),
            "owner": await _subscribe(manager, f"{name}/*", owner),
            "admin": await _subscribe(manager, f"{name}/*", admin_auth),
            "owner_filtered": await _subscribe(
                manager, f"{name}/*", owner, filter='name != ""'
            ),
        }
        for user in ("vis", "hid"):
            await broadcast_record_change(
                manager,
                name,
                ids[user],
                "update",
                await get_record(engine, collection, ids[user]),
                engine=engine,
                collection=collection,
            )
        return {key: await _emails(manager, c) for key, c in clients.items()}

    visible = f"vis_{suffix}@example.invalid"
    hidden = f"hid_{suffix}@example.invalid"
    for rule in (None, "id = 'nobody'"):
        for key, seen in (await round_(rule)).items():
            assert seen == {"vis": visible, "hid": "<absent>"}, (rule, key, seen)
    allowed = await round_("id = @request.auth.id")
    for key in ("guest", "admin"):
        assert allowed[key] == {"vis": visible, "hid": "<absent>"}
    for key in ("owner", "owner_filtered"):
        assert allowed[key] == {"vis": visible, "hid": hidden}
    await round_(None)


async def test_email_visibility_and_verified_parse_like_cast_to_bool(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    # PocketBase loads emailVisibility/verified with cast.ToBool
    # (forms/record_upsert.go l.380-384): "false"/"0" are false, not truthy.
    name, suffix = hidden_env["name"], hidden_env["suffix"]
    admin = {"Authorization": hidden_env["admin"]}
    pwd = "hidden-Pw-123456"
    cases = (
        ("false", False),
        ("0", False),
        ("f", False),
        ("no", False),
        ("true", True),
        ("1", True),
        ("T", True),
    )
    for index, (raw, expected) in enumerate(cases):
        response = await app_client.post(
            f"/api/collections/{name}/records",
            headers=admin,
            data={
                "email": f"cast{index}_{suffix}@example.invalid",
                "emailVisibility": raw,
                "verified": raw,
                "password": pwd,
                "passwordConfirm": pwd,
                "name": f"cast{index}",
            },
            files={"avatar_unused": ("a.txt", b"x", "text/plain")},
        )
        assert response.status_code == 200, response.text
        record = response.json()
        assert record["emailVisibility"] is expected, raw
        assert record["verified"] is expected, raw
        # JSON strings follow the same rule, on update too.
        response = await app_client.patch(
            f"/api/collections/{name}/records/{record['id']}",
            headers=admin,
            json={"emailVisibility": raw, "verified": raw},
        )
        assert response.status_code == 200, response.text
        assert response.json()["emailVisibility"] is expected, raw
        assert response.json()["verified"] is expected, raw
        response = await app_client.patch(
            f"/api/collections/{name}/records/{record['id']}",
            headers=admin,
            data={"emailVisibility": raw},
            files={"avatar_unused": ("a.txt", b"x", "text/plain")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["emailVisibility"] is expected, raw
        await app_client.delete(
            f"/api/collections/{name}/records/{record['id']}", headers=admin
        )


async def test_expand_applies_the_target_view_rule(
    app_client: AsyncClient, hidden_env: dict
) -> None:
    # PocketBase expandFetch (apis/record_helpers.go l.177-210): a nil
    # viewRule is admin-only, any other rule filters the related records; a
    # relation with nothing visible has no expand key.
    admin, suffix = hidden_env["admin"], hidden_env["suffix"]
    secret = await _admin(
        app_client,
        admin,
        {
            "name": f"hf_secret_{suffix}",
            "schema": [{"name": "name", "type": "text"}],
            "listRule": None,
            "viewRule": None,
        },
    )
    guarded = await _admin(
        app_client,
        admin,
        {
            "name": f"hf_guarded_{suffix}",
            "schema": [{"name": "name", "type": "text"}],
            "listRule": "",
            "viewRule": '@request.auth.id != ""',
        },
    )
    posts = await _admin(
        app_client,
        admin,
        {
            "name": f"hf_posts_{suffix}",
            "schema": [
                {"name": "name", "type": "text"},
                {
                    "name": "secret",
                    "type": "relation",
                    "options": {"collectionId": secret["id"], "maxSelect": 1},
                },
                {
                    "name": "guarded",
                    "type": "relation",
                    "options": {"collectionId": guarded["id"], "maxSelect": 1},
                },
            ],
            "listRule": "",
            "viewRule": "",
        },
    )
    s1 = await _create(app_client, secret["name"], {"name": "s1"}, admin)
    g1 = await _create(app_client, guarded["name"], {"name": "g1"}, admin)
    await _create(
        app_client,
        posts["name"],
        {"name": "p1", "secret": s1["id"], "guarded": g1["id"]},
        admin,
    )
    await _create(app_client, posts["name"], {"name": "p2", "secret": s1["id"]}, admin)

    async def expanded(token: str) -> dict:
        response = await app_client.get(
            f"/api/collections/{posts['name']}/records",
            headers={"Authorization": token} if token else {},
            params={"expand": "secret,guarded", "sort": "name"},
        )
        assert response.status_code == 200, response.text
        return {
            item["name"]: sorted(item["expand"]) if "expand" in item else None
            for item in response.json()["items"]
        }

    assert await expanded("") == {"p1": None, "p2": None}
    assert await expanded(hidden_env["user"]) == {"p1": ["guarded"], "p2": None}
    assert await expanded(admin) == {"p1": ["guarded", "secret"], "p2": ["secret"]}
