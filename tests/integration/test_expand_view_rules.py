"""PostgreSQL integration tests: ``expand`` applies the related ``viewRule``.

PocketBase 0.22.34 loads every expanded record through ``expandFetch``
(apis/record_helpers.go l.176-210): admins see everything, a ``viewRule`` of
``null`` fails the path, any other rule filters the related records with the
request info of the current request; ``autoIgnoreAuthRecordsEmailVisibility``
(l.212-279) shows an expanded auth email to an admin, its owner and a
``manageRule`` match only.  ``Dao.ExpandRecords`` (daos/record_expand.go
l.37-302) normalizes the paths, expands nested levels before assigning the
parent level (a failing nested level drops the whole path), keeps the field
order and skips missing or refused targets.

The schema mimics the BUMA shapes (a public ``buma_sale`` pointing to a
protected ``buma_customer``, an admin-only ``buma_pdv``, a multiple relation,
the users ``manageRule``) with synthetic data only.  The expected values are
the PocketBase 0.22.34 results of ``tests/pocketbase_parity/
expand_rules_probe.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event, text

import ppbase.api.records as records_api
from ppbase.db.engine import get_engine
from ppbase.services.realtime_service import (
    SubscriptionManager,
    broadcast_record_change,
)
from ppbase.services.record_service import get_record, resolve_collection

pytestmark = pytest.mark.asyncio

PASSWORD = "expand-Password-1"
PUBLIC = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}
# "readers" plays the role/permission: PPBase resolves @request.auth.<field>
# from the token claims and the host context only, so the generic tests use
# relation rules both servers evaluate natively.
# (a direct multi-value field compares as its JSON text in 0.22, so the
# membership test goes through the relation path, which is a multi-match).
CUSTOMER_RULE = (
    "public = true || (@request.auth.id != '' && readers.id ?= @request.auth.id)"
)
MANAGE_RULE = "managers.id ?= @request.auth.id"
# The exact buma_customer rule; BUMA flattens role.permission.* into the
# request auth context (app/auth/context.py).
BUMA_CUSTOMER_RULE = (
    "@request.auth.id != '' && (@request.auth.role.permission.isAdmin = true "
    "|| @request.auth.role.permission.customer.read = true)"
)


def _field(name: str, field_type: str, **options: Any) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": field_type,
        "required": False,
        "presentable": False,
        "unique": False,
        "options": options,
    }


def _relation(name: str, collection_id: str, max_select: Any) -> dict[str, Any]:
    return _field(
        name,
        "relation",
        collectionId=collection_id,
        cascadeDelete=False,
        minSelect=None,
        maxSelect=max_select,
        displayFields=None,
    )


async def _collection(client: AsyncClient, admin: str, body: dict) -> dict:
    response = await client.post(
        "/api/collections",
        headers={"Authorization": admin},
        json={"type": "base", **PUBLIC, **body},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create(client: AsyncClient, admin: str, name: str, body: dict) -> str:
    response = await client.post(
        f"/api/collections/{name}/records",
        headers={"Authorization": admin},
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _login(client: AsyncClient, name: str, email: str) -> str:
    response = await client.post(
        f"/api/collections/{name}/auth-with-password",
        json={"identity": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


@pytest_asyncio.fixture(scope="module")
async def env(app_client: AsyncClient, admin_token: str) -> dict[str, Any]:
    sfx = uuid.uuid4().hex[:8]
    admin = admin_token
    names = {key: f"x{key}_{sfx}" for key in ("r", "p", "c", "u", "t", "b", "s", "n")}

    region = await _collection(
        app_client, admin, {"name": names["r"], "schema": [_field("name", "text")]}
    )
    pdv = await _collection(
        app_client,
        admin,
        {
            "name": names["p"],
            "schema": [_field("name", "text")],
            "listRule": None,
            "viewRule": None,
        },
    )
    auth_options = {
        "allowEmailAuth": True,
        "allowOAuth2Auth": False,
        "allowUsernameAuth": True,
        "exceptEmailDomains": None,
        "manageRule": None,
        "minPasswordLength": 8,
        "onlyEmailDomains": None,
        "onlyVerified": False,
        "requireEmail": False,
    }
    users = await _collection(
        app_client,
        admin,
        {
            "name": names["u"],
            "type": "auth",
            "schema": [],
            "updateRule": None,
            "deleteRule": None,
            "options": auth_options,
        },
    )
    customer = await _collection(
        app_client,
        admin,
        {
            "name": names["c"],
            "schema": [
                _field("name", "text"),
                _field("public", "bool"),
                _relation("readers", users["id"], None),
                _relation("region", region["id"], 1),
                _relation("pdv", pdv["id"], None),
            ],
            "listRule": CUSTOMER_RULE,
            "viewRule": CUSTOMER_RULE,
        },
    )
    response = await app_client.patch(
        f"/api/collections/{users['id']}",
        headers={"Authorization": admin},
        json={
            "schema": [
                _relation("managers", users["id"], None),
                _relation("customer", customer["id"], 1),
            ],
            "options": {**auth_options, "manageRule": MANAGE_RULE},
        },
    )
    assert response.status_code == 200, response.text
    tag = await _collection(
        app_client,
        admin,
        {
            "name": names["t"],
            "schema": [_field("name", "text")],
            "listRule": None,
            "viewRule": '@request.data.reveal = true || @request.query.reveal = "1"',
        },
    )
    bcustomer = await _collection(
        app_client,
        admin,
        {
            "name": names["b"],
            "schema": [_field("name", "text")],
            "listRule": BUMA_CUSTOMER_RULE,
            "viewRule": BUMA_CUSTOMER_RULE,
        },
    )
    await _collection(
        app_client,
        admin,
        {
            "name": names["s"],
            "schema": [
                _field("label", "text"),
                _relation("customer", customer["id"], 1),
                _relation("customers", customer["id"], None),
                _relation("user", users["id"], 1),
                _relation("tag", tag["id"], 1),
                _relation("bcustomer", bcustomer["id"], 1),
            ],
        },
    )
    await _collection(
        app_client,
        admin,
        {
            "name": names["n"],
            "schema": [
                _field("label", "text"),
                _relation("customer", customer["id"], 1),
            ],
            "listRule": None,
            "viewRule": None,
        },
    )

    ids: dict[str, str] = {}
    emails: dict[str, str] = {}
    users_spec = {
        "manager": {"emailVisibility": False},
        "reader": {"emailVisibility": False},
        "visible": {"emailVisibility": True},
        "plain": {"emailVisibility": False, "managers": ["manager"]},
    }
    for key, extra in users_spec.items():
        emails[key] = f"{key}-{sfx}@example.invalid"
        body = {**extra}
        if "managers" in body:
            body["managers"] = [ids[name] for name in body["managers"]]
        ids[key] = await _create(
            app_client,
            admin,
            names["u"],
            {
                "email": emails[key],
                "password": PASSWORD,
                "passwordConfirm": PASSWORD,
                **body,
            },
        )

    ids["r1"] = await _create(app_client, admin, names["r"], {"name": "r1"})
    ids["p1"] = await _create(app_client, admin, names["p"], {"name": "p1"})
    ids["c1"] = await _create(
        app_client,
        admin,
        names["c"],
        {"name": "c1", "public": True, "region": ids["r1"], "pdv": [ids["p1"]]},
    )
    ids["c2"] = await _create(
        app_client,
        admin,
        names["c"],
        {
            "name": "c2",
            "public": False,
            "readers": [ids["reader"]],
            "region": ids["r1"],
            "pdv": [ids["p1"]],
        },
    )
    ids["c3"] = await _create(
        app_client, admin, names["c"], {"name": "c3", "public": True}
    )
    for key in ("plain", "reader"):
        response = await app_client.patch(
            f"/api/collections/{names['u']}/records/{ids[key]}",
            headers={"Authorization": admin},
            json={"customer": ids["c2"]},
        )
        assert response.status_code == 200, response.text
    ids["t1"] = await _create(app_client, admin, names["t"], {"name": "t1"})
    ids["b1"] = await _create(app_client, admin, names["b"], {"name": "b1"})

    ids["s1"] = await _create(
        app_client,
        admin,
        names["s"],
        {
            "label": "s1",
            "customer": ids["c2"],
            "customers": [ids["c2"], ids["c3"], ids["c1"]],
            "user": ids["plain"],
            "tag": ids["t1"],
            "bcustomer": ids["b1"],
        },
    )
    ids["s2"] = await _create(
        app_client,
        admin,
        names["s"],
        {"label": "s2", "customer": ids["c1"], "user": ids["visible"]},
    )
    ids["n1"] = await _create(
        app_client, admin, names["n"], {"label": "n1", "customer": ids["c1"]}
    )
    # A legacy orphan id (PocketBase removes references on delete, imported
    # data keeps them, D-0021): stored directly, expand must skip it.
    async with get_engine().begin() as conn:
        await conn.execute(
            text(f'UPDATE "{names["s"]}" SET "customers" = :ids WHERE "id" = :id'),
            {
                "ids": [ids["c2"], "missing0000000x", ids["c3"], ids["c1"]],
                "id": ids["s1"],
            },
        )

    tokens = {"guest": None, "admin": admin}
    for key in ("plain", "reader", "manager"):
        tokens[key] = await _login(app_client, names["u"], emails[key])

    return {"names": names, "ids": ids, "tokens": tokens, "emails": emails}


def _headers(env: dict[str, Any], caller: str) -> dict[str, str]:
    token = env["tokens"][caller]
    return {"Authorization": token} if token else {}


async def _view(
    client: AsyncClient,
    env: dict[str, Any],
    caller: str,
    key: str,
    expand: str,
    **query: str,
) -> dict[str, Any]:
    names = env["names"]
    collection = {"s": names["s"], "c": names["c"]}[key[0]]
    response = await client.get(
        f"/api/collections/{collection}/records/{env['ids'][key]}",
        headers=_headers(env, caller),
        params={"expand": expand, **query},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _names(value: Any) -> Any:
    if isinstance(value, list):
        return [item.get("name") for item in value]
    return value.get("name") if isinstance(value, dict) else value


# ---------------------------------------------------------------------------
# HTTP list / view
# ---------------------------------------------------------------------------


async def test_anonymous_list_drops_protected_relations(
    app_client: AsyncClient, env: dict[str, Any]
) -> None:
    response = await app_client.get(
        f"/api/collections/{env['names']['s']}/records",
        params={"expand": "customer,customers,tag", "sort": "label"},
    )
    assert response.status_code == 200, response.text
    s1, s2 = response.json()["items"]
    # c2 is private: the single relation key is omitted, the list keeps the
    # field order of the authorized records (c2 refused, orphan skipped).
    assert "customer" not in s1["expand"]
    assert _names(s1["expand"]["customers"]) == ["c3", "c1"]
    assert "tag" not in s1["expand"]
    assert _names(s2["expand"]["customer"]) == "c1"
    # The relation values themselves are untouched.
    assert s1["customer"] == env["ids"]["c2"]


@pytest.mark.parametrize(
    ("caller", "customer", "customers"),
    [
        ("guest", None, ["c3", "c1"]),
        ("plain", None, ["c3", "c1"]),
        ("reader", "c2", ["c2", "c3", "c1"]),
        ("manager", None, ["c3", "c1"]),
        ("admin", "c2", ["c2", "c3", "c1"]),
    ],
)
async def test_view_expand_follows_the_customer_rule_per_caller(
    app_client: AsyncClient,
    env: dict[str, Any],
    caller: str,
    customer: str | None,
    customers: list[str],
) -> None:
    record = await _view(app_client, env, caller, "s1", "customer,customers")
    expand = record["expand"]
    assert _names(expand.get("customer")) == customer
    assert _names(expand["customers"]) == customers


async def test_nested_admin_only_level_drops_the_whole_path(
    app_client: AsyncClient, env: dict[str, Any]
) -> None:
    # buma_pdv-like viewRule null: the nested fetch fails, so PocketBase
    # assigns neither level (record_expand.go l.203-209), even when the
    # parent level alone was requested too (it is merged into the longer
    # path by normalizeExpands).
    for expand in ("customer.pdv", "customer,customer.pdv", "customer.pdv,customer"):
        record = await _view(app_client, env, "reader", "s1", expand)
        assert "expand" not in record, (expand, record.get("expand"))

    record = await _view(
        app_client, env, "reader", "s1", "customer.region,customer.pdv"
    )
    assert _names(record["expand"]["customer"]) == "c2"
    assert _names(record["expand"]["customer"]["expand"]["region"]) == "r1"
    assert "pdv" not in record["expand"]["customer"].get("expand", {})

    record = await _view(app_client, env, "admin", "s1", "customer.pdv,customer.region")
    nested = record["expand"]["customer"]["expand"]
    assert _names(nested["pdv"]) == ["p1"]
    assert _names(nested["region"]) == "r1"


async def test_unknown_nested_field_drops_the_path(
    app_client: AsyncClient, env: dict[str, Any]
) -> None:
    record = await _view(app_client, env, "admin", "s1", "customer.nope,customers")
    assert "customer" not in record["expand"]
    assert _names(record["expand"]["customers"]) == ["c2", "c3", "c1"]
    record = await _view(app_client, env, "admin", "s1", "customer..region")
    assert "expand" not in record


async def test_back_relation_expand_applies_the_view_rule(
    app_client: AsyncClient, env: dict[str, Any]
) -> None:
    names = env["names"]
    via_sale = f"{names['s']}_via_customer"
    via_note = f"{names['n']}_via_customer"
    record = await _view(app_client, env, "guest", "c1", f"{via_sale},{via_note}")
    assert [item["label"] for item in record["expand"][via_sale]] == ["s2"]
    assert via_note not in record["expand"]  # notes are admin-only

    record = await _view(app_client, env, "admin", "c1", f"{via_note}.customer")
    notes = record["expand"][via_note]
    assert [item["label"] for item in notes] == ["n1"]
    assert _names(notes[0]["expand"]["customer"]) == "c1"
    # The dynamic back-relation value is not serialized (exportUnknown=false).
    assert via_note not in record


@pytest.mark.parametrize(
    ("caller", "has_email"),
    [
        ("guest", False),
        ("plain", True),  # owner
        ("reader", False),
        ("manager", True),  # manageRule managers.id ?= @request.auth.id
        ("admin", True),
    ],
)
async def test_expanded_auth_email_visibility(
    app_client: AsyncClient,
    env: dict[str, Any],
    caller: str,
    has_email: bool,
) -> None:
    record = await _view(app_client, env, caller, "s1", "user")
    user = record["expand"]["user"]
    assert user["id"] == env["ids"]["plain"]
    assert user["emailVisibility"] is False
    assert ("email" in user) is has_email
    if has_email:
        assert user["email"] == env["emails"]["plain"]

    visible = (await _view(app_client, env, caller, "s2", "user"))["expand"]["user"]
    assert visible["email"] == env["emails"]["visible"]


async def test_request_query_is_part_of_the_rule_context(
    app_client: AsyncClient, env: dict[str, Any]
) -> None:
    record = await _view(app_client, env, "guest", "s1", "tag", reveal="1")
    assert _names(record["expand"]["tag"]) == "t1"
    record = await _view(app_client, env, "guest", "s1", "tag")
    assert "expand" not in record


async def test_flattened_role_context_is_shared_with_expand(
    app_client: AsyncClient,
    env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BUMA rule sees the same patched ``@request.auth`` as list/view."""
    record = await _view(app_client, env, "reader", "s1", "bcustomer")
    assert "expand" not in record

    original = records_api._prepare_rule_context
    reader = env["ids"]["reader"]

    def patched(request, token_payload, data=None):
        auth_ctx, request_ctx = original(request, token_payload, data=data)
        allowed = bool(token_payload) and token_payload.get("id") == reader
        request_ctx.setdefault("auth", {}).update(
            {
                "role.permission.isAdmin": False,
                "role.permission.customer.read": allowed,
            }
        )
        return auth_ctx, request_ctx

    monkeypatch.setattr(records_api, "_prepare_rule_context", patched)
    record = await _view(app_client, env, "reader", "s1", "bcustomer")
    assert _names(record["expand"]["bcustomer"]) == "b1"
    record = await _view(app_client, env, "plain", "s1", "bcustomer")
    assert "expand" not in record


