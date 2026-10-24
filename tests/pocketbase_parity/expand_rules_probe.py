"""Paired PocketBase 0.22.34 / PPBase probe: expand view rules and 0.22 errors.

PocketBase 0.22.34 filters every expanded record by the ``viewRule`` of its
collection with the request info of the current request (``expandFetch``,
apis/record_helpers.go l.176-210; realtime: apis/realtime.go l.424-437),
fails a path whose target ``viewRule`` is ``null`` for a non-admin, drops the
whole path when a nested level fails (daos/record_expand.go l.197-209) and
shows an expanded auth email only to an admin, its owner or a ``manageRule``
match (l.212-279).  Its errors are ``{"code", "message", "data"}``
(apis/api_error.go l.11-15).

The probe builds the same synthetic, BUMA-shaped schema on an official
PocketBase 0.22.34 binary and on PPBase (``error_compat="0.22"``): a public
``sale`` pointing to a protected ``customer`` (single and multiple
relations), an admin-only ``pdv`` nested under it, an admin-only ``note``
back relation, an auth ``users`` collection with a ``manageRule``, a rule on
``@request.data``/``@request.query`` and the exact ``buma_customer`` rule on
``@request.auth.role.permission.*``.  PocketBase resolves that last rule
through the ``users.role`` relation; PPBase core only reads flat
``@request.auth`` keys, so the PPBase side installs the same kind of live
role context that BUMA's ``app/auth/context.py`` installs (flattened
``role.permission.*`` keys).  It then runs the same HTTP list/view/create/
update/auth/realtime requests for seven callers and a list of error requests,
and writes ``pocketbase.json``, ``ppbase.json`` and ``diff.json`` in
``--out``.  ``created``/``updated`` timestamps and the auth ``username``
(PPBase has no username column on a fresh auth collection) are dropped
before the comparison.  Differences listed in ``KNOWN_DIVERGENCES`` are
reported separately.

Only synthetic records are created.  Both servers listen on random loopback
ports and are always stopped; ``--database-url`` must point to an empty,
disposable PostgreSQL database.

Usage (from the repository root; ``PYTHONPATH`` makes the script import
this checkout of PPBase)::

    PYTHONPATH=. python tests/pocketbase_parity/expand_rules_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --out /tmp/expand-rules-probe
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

ADMIN_EMAIL = "probe-admin@example.invalid"
ADMIN_PASSWORD = "probe-Password-123"
PASSWORD = "probe-User-Password-1"

C = {  # collection ids (15 chars, PocketBase id format)
    "region": "wsa6colregion01",
    "pdv": "wsa6colpdv00001",
    "roles": "wsa6colroles001",
    "users": "wsa6colusers001",
    "customer": "wsa6colcustom01",
    "tag": "wsa6coltag00001",
    "bcustomer": "wsa6colbcust001",
    "sale": "wsa6colsale0001",
    "note": "wsa6colnote0001",
}
N = {key: f"wsa6_{key}" for key in C}
R = {  # record ids
    "r1": "wsa6region00001",
    "p1": "wsa6pdv00000001",
    "role_read": "wsa6roleread001",
    "role_none": "wsa6rolenone001",
    "manager": "wsa6usermanager",
    "reader": "wsa6userreader1",
    "visible": "wsa6uservisible",
    "plain": "wsa6userplain01",
    "rolereader": "wsa6userrolerd1",
    "rolenone": "wsa6userrolen01",
    "c1": "wsa6customer001",
    "c2": "wsa6customer002",
    "c3": "wsa6customer003",
    "t1": "wsa6tag00000001",
    "b1": "wsa6bcustomer01",
    "s1": "wsa6sale0000001",
    "s2": "wsa6sale0000002",
    "n1": "wsa6note0000001",
}
USERS = ("manager", "reader", "visible", "plain", "rolereader", "rolenone")
CALLERS = ("guest", "plain", "reader", "manager", "rolereader", "rolenone", "admin")

CUSTOMER_RULE = (
    "public = true || (@request.auth.id != '' && readers.id ?= @request.auth.id)"
)
MANAGE_RULE = "managers.id ?= @request.auth.id"
TAG_RULE = '@request.data.reveal = true || @request.query.reveal = "1"'
BUMA_CUSTOMER_RULE = (
    "@request.auth.id != '' && (@request.auth.role.permission.isAdmin = true "
    "|| @request.auth.role.permission.customer.read = true)"
)
# PPBase side: the flattened live role context BUMA computes from the
# users.role -> roles.permission JSON (app/auth/context.py).
BUMA_ROLE_CONTEXT = {
    R["rolereader"]: {
        "role.permission.isAdmin": False,
        "role.permission.customer.read": True,
    },
    R["rolenone"]: {
        "role.permission.isAdmin": False,
        "role.permission.customer.read": False,
    },
}

RULES_PUBLIC = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}
RULES_ADMIN = {key: None for key in RULES_PUBLIC}
AUTH_OPTIONS = {
    "allowEmailAuth": True,
    "allowOAuth2Auth": False,
    "allowUsernameAuth": True,
    "exceptEmailDomains": None,
    "manageRule": MANAGE_RULE,
    "minPasswordLength": 8,
    "onlyEmailDomains": None,
    "onlyVerified": False,
    "requireEmail": False,
}

# Differences that are known and documented (status codes the compatibility
# mode never changes, and PPBase validation details).
KNOWN_DIVERGENCES: dict[str, str] = {
    "errors.refresh_admin_token.status": (
        "PPBase answers 403 'requires auth record from <collection>' to an "
        "admin token; 0.22.34 RequireSameContextRecordAuth answers 401 "
        "(apis/middlewares.go l.97-100). Status not rewritten."
    ),
    "errors.refresh_admin_token.body.code": "same case as the status",
    "errors.refresh_admin_token.body.message": "same case as the status",
    "errors.password_on_base_collection.status": (
        "PPBase answers 404 'not an auth collection'; 0.22.34 "
        "LoadCollectionContext answers 400 'Unsupported collection type.' "
        "(apis/middlewares.go l.269-271). Status not rewritten."
    ),
    "errors.password_on_base_collection.body.code": "same case as the status",
    "errors.password_on_base_collection.body.message": "same case as the status",
    "errors.file_token_guest.status": (
        "PPBase answers 401; 0.22.34 answers 400 'Failed to generate file "
        "token.' (apis/file.go l.74). Status not rewritten."
    ),
    "errors.file_token_guest.body.code": "same case as the status",
    "errors.file_token_guest.body.message": "same case as the status",
    "errors.method_not_allowed.status": (
        "PPBase (Starlette) answers 405 'Method Not Allowed.' to an unknown "
        "method on a known path; PocketBase 0.22.34 answers 404 'Not Found.'. "
        "Status not rewritten."
    ),
    "errors.method_not_allowed.body.code": "same case as the status",
    "errors.method_not_allowed.body.message": "same case as the status",
    "errors.confirm_verification_bad_token.body.data.token.code": (
        "Validation detail of an unparsable token: 0.22.34 reports the "
        "missing JWT claim (validation_invalid_token_claims); PPBase reports "
        "validation_invalid_token. Envelope, status and message match."
    ),
    "errors.confirm_verification_bad_token.body.data.token.message": (
        "same case as the data code"
    ),
}


def text_field(name: str) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "text",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {"min": None, "max": None, "pattern": ""},
    }


def bool_field(name: str) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "bool",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {},
    }


def number_field(name: str) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "number",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {"min": None, "max": None, "noDecimal": False},
    }


def json_field(name: str) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "json",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {"maxSize": 2000000},
    }


def relation_field(name: str, collection_id: str, max_select: Any) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "relation",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {
            "collectionId": collection_id,
            "cascadeDelete": False,
            "minSelect": None,
            "maxSelect": max_select,
            "displayFields": None,
        },
    }


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


async def _ok(response: httpx.Response, what: str) -> Any:
    if response.status_code not in (200, 204):
        raise RuntimeError(f"{what}: {response.status_code} {response.text[:500]}")
    return _json(response) if response.status_code == 200 else None


async def _collection(
    client: httpx.AsyncClient, admin: dict[str, str], key: str, body: dict[str, Any]
) -> None:
    await _ok(
        await client.post(
            "/api/collections",
            headers=admin,
            json={"id": C[key], "name": N[key], "type": "base", **body},
        ),
        f"collection {key}",
    )


async def _create(
    client: httpx.AsyncClient, admin: dict[str, str], key: str, rid: str, body: dict
) -> None:
    await _ok(
        await client.post(
            f"/api/collections/{N[key]}/records",
            headers=admin,
            json={"id": R[rid], **body},
        ),
        f"record {rid}",
    )


async def dataset(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    await _collection(
        client, admin, "region", {"schema": [text_field("name")], **RULES_PUBLIC}
    )
    await _collection(
        client, admin, "pdv", {"schema": [text_field("name")], **RULES_ADMIN}
    )
    await _collection(
        client,
        admin,
        "roles",
        {"schema": [text_field("name"), json_field("permission")], **RULES_ADMIN},
    )
    await _collection(
        client,
        admin,
        "users",
        {
            "type": "auth",
            "schema": [relation_field("role", C["roles"], 1)],
            **RULES_PUBLIC,
            "updateRule": None,
            "deleteRule": None,
            "options": {**AUTH_OPTIONS, "manageRule": None},
        },
    )
    await _collection(
        client,
        admin,
        "customer",
        {
            "schema": [
                text_field("name"),
                bool_field("public"),
                relation_field("readers", C["users"], None),
                relation_field("region", C["region"], 1),
                relation_field("pdv", C["pdv"], None),
            ],
            **RULES_PUBLIC,
            "listRule": CUSTOMER_RULE,
            "viewRule": CUSTOMER_RULE,
        },
    )
    users = await _ok(
        await client.get(f"/api/collections/{C['users']}", headers=admin), "users"
    )
    await _ok(
        await client.patch(
            f"/api/collections/{C['users']}",
            headers=admin,
            json={
                "schema": [
                    *users["schema"],
                    relation_field("managers", C["users"], None),
                    relation_field("customer", C["customer"], 1),
                ],
                "options": AUTH_OPTIONS,
            },
        ),
        "users.managers/customer/manageRule",
    )
    await _collection(
        client,
        admin,
        "tag",
        {"schema": [text_field("name")], **RULES_ADMIN, "viewRule": TAG_RULE},
    )
    await _collection(
        client,
        admin,
        "bcustomer",
        {
            "schema": [text_field("name")],
            **RULES_ADMIN,
            "listRule": BUMA_CUSTOMER_RULE,
            "viewRule": BUMA_CUSTOMER_RULE,
        },
    )
    await _collection(
        client,
        admin,
        "sale",
        {
            "schema": [
                text_field("label"),
                number_field("n"),
                relation_field("customer", C["customer"], 1),
                relation_field("customers", C["customer"], None),
                relation_field("user", C["users"], 1),
                relation_field("tag", C["tag"], 1),
                relation_field("bcustomer", C["bcustomer"], 1),
            ],
            **RULES_PUBLIC,
        },
    )
    await _collection(
        client,
        admin,
        "note",
        {
            "schema": [
                text_field("label"),
                relation_field("customer", C["customer"], 1),
            ],
            **RULES_ADMIN,
        },
    )

    await _create(client, admin, "region", "r1", {"name": "r1"})
    await _create(client, admin, "pdv", "p1", {"name": "p1"})
    await _create(
        client,
        admin,
        "roles",
        "role_read",
        {"name": "read", "permission": {"isAdmin": False, "customer": {"read": True}}},
    )
    await _create(
        client,
        admin,
        "roles",
        "role_none",
        {"name": "none", "permission": {"isAdmin": False, "customer": {"read": False}}},
    )
    extra = {
        "manager": {},
        "reader": {},
        "visible": {"emailVisibility": True},
        "plain": {"managers": [R["manager"]]},
        "rolereader": {"role": R["role_read"]},
        "rolenone": {"role": R["role_none"]},
    }
    for key in USERS:
        await _create(
            client,
            admin,
            "users",
            key,
            {
                "email": f"{key}@example.invalid",
                "emailVisibility": False,
                "password": PASSWORD,
                "passwordConfirm": PASSWORD,
                **extra[key],
            },
        )
    await _create(
        client,
        admin,
        "customer",
        "c1",
        {"name": "c1", "public": True, "region": R["r1"], "pdv": [R["p1"]]},
    )
    await _create(
        client,
        admin,
        "customer",
        "c2",
        {
            "name": "c2",
            "public": False,
            "readers": [R["reader"]],
            "region": R["r1"],
            "pdv": [R["p1"]],
        },
    )
    await _create(client, admin, "customer", "c3", {"name": "c3", "public": True})
    for key in ("plain", "reader"):
        await _ok(
            await client.patch(
                f"/api/collections/{N['users']}/records/{R[key]}",
                headers=admin,
                json={"customer": R["c2"]},
            ),
            f"{key}.customer",
        )
    await _create(client, admin, "tag", "t1", {"name": "t1"})
    await _create(client, admin, "bcustomer", "b1", {"name": "b1"})
    await _create(
        client,
        admin,
        "sale",
        "s1",
        {
            "label": "s1",
            "customer": R["c2"],
            "customers": [R["c2"], R["c3"], R["c1"]],
            "user": R["plain"],
            "tag": R["t1"],
            "bcustomer": R["b1"],
        },
    )
    await _create(
        client,
        admin,
        "sale",
        "s2",
        {"label": "s2", "customer": R["c1"], "user": R["visible"]},
    )
    await _create(client, admin, "note", "n1", {"label": "n1", "customer": R["c1"]})


async def caller_headers(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> dict[str, dict[str, str]]:
    headers: dict[str, dict[str, str]] = {"guest": {}, "admin": admin}
    for key in USERS:
        response = await client.post(
            f"/api/collections/{N['users']}/auth-with-password",
            json={"identity": f"{key}@example.invalid", "password": PASSWORD},
        )
        payload = await _ok(response, f"login {key}")
        headers[key] = {"Authorization": payload["token"]}
    return headers


_DROPPED = {"created", "updated", "username"}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in _DROPPED}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


async def _get(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], **params: str
) -> dict[str, Any]:
    response = await client.get(url, headers=headers, params=params)
    return {"status": response.status_code, "body": _clean(_json(response))}


async def expand_scenarios(
    client: httpx.AsyncClient, headers: dict[str, dict[str, str]]
) -> dict[str, Any]:
    sale = f"/api/collections/{N['sale']}/records"
    customer = f"/api/collections/{N['customer']}/records"
    via_sale = f"{N['sale']}_via_customer"
    via_note = f"{N['note']}_via_customer"
    views = {
        "list": (sale, {"expand": "customer,customers,user,tag", "sort": "label"}),
        "single_and_multiple": (f"{sale}/{R['s1']}", {"expand": "customer,customers"}),
        "nested_admin_only": (f"{sale}/{R['s1']}", {"expand": "customer.pdv"}),
        "nested_merged_parent": (
            f"{sale}/{R['s1']}",
            {"expand": "customer,customer.pdv"},
        ),
        "nested_public_and_admin_only": (
            f"{sale}/{R['s1']}",
            {"expand": "customer.region,customer.pdv"},
        ),
        "nested_unknown_field": (
            f"{sale}/{R['s1']}",
            {"expand": "customer.nope,customers"},
        ),
        "empty_segment": (f"{sale}/{R['s1']}", {"expand": "customer..region"}),
        "back_relations": (
            f"{customer}/{R['c1']}",
            {"expand": f"{via_sale},{via_note}"},
        ),
        "back_relation_nested": (
            f"{customer}/{R['c1']}",
            {"expand": f"{via_note}.customer,{via_sale}.user"},
        ),
        "auth_email": (f"{sale}/{R['s1']}", {"expand": "user"}),
        "auth_email_visible": (f"{sale}/{R['s2']}", {"expand": "user"}),
        "request_query": (f"{sale}/{R['s1']}", {"expand": "tag", "reveal": "1"}),
        "buma_role_rule": (f"{sale}/{R['s1']}", {"expand": "bcustomer"}),
    }
    result: dict[str, Any] = {}
    for name, (url, params) in views.items():
        result[name] = {
            caller: await _get(client, url, headers[caller], **params)
            for caller in CALLERS
        }
    return result


async def write_scenarios(
    client: httpx.AsyncClient, headers: dict[str, dict[str, str]]
) -> dict[str, Any]:
    sale = f"/api/collections/{N['sale']}/records"
    result: dict[str, Any] = {}
    for caller in ("guest", "reader", "admin"):
        record_id = f"wsa6salew{caller[:5]:0<6}"
        response = await client.post(
            sale,
            headers=headers[caller],
            params={"expand": "customer,customers,tag"},
            json={
                "id": record_id,
                "label": f"w-{caller}",
                "customer": R["c2"],
                "customers": [R["c2"], R["c1"]],
                "tag": R["t1"],
                "reveal": True,
            },
        )
        created = {"status": response.status_code, "body": _clean(_json(response))}
        response = await client.patch(
            f"{sale}/{record_id}",
            headers=headers[caller],
            params={"expand": "customer,customers,tag"},
            json={"label": f"w-{caller}-2"},
        )
        updated = {"status": response.status_code, "body": _clean(_json(response))}
        deleted = await client.delete(f"{sale}/{record_id}", headers=headers["admin"])
        result[caller] = {
            "create": created,
            "update": updated,
            "delete": deleted.status_code,
        }
    return result


async def auth_scenarios(
    client: httpx.AsyncClient, headers: dict[str, dict[str, str]]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("plain", "reader"):
        response = await client.post(
            f"/api/collections/{N['users']}/auth-with-password",
            params={"expand": "customer,managers"},
            json={"identity": f"{key}@example.invalid", "password": PASSWORD},
        )
        body = _json(response)
        login = {
            "status": response.status_code,
            "record": _clean(body.get("record")) if isinstance(body, dict) else body,
        }
        response = await client.post(
            f"/api/collections/{N['users']}/auth-refresh",
            headers=headers[key],
            params={"expand": "customer,managers"},
        )
        body = _json(response)
        refresh = {
            "status": response.status_code,
            "record": _clean(body.get("record")) if isinstance(body, dict) else body,
        }
        result[key] = {"auth_with_password": login, "auth_refresh": refresh}
    return result


async def _next_event(lines: AsyncIterator[str]) -> dict[str, str]:
    event: dict[str, str] = {}
    async for line in lines:
        if line == "":
            if event:
                return event
            continue
        name, _, value = line.partition(":")
        event[name] = value.removeprefix(" ")
    raise RuntimeError("realtime stream closed")


async def realtime_event(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    topic: str,
    trigger: Callable[[], Awaitable[None]],
) -> Any:
    async with client.stream("GET", "/api/realtime") as stream:
        lines = stream.aiter_lines()
        connect = await asyncio.wait_for(_next_event(lines), 15)
        client_id = json.loads(connect["data"])["clientId"]
        response = await client.post(
            "/api/realtime",
            headers=auth,
            json={"clientId": client_id, "subscriptions": [topic]},
        )
        if response.status_code != 204:
            return {"subscribe": response.status_code, "body": _json(response)}
        await trigger()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            event = await asyncio.wait_for(_next_event(lines), 15)
            if event.get("event") == topic:
                data = json.loads(event["data"])
                return {
                    "action": data.get("action"),
                    "record": _clean(data.get("record")),
                }
    return None


async def realtime_scenarios(
    client: httpx.AsyncClient, headers: dict[str, dict[str, str]]
) -> dict[str, Any]:
    options = quote(json.dumps({"query": {"expand": "customer,customers,user"}}))
    topic = f"{N['sale']}/{R['s1']}?options={options}"
    result: dict[str, Any] = {}
    for index, caller in enumerate(("guest", "reader", "plain", "admin")):

        async def trigger(value: int = index) -> None:
            await _ok(
                await client.patch(
                    f"/api/collections/{N['sale']}/records/{R['s1']}",
                    headers=headers["admin"],
                    json={"n": value + 1},
                ),
                "realtime trigger",
            )

        result[caller] = await realtime_event(client, headers[caller], topic, trigger)
    return result


async def error_scenarios(
    client: httpx.AsyncClient, headers: dict[str, dict[str, str]]
) -> dict[str, Any]:
    note = f"/api/collections/{N['note']}/records"
    sale = f"/api/collections/{N['sale']}/records"
    users = f"/api/collections/{N['users']}"
    raw_json = {"content-type": "application/json"}
    cases: dict[str, tuple[str, str, dict[str, Any]]] = {
        "list_admin_only": ("GET", note, {}),
        "view_admin_only": ("GET", f"{note}/{R['n1']}", {}),
        "create_admin_only": ("POST", note, {"json": {}}),
        "update_admin_only": ("PATCH", f"{note}/{R['n1']}", {"json": {}}),
        "delete_admin_only": ("DELETE", f"{note}/{R['n1']}", {}),
        "unknown_collection_list": ("GET", "/api/collections/wsa6_nope/records", {}),
        "unknown_collection_view": ("GET", "/api/collections/wsa6_nope/records/x", {}),
        "unknown_record": ("GET", f"{sale}/zzzzzzzzzzzzzzz", {}),
        "view_refused_by_rule": (
            "GET",
            f"/api/collections/{N['customer']}/records/{R['c2']}",
            {},
        ),
        "create_invalid_json": (
            "POST",
            sale,
            {"content": b"{not json", "headers": raw_json},
        ),
        "create_array_body": ("POST", sale, {"content": b"[1]", "headers": raw_json}),
        "create_missing_relation": (
            "POST",
            sale,
            {"json": {"customer": "zzzzzzzzzzzzzzz"}},
        ),
        "update_invalid_json": (
            "PATCH",
            f"{sale}/{R['s2']}",
            {"content": b"[1", "headers": raw_json},
        ),
        "refresh_without_token": ("POST", f"{users}/auth-refresh", {}),
        "refresh_admin_token": ("POST", f"{users}/auth-refresh", {"headers": "admin"}),
        "password_bad_credentials": (
            "POST",
            f"{users}/auth-with-password",
            {"json": {"identity": "nobody@example.invalid", "password": "wrong-pass1"}},
        ),
        "password_invalid_json": (
            "POST",
            f"{users}/auth-with-password",
            {"content": b"{bad", "headers": raw_json},
        ),
        "password_on_base_collection": (
            "POST",
            f"/api/collections/{N['sale']}/auth-with-password",
            {"json": {"identity": "x", "password": "y"}},
        ),
        "request_verification_missing_email": (
            "POST",
            f"{users}/request-verification",
            {"json": {}},
        ),
        "request_password_reset_missing_email": (
            "POST",
            f"{users}/request-password-reset",
            {"json": {}},
        ),
        "confirm_verification_bad_token": (
            "POST",
            f"{users}/confirm-verification",
            {"json": {"token": "bad-token"}},
        ),
        "confirm_password_reset_bad_token": (
            "POST",
            f"{users}/confirm-password-reset",
            {
                "json": {
                    "token": "bad-token",
                    "password": PASSWORD,
                    "passwordConfirm": PASSWORD,
                }
            },
        ),
        "request_email_change_guest": (
            "POST",
            f"{users}/request-email-change",
            {"json": {"newEmail": "new@example.invalid"}},
        ),
        "realtime_invalid_json": (
            "POST",
            "/api/realtime",
            {"content": b"{bad", "headers": raw_json},
        ),
        "realtime_missing_client": ("POST", "/api/realtime", {"json": {}}),
        "realtime_unknown_client": (
            "POST",
            "/api/realtime",
            {"json": {"clientId": "unknown-client", "subscriptions": []}},
        ),
        "admin_route_guest": ("GET", "/api/collections", {}),
        "admin_route_record_token": ("GET", "/api/collections", {"headers": "reader"}),
        "settings_guest": ("GET", "/api/settings", {}),
        "admin_only_filter": (
            "GET",
            sale,
            {"params": {"filter": f"@collection.{N['region']}.name = 'r1'"}},
        ),
        "invalid_filter": ("GET", sale, {"params": {"filter": "(("}}),
        "unknown_route": ("GET", "/api/wsa6-nothing", {}),
        "method_not_allowed": ("DELETE", "/api/health", {}),
        "file_token_guest": ("POST", "/api/files/token", {}),
        "admin_password_bad": (
            "POST",
            "/api/admins/auth-with-password",
            {"json": {"identity": ADMIN_EMAIL, "password": "wrong-password-1"}},
        ),
    }
    result: dict[str, Any] = {}
    for name, (method, url, options) in cases.items():
        kwargs = dict(options)
        auth = kwargs.pop("headers", None)
        if isinstance(auth, str):
            kwargs["headers"] = headers[auth]
        elif auth is not None:
            kwargs["headers"] = auth
        response = await client.request(method, url, **kwargs)
        result[name] = {"status": response.status_code, "body": _json(response)}
    return result


async def run_scenarios(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> dict[str, Any]:
    await dataset(client, admin)
    headers = await caller_headers(client, admin)
    return {
        "expand": await expand_scenarios(client, headers),
        "writes": await write_scenarios(client, headers),
        "auth": await auth_scenarios(client, headers),
        "realtime": await realtime_scenarios(client, headers),
        "errors": await error_scenarios(client, headers),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_health(client: httpx.AsyncClient, what: str) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            if (await client.get("/api/health")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"{what} did not start")
        await asyncio.sleep(0.2)


@contextlib.asynccontextmanager
async def pocketbase_client(binary: Path) -> AsyncIterator[httpx.AsyncClient]:
    with tempfile.TemporaryDirectory(prefix="ppbase-pb-expand-probe-") as root:
        flags = [
            "--dir",
            str(Path(root) / "pb_data"),
            "--migrationsDir",
            str(Path(root) / "pb_migrations"),
            "--hooksDir",
            str(Path(root) / "pb_hooks"),
            "--automigrate=false",
        ]
        await asyncio.to_thread(
            subprocess.run,
            [str(binary), "migrate", "up", *flags],
            check=True,
            capture_output=True,
        )
        created = await asyncio.to_thread(
            subprocess.run,
            [str(binary), "admin", "create", ADMIN_EMAIL, ADMIN_PASSWORD, *flags],
            check=True,
            capture_output=True,
            text=True,
        )
        if "Successfully" not in created.stdout + created.stderr:
            raise RuntimeError(f"PocketBase admin creation failed: {created.stderr}")
        port = _free_port()
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "serve",
            "--http",
            f"127.0.0.1:{port}",
            "--publicDir",
            str(Path(root) / "pb_public"),
            *flags,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=30
            ) as client:
                await _wait_health(client, "PocketBase")
                yield client
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def run_pocketbase(binary: Path) -> dict[str, Any]:
    async with pocketbase_client(binary) as client:
        response = await client.post(
            "/api/admins/auth-with-password",
            json={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        response.raise_for_status()
        return await run_scenarios(client, {"Authorization": response.json()["token"]})


def install_buma_like_role_context() -> None:
    """Emulate BUMA's flattened ``@request.auth.role.permission.*`` context."""
    import ppbase.api.records as records_api

    original = records_api._prepare_rule_context

    def patched(request, token_payload, data=None):
        auth_ctx, request_ctx = original(request, token_payload, data=data)
        live = BUMA_ROLE_CONTEXT.get(str((token_payload or {}).get("id") or ""))
        if live:
            request_ctx.setdefault("auth", {}).update(live)
        return auth_ctx, request_ctx

    records_api._prepare_rule_context = patched


