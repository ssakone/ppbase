"""Collection API rules applied to every record read path.

Covers relation ``expand``, file downloads and ``@collection`` references in
caller supplied filters.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_PASSWORD = "rulespass123"


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


async def _create_collection(
    app_client: AsyncClient,
    admin_token: str,
    *,
    name: str,
    schema: list[dict[str, Any]],
    list_rule: str | None = "",
    view_rule: str | None = "",
) -> dict[str, Any]:
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": name,
            "type": "base",
            "schema": schema,
            "listRule": list_rule,
            "viewRule": view_rule,
            "createRule": None,
            "updateRule": None,
            "deleteRule": None,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _delete_collection(
    app_client: AsyncClient,
    admin_token: str,
    collection: dict[str, Any] | None,
) -> None:
    if collection is not None:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def _create_record(
    app_client: AsyncClient,
    admin_token: str,
    collection_name: str,
    *,
    json: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"headers": {"Authorization": admin_token}}
    if files is not None:
        kwargs["data"] = json or {}
        kwargs["files"] = files
    else:
        kwargs["json"] = json or {}
    response = await app_client.post(
        f"/api/collections/{collection_name}/records",
        **kwargs,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _user_token(app_client: AsyncClient) -> tuple[str, str]:
    email = f"rules_{_suffix()}@example.com"
    create = await app_client.post(
        "/api/collections/users/records",
        json={"email": email, "password": _PASSWORD, "passwordConfirm": _PASSWORD},
    )
    assert create.status_code == 200, create.text
    login = await app_client.post(
        "/api/collections/users/auth-with-password",
        json={"identity": email, "password": _PASSWORD},
    )
    assert login.status_code == 200, login.text
    return login.json()["token"], create.json()["id"]


def _headers(token: str | None) -> dict[str, str]:
    return {"Authorization": token} if token else {}


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


async def test_expand_respects_view_rule(
    app_client: AsyncClient,
    admin_token: str,
    auth_collection: dict,
) -> None:
    suffix = _suffix()
    locked = members = posts = None
    try:
        # Related collection only superusers may view.
        locked = await _create_collection(
            app_client,
            admin_token,
            name=f"exp_locked_{suffix}",
            schema=[{"name": "name", "type": "text"}],
            list_rule=None,
            view_rule=None,
        )
        # Related collection any authenticated caller may view.
        members = await _create_collection(
            app_client,
            admin_token,
            name=f"exp_members_{suffix}",
            schema=[
                {"name": "name", "type": "text"},
                {
                    "name": "secret",
                    "type": "relation",
                    "options": {"collectionId": locked["id"], "maxSelect": 1},
                },
            ],
            list_rule='@request.auth.id != ""',
            view_rule='@request.auth.id != ""',
        )
        posts = await _create_collection(
            app_client,
            admin_token,
            name=f"exp_posts_{suffix}",
            schema=[
                {"name": "title", "type": "text"},
                {
                    "name": "secret",
                    "type": "relation",
                    "options": {"collectionId": locked["id"], "maxSelect": 1},
                },
                {
                    "name": "members",
                    "type": "relation",
                    "options": {"collectionId": members["id"], "maxSelect": 5},
                },
            ],
        )

        locked_rec = await _create_record(
            app_client, admin_token, locked["name"], json={"name": "hidden"}
        )
        member_rec = await _create_record(
            app_client,
            admin_token,
            members["name"],
            json={"name": "alice", "secret": locked_rec["id"]},
        )
        post_rec = await _create_record(
            app_client,
            admin_token,
            posts["name"],
            json={
                "title": "hello",
                "secret": locked_rec["id"],
                "members": [member_rec["id"]],
            },
        )

        user_token, _user_id = await _user_token(app_client)
        expand = "secret,members,members.secret"
        list_url = f"/api/collections/{posts['name']}/records"
        view_url = f"{list_url}/{post_rec['id']}"

        async def _fetch(token: str | None) -> list[dict[str, Any]]:
            listed = await app_client.get(
                list_url, params={"expand": expand}, headers=_headers(token)
            )
            assert listed.status_code == 200, listed.text
            viewed = await app_client.get(
                view_url, params={"expand": expand}, headers=_headers(token)
            )
            assert viewed.status_code == 200, viewed.text
            return [listed.json()["items"][0], viewed.json()]

        # Anonymous: neither the locked record nor the auth-only members.
        for record in await _fetch(None):
            assert record["secret"] == locked_rec["id"]
            assert record.get("expand", {}) == {}

        # Authenticated user: members are visible, the locked record is not,
        # at any nesting level.
        for record in await _fetch(user_token):
            expanded = record.get("expand", {})
            assert "secret" not in expanded
            assert [m["id"] for m in expanded["members"]] == [member_rec["id"]]
            assert "secret" not in expanded["members"][0].get("expand", {})

        # Superuser: everything expands.
        for record in await _fetch(admin_token):
            expanded = record["expand"]
            assert expanded["secret"]["id"] == locked_rec["id"]
            assert expanded["secret"]["name"] == "hidden"
            assert expanded["members"][0]["expand"]["secret"]["id"] == locked_rec["id"]
    finally:
        await _delete_collection(app_client, admin_token, posts)
        await _delete_collection(app_client, admin_token, members)
        await _delete_collection(app_client, admin_token, locked)


async def test_auth_response_expand_respects_view_rule(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    suffix = _suffix()
    locked = members = accounts = None
    try:
        locked = await _create_collection(
            app_client,
            admin_token,
            name=f"aexp_locked_{suffix}",
            schema=[{"name": "name", "type": "text"}],
            list_rule=None,
            view_rule=None,
        )
        members = await _create_collection(
            app_client,
            admin_token,
            name=f"aexp_members_{suffix}",
            schema=[{"name": "name", "type": "text"}],
            list_rule='@request.auth.id != ""',
            view_rule='@request.auth.id != ""',
        )
        response = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": f"aexp_accounts_{suffix}",
                "type": "auth",
                "schema": [
                    {
                        "name": "secret",
                        "type": "relation",
                        "options": {"collectionId": locked["id"], "maxSelect": 1},
                    },
                    {
                        "name": "team",
                        "type": "relation",
                        "options": {"collectionId": members["id"], "maxSelect": 1},
                    },
                ],
                "listRule": "id = @request.auth.id",
                "viewRule": "id = @request.auth.id",
                "createRule": None,
                "updateRule": None,
                "deleteRule": None,
            },
        )
        assert response.status_code == 200, response.text
        accounts = response.json()

        locked_rec = await _create_record(
            app_client, admin_token, locked["name"], json={"name": "hidden"}
        )
        member_rec = await _create_record(
            app_client, admin_token, members["name"], json={"name": "team"}
        )
        email = f"aexp_{suffix}@example.com"
        await _create_record(
            app_client,
            admin_token,
            accounts["name"],
            json={
                "email": email,
                "password": _PASSWORD,
                "passwordConfirm": _PASSWORD,
                "secret": locked_rec["id"],
                "team": member_rec["id"],
            },
        )

        base = f"/api/collections/{accounts['name']}"
        login = await app_client.post(
            f"{base}/auth-with-password",
            params={"expand": "secret,team"},
            json={"identity": email, "password": _PASSWORD},
        )
        assert login.status_code == 200, login.text
        refresh = await app_client.post(
            f"{base}/auth-refresh",
            params={"expand": "secret,team"},
            headers={"Authorization": login.json()["token"]},
        )
        assert refresh.status_code == 200, refresh.text

        # The authenticated record is the caller: the auth-only collection
        # expands, the superuser-only one does not.
        for record in (login.json()["record"], refresh.json()["record"]):
            assert record["secret"] == locked_rec["id"]
            expanded = record.get("expand", {})
            assert "secret" not in expanded
            assert expanded["team"]["id"] == member_rec["id"]
    finally:
        await _delete_collection(app_client, admin_token, accounts)
        await _delete_collection(app_client, admin_token, members)
        await _delete_collection(app_client, admin_token, locked)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


async def _file_token(app_client: AsyncClient, token: str) -> str:
    response = await app_client.post(
        "/api/files/token", headers={"Authorization": token}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def test_file_requires_view_rule(
    app_client: AsyncClient,
    admin_token: str,
    auth_collection: dict,
) -> None:
    suffix = _suffix()
    locked = members = None
    try:
        file_schema = [
            {"name": "doc", "type": "file", "options": {"maxSelect": 1}},
            {
                "name": "privateDoc",
                "type": "file",
                "options": {"maxSelect": 1, "protected": True},
            },
        ]
        locked = await _create_collection(
            app_client,
            admin_token,
            name=f"file_locked_{suffix}",
            schema=file_schema,
            list_rule=None,
            view_rule=None,
        )
        members = await _create_collection(
            app_client,
            admin_token,
            name=f"file_members_{suffix}",
            schema=file_schema,
            list_rule='@request.auth.id != ""',
            view_rule='@request.auth.id != ""',
        )
        user_token, _user_id = await _user_token(app_client)
        admin_file_token = await _file_token(app_client, admin_token)
        user_file_token = await _file_token(app_client, user_token)

        async def _upload(collection: dict[str, Any]) -> tuple[str, str]:
            record = await _create_record(
                app_client,
                admin_token,
                collection["name"],
                files={
                    "doc": ("plain.txt", b"plain body", "text/plain"),
                    "privateDoc": ("private.txt", b"private body", "text/plain"),
                },
            )
            base = f"/api/files/{collection['id']}/{record['id']}"
            return f"{base}/{record['doc']}", f"{base}/{record['privateDoc']}"

        async def _status(
            url: str,
            *,
            token: str | None = None,
            file_token: str | None = None,
        ) -> int:
            params = {"token": file_token} if file_token else None
            response = await app_client.get(
                url, params=params, headers=_headers(token)
            )
            if response.status_code == 200:
                assert response.content in {b"plain body", b"private body"}
            return response.status_code

        # viewRule null: unprotected files are superuser-only too.
        plain, private = await _upload(locked)
        assert await _status(plain) == 404
        assert await _status(plain, token=user_token) == 404
        assert await _status(plain, file_token=user_file_token) == 404
        assert await _status(plain, token=admin_token) == 200
        assert await _status(plain, file_token=admin_file_token) == 200
        assert await _status(private, token=admin_token) == 404
        assert await _status(private, file_token=admin_file_token) == 200

        # Expression viewRule: evaluated for the caller.
        plain, private = await _upload(members)
        assert await _status(plain) == 404
        assert await _status(plain, file_token="invalid.token.value") == 404
        assert await _status(plain, token=user_token) == 200
        assert await _status(plain, file_token=user_file_token) == 200
        # Protected files still need a file token, even for allowed callers.
        assert await _status(private) == 404
        assert await _status(private, token=user_token) == 404
        assert await _status(private, file_token=user_file_token) == 200
    finally:
        await _delete_collection(app_client, admin_token, members)
        await _delete_collection(app_client, admin_token, locked)


# ---------------------------------------------------------------------------
# @collection filters
# ---------------------------------------------------------------------------


async def test_collection_filter_requires_list_rule(
    app_client: AsyncClient,
    admin_token: str,
    auth_collection: dict,
) -> None:
    suffix = _suffix()
    locked = members = posts = guarded = None
    try:
        locked = await _create_collection(
            app_client,
            admin_token,
            name=f"flt_locked_{suffix}",
            schema=[{"name": "code", "type": "text"}],
            list_rule=None,
            view_rule=None,
        )
        members = await _create_collection(
            app_client,
            admin_token,
            name=f"flt_members_{suffix}",
            schema=[
                {"name": "code", "type": "text"},
                {"name": "owner", "type": "text"},
            ],
            list_rule="owner = @request.auth.id",
            view_rule="owner = @request.auth.id",
        )
        posts = await _create_collection(
            app_client,
            admin_token,
            name=f"flt_posts_{suffix}",
            schema=[{"name": "code", "type": "text"}],
        )
        # The collection's own listRule may still reference a locked
        # collection: API rules are trusted expressions.
        guarded = await _create_collection(
            app_client,
            admin_token,
            name=f"flt_guarded_{suffix}",
            schema=[{"name": "code", "type": "text"}],
            list_rule=f"@collection.{locked['name']}.code ?= code",
        )

        user_token, user_id = await _user_token(app_client)
        await _create_record(app_client, admin_token, locked["name"], json={"code": "a"})
        await _create_record(
            app_client,
            admin_token,
            members["name"],
            json={"code": "b", "owner": user_id},
        )
        await _create_record(app_client, admin_token, posts["name"], json={"code": "a"})
        await _create_record(app_client, admin_token, posts["name"], json={"code": "b"})
        await _create_record(app_client, admin_token, guarded["name"], json={"code": "a"})
        await _create_record(app_client, admin_token, guarded["name"], json={"code": "z"})

        posts_url = f"/api/collections/{posts['name']}/records"
        locked_filter = f"@collection.{locked['name']}.code ?= code"
        members_filter = f"@collection.{members['name']}.code ?= code"

        async def _list(url: str, token: str | None, flt: str | None = None):
            params = {"filter": flt} if flt else None
            return await app_client.get(url, params=params, headers=_headers(token))

        # Locked collection: fails to resolve for non-superusers.
        for token in (None, user_token):
            response = await _list(posts_url, token, locked_filter)
            assert response.status_code == 400, response.text
            assert response.json()["status"] == 400
            response = await _list(
                posts_url, token, f'code = "a" || {locked_filter}'
            )
            assert response.status_code == 400, response.text

        response = await _list(posts_url, admin_token, locked_filter)
        assert response.status_code == 200, response.text
        assert [item["code"] for item in response.json()["items"]] == ["a"]

        # Expression listRule: applied inside the reference.
        response = await _list(posts_url, None, members_filter)
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
        response = await _list(posts_url, user_token, members_filter)
        assert response.status_code == 200, response.text
        assert [item["code"] for item in response.json()["items"]] == ["b"]

        # A rule (not a caller filter) referencing the locked collection keeps
        # working for everyone.
        response = await _list(f"/api/collections/{guarded['name']}/records", None)
        assert response.status_code == 200, response.text
        assert [item["code"] for item in response.json()["items"]] == ["a"]
    finally:
        await _delete_collection(app_client, admin_token, guarded)
        await _delete_collection(app_client, admin_token, posts)
        await _delete_collection(app_client, admin_token, members)
        await _delete_collection(app_client, admin_token, locked)
