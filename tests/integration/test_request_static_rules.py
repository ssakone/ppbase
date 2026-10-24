"""Absent @request.* values must not crash the list query (PostgreSQL).

On the real backup an anonymous list of buma_article_stockage
(``@request.auth.id != '' && @request.auth.id != null``) or buma_pdv
(``@request.auth.role.permission.isAdmin = true || ....read = true``) raised
asyncpg IndeterminateDatatypeError ("could not determine data type of
parameter") and DataError ("'' ... a boolean is required"), closed the
connection and failed the next request (ReadError).  PocketBase 0.22.34
returns 200 with the authorized rows.  These tests execute the exact rule
shapes against PostgreSQL and assert no 500 and a usable connection after.
Values are paired with the 0.22.34 binary by
tests/pocketbase_parity/request_static_rules_probe.py.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

RULE_AUTH_ID = "@request.auth.id != '' && @request.auth.id != null"
RULE_PERM = (
    "(@request.auth.role.permission.isAdmin = true"
    " || @request.auth.role.permission.pdv.read = true)"
    " && @request.auth.agent = agent"
)


async def _collection(app_client: AsyncClient, admin_token: str, body: dict) -> dict:
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
            "type": "base",
            **body,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_anonymous_list_with_absent_request_auth_does_not_500(
    app_client: AsyncClient, admin_token: str
) -> None:
    suffix = uuid.uuid4().hex[:8]
    stk = await _collection(
        app_client,
        admin_token,
        {
            "name": f"rs_stk_{suffix}",
            "schema": [{"name": "name", "type": "text"}],
            "listRule": RULE_AUTH_ID,
        },
    )
    pdv = await _collection(
        app_client,
        admin_token,
        {
            "name": f"rs_pdv_{suffix}",
            "schema": [
                {"name": "name", "type": "text"},
                {"name": "agent", "type": "text"},
            ],
            "listRule": RULE_PERM,
        },
    )
    try:
        for i in range(3):
            resp = await app_client.post(
                f"/api/collections/{stk['name']}/records",
                headers={"Authorization": admin_token},
                json={"name": f"s{i}"},
            )
            assert resp.status_code == 200, resp.text
        for name, agent in (("p_empty", ""), ("p_a1", "A1")):
            resp = await app_client.post(
                f"/api/collections/{pdv['name']}/records",
                headers={"Authorization": admin_token},
                json={"name": name, "agent": agent},
            )
            assert resp.status_code == 200, resp.text

        # Anonymous: the absent @request.auth.* subtree must compare as
        # NULL/empty, yield 200 and no rows (the && short-circuits), never 500.
        stk_resp = await app_client.get(f"/api/collections/{stk['name']}/records")
        assert stk_resp.status_code == 200, stk_resp.text
        assert stk_resp.json()["items"] == []

        pdv_resp = await app_client.get(f"/api/collections/{pdv['name']}/records")
        assert pdv_resp.status_code == 200, pdv_resp.text
        assert pdv_resp.json()["items"] == []

        # The connection must still be usable (the old crash poisoned it).
        again = await app_client.get(f"/api/collections/{stk['name']}/records")
        assert again.status_code == 200, again.text
    finally:
        for coll in (stk, pdv):
            await app_client.delete(
                f"/api/collections/{coll['id']}",
                headers={"Authorization": admin_token},
            )


async def test_empty_list_total_pages_is_zero(
    app_client: AsyncClient, admin_token: str
) -> None:
    coll = await _collection(
        app_client,
        admin_token,
        {
            "name": f"rs_empty_{uuid.uuid4().hex[:8]}",
            "schema": [{"name": "name", "type": "text"}],
        },
    )
    try:
        response = await app_client.get(
            f"/api/collections/{coll['name']}/records",
            params={"filter": 'name = "nope"'},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["totalItems"] == 0
        assert body["totalPages"] == 0
    finally:
        await app_client.delete(
            f"/api/collections/{coll['id']}",
            headers={"Authorization": admin_token},
        )


async def test_expand_key_is_omitted_without_a_related_record(
    app_client: AsyncClient, admin_token: str
) -> None:
    suffix = uuid.uuid4().hex[:8]
    rels = await _collection(
        app_client,
        admin_token,
        {"name": f"rs_rel_{suffix}", "schema": [{"name": "name", "type": "text"}]},
    )
    posts = await _collection(
        app_client,
        admin_token,
        {
            "name": f"rs_post_{suffix}",
            "schema": [
                {"name": "name", "type": "text"},
                {
                    "name": "rel",
                    "type": "relation",
                    "options": {"collectionId": rels["id"], "maxSelect": 1},
                },
            ],
        },
    )
    try:
        linked = (
            await app_client.post(
                f"/api/collections/{rels['name']}/records",
                headers={"Authorization": admin_token},
                json={"name": "r1"},
            )
        ).json()
        for payload in ({"name": "with_rel", "rel": linked["id"]}, {"name": "no_rel"}):
            resp = await app_client.post(
                f"/api/collections/{posts['name']}/records",
                headers={"Authorization": admin_token},
                json=payload,
            )
            assert resp.status_code == 200, resp.text

        response = await app_client.get(
            f"/api/collections/{posts['name']}/records",
            params={"expand": "rel", "sort": "name"},
        )
        assert response.status_code == 200, response.text
        by_name = {item["name"]: item for item in response.json()["items"]}
        assert "expand" not in by_name["no_rel"]
        assert by_name["with_rel"]["expand"]["rel"]["name"] == "r1"
    finally:
        for coll in (posts, rels):
            await app_client.delete(
                f"/api/collections/{coll['id']}",
                headers={"Authorization": admin_token},
            )


@pytest.mark.parametrize(
    ("permission", "allowed"),
    [
        # BUMA flattens the role JSON into dotted @request.auth keys
        # (app/auth/context.py).  PocketBase compares the json_extract value
        # with ``true`` (1) in SQLite: 1 and true match, "true"/0 do not, a
        # missing key is NULL.  Paired with the 0.22.34 binary by
        # tests/pocketbase_parity/request_static_rules_probe.py.
        ({"role.permission.pdv.read": True}, True),
        ({"role.permission.pdv.read": 1}, True),
        ({"role.permission.pdv.read": 1.0}, True),
        ({"role.permission.pdv.read": 0}, False),
        ({"role.permission.pdv.read": "true"}, False),
        ({"role.permission.pdv.read": "1"}, False),
        ({"role.permission.pdv.read": None}, False),
        ({"role.permission.pdv.read": [True]}, False),
        ({}, False),
        ({"role.permission.isAdmin": "true"}, False),
        ({"role.permission.isAdmin": 1}, True),
    ],
)
async def test_present_non_boolean_permission_values_never_500(
    app_client: AsyncClient, admin_token: str, permission: dict, allowed: bool
) -> None:
    from ppbase.db.engine import get_engine
    from ppbase.services.record_service import list_records, resolve_collection

    suffix = uuid.uuid4().hex[:8]
    pdv = await _collection(
        app_client,
        admin_token,
        {
            "name": f"rs_pdvv_{suffix}",
            "schema": [
                {"name": "name", "type": "text"},
                {"name": "agent", "type": "text"},
            ],
            "listRule": RULE_PERM,
        },
    )
    try:
        for name, agent in (("p_empty", ""), ("p_a1", "A1"), ("p_a2", "A2")):
            resp = await app_client.post(
                f"/api/collections/{pdv['name']}/records",
                headers={"Authorization": admin_token},
                json={"name": name, "agent": agent},
            )
            assert resp.status_code == 200, resp.text
        engine = get_engine()
        collection = await resolve_collection(engine, pdv["name"])
        auth = {"id": "u1", "type": "authRecord", "agent": "A1", **permission}

        def is_true(key: str) -> bool:
            # SQLite: ``value = 1`` holds for true, 1 and 1.0 only.
            value = permission.get(key)
            return value is True or (type(value) in (int, float) and value == 1)

        # ``(isAdmin != true || read != true)``: true unless both are true.
        negated_allowed = not (
            is_true("role.permission.isAdmin") and is_true("role.permission.pdv.read")
        )
        for rule, expected in (
            (RULE_PERM, ["p_a1"] if allowed else []),
            (
                RULE_PERM.replace("= true", "!= true"),
                ["p_a1"] if negated_allowed else [],
            ),
        ):
            result = await list_records(
                engine,
                collection,
                filter_str=rule,
                sort="name",
                request_context={"auth": auth, "query": {}, "data": {}},
                allow_hidden=False,
            )
            assert [item["name"] for item in result["items"]] == expected, rule
        # Absent request values are NULL for ordering/like comparisons.
        for value in ("name < @request.query.nope", "name ~ @request.query.nope"):
            result = await list_records(
                engine,
                collection,
                filter_str=value,
                request_context={"auth": auth, "query": {}, "data": {}},
            )
            assert result["items"] == [], value
    finally:
        await app_client.delete(
            f"/api/collections/{pdv['id']}", headers={"Authorization": admin_token}
        )