async def run_ppbase(database_url: str) -> dict[str, Any]:
    import uvicorn
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ppbase.app import create_app
    from ppbase.config import Settings
    from ppbase.db.engine import get_engine
    from ppbase.db.system_tables import ParamRecord

    install_buma_like_role_context()
    with tempfile.TemporaryDirectory(prefix="ppbase-expand-probe-") as root:
        settings = Settings(
            database_url=database_url,
            jwt_secret="probe-secret-key-for-expand-rules-00000001",
            auto_migrate=False,
            apply_migrations_on_start=False,
            generate_migrations=False,
            data_dir=str(Path(root) / "data"),
            migrations_dir=str(Path(root) / "migrations"),
            hooks_dir=str(Path(root) / "hooks"),
            log_level="WARNING",
            error_compat="0.22",
        )
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings),
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="on",
            )
        )
        task = asyncio.create_task(server.serve())
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=30
            ) as client:
                await _wait_health(client, "PPBase")
                factory = async_sessionmaker(
                    bind=get_engine(), class_=AsyncSession, expire_on_commit=False
                )
                async with factory() as session:
                    row = (
                        (
                            await session.execute(
                                select(ParamRecord).where(
                                    ParamRecord.key == "_setup_token"
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                setup = (
                    row.value.get("token")
                    if row and isinstance(row.value, dict)
                    else None
                )
                body = {
                    "email": ADMIN_EMAIL,
                    "password": ADMIN_PASSWORD,
                    "passwordConfirm": ADMIN_PASSWORD,
                }
                if setup:
                    body["setupToken"] = setup
                response = await client.post("/api/admins/init", json=body)
                response.raise_for_status()
                return await run_scenarios(
                    client, {"Authorization": response.json()["token"]}
                )
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=30)


def _diff(left: Any, right: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(left, dict) and isinstance(right, dict):
        rows: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            rows.extend(
                _diff(left.get(key), right.get(key), f"{path}.{key}" if path else key)
            )
        return rows
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        rows = []
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            rows.extend(_diff(a, b, f"{path}[{index}]"))
        return rows
    if left != right:
        return [{"path": path, "pocketbase": left, "ppbase": right}]
    return []


def _known(path: str) -> str | None:
    if path in KNOWN_DIVERGENCES:
        return KNOWN_DIVERGENCES[path]
    for key, reason in KNOWN_DIVERGENCES.items():
        if key.endswith(".*") and path.startswith(key[:-2]):
            return reason
    return None


def summary(
    differences: list[dict[str, Any]], pocketbase: dict[str, Any]
) -> dict[str, Any]:
    def count(group: Any) -> int:
        if isinstance(group, dict) and ("status" in group or "action" in group):
            return 1
        if group is None:
            return 1
        if isinstance(group, dict):
            return sum(count(value) for value in group.values())
        return 1

    checks = {name: count(group) for name, group in pocketbase.items()}
    unexpected = [row for row in differences if not row["known"]]
    return {
        "checks": checks,
        "differences": len(differences),
        "known": len(differences) - len(unexpected),
        "unexpected": len(unexpected),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skip-pocketbase", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    reference = args.out / "pocketbase.json"
    if args.skip_pocketbase and reference.exists():
        pocketbase = json.loads(reference.read_text())
    else:
        pocketbase = asyncio.run(run_pocketbase(args.pocketbase))
        reference.write_text(json.dumps(pocketbase, indent=1, sort_keys=True) + "\n")
    ppbase = asyncio.run(run_ppbase(args.database_url))
    differences = _diff(pocketbase, ppbase)
    for row in differences:
        row["known"] = _known(row["path"])
    for name, payload in (("ppbase.json", ppbase), ("diff.json", differences)):
        (args.out / name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n"
        )
    result = summary(differences, pocketbase)
    import ppbase

    # Run with PYTHONPATH=<repository root>: a script's sys.path starts with
    # its own directory, not the working directory.
    result["ppbase_source"] = str(Path(ppbase.__file__).resolve().parent)
    print(json.dumps(result, indent=1))
    for row in differences:
        print(json.dumps(row, sort_keys=True)[:600])
    return 1 if result["unexpected"] else 0


if __name__ == "__main__":
    sys.exit(main())