# ---------------------------------------------------------------------------
# Create / update / auth responses / realtime
# ---------------------------------------------------------------------------


async def test_create_and_update_expand_with_the_request_data(
    app_client: AsyncClient, env: dict[str, Any]
) -> None:
    names, ids = env["names"], env["ids"]
    response = await app_client.post(
        f"/api/collections/{names['s']}/records",
        params={"expand": "customer,tag"},
        json={"label": "s3", "customer": ids["c2"], "tag": ids["t1"], "reveal": True},
    )
    assert response.status_code == 200, response.text
    created = response.json()
    assert list(created["expand"]) == ["tag"]  # @request.data.reveal = true

    response = await app_client.patch(
        f"/api/collections/{names['s']}/records/{created['id']}",
        headers=_headers(env, "reader"),
        params={"expand": "customer,tag"},
        json={"label": "s3-updated"},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert list(updated["expand"]) == ["customer"]
    assert _names(updated["expand"]["customer"]) == "c2"

    response = await app_client.delete(
        f"/api/collections/{names['s']}/records/{created['id']}"
    )
    assert response.status_code == 204, response.text


@pytest.mark.parametrize(("caller", "expanded"), [("plain", False), ("reader", True)])
async def test_auth_responses_expand_as_the_authenticated_record(
    app_client: AsyncClient,
    env: dict[str, Any],
    caller: str,
    expanded: bool,
) -> None:
    name = env["names"]["u"]
    response = await app_client.post(
        f"/api/collections/{name}/auth-with-password",
        params={"expand": "customer"},
        json={"identity": env["emails"][caller], "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert ("expand" in record) is expanded
    if expanded:
        assert _names(record["expand"]["customer"]) == "c2"

    response = await app_client.post(
        f"/api/collections/{name}/auth-refresh",
        headers=_headers(env, caller),
        params={"expand": "customer"},
    )
    assert response.status_code == 200, response.text
    assert ("expand" in response.json()["record"]) is expanded


async def test_realtime_expand_applies_the_view_rule(
    app_client: AsyncClient, env: dict[str, Any]
) -> None:
    engine = get_engine()
    sales = await resolve_collection(engine, env["names"]["s"])
    record = await get_record(engine, sales, env["ids"]["s1"])
    manager = SubscriptionManager()
    callers = {
        "guest": None,
        "admin": {"id": "admin", "type": "admin"},
    }
    clients = {}
    for caller, payload in callers.items():
        client_id = manager.register_client()
        await manager.add_subscription(
            client_id,
            f"{sales.name}/*",
            auth_payload=payload,
            options_query={"expand": "customer,customers"},
        )
        clients[caller] = client_id

    await broadcast_record_change(
        manager,
        sales.name,
        record["id"],
        "update",
        record,
        engine=engine,
        collection=sales,
    )
    events = {}
    for caller, client_id in clients.items():
        session = manager.get_session(client_id)
        assert session is not None and not session.response_queue.empty(), caller
        events[caller] = session.response_queue.get_nowait()["data"]["record"]

    assert "customer" not in events["guest"]["expand"]
    assert _names(events["guest"]["expand"]["customers"]) == ["c3", "c1"]
    assert _names(events["admin"]["expand"]["customer"]) == "c2"
    assert _names(events["admin"]["expand"]["customers"]) == ["c2", "c3", "c1"]


async def test_list_expand_runs_one_query_per_collection_and_level(
    app_client: AsyncClient, env: dict[str, Any]
) -> None:
    customer_table = f'FROM "{env["names"]["c"]}"'
    region_table = f'FROM "{env["names"]["r"]}"'
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        response = await app_client.get(
            f"/api/collections/{env['names']['s']}/records",
            headers=_headers(env, "reader"),
            params={"expand": "customer.region,customers", "perPage": 50},
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) >= 2
    # customer.region: one customer query + one region query; customers: one.
    assert sum(customer_table in sql for sql in statements) == 2
    assert sum(region_table in sql for sql in statements) == 1
