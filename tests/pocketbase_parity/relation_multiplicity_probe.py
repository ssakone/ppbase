"""Paired PocketBase 0.22 / PPBase probe for relation multiplicity.

Runs the same HTTP scenarios against an official PocketBase 0.22 binary and
against PPBase, then writes ``pocketbase.json``, ``ppbase.json`` and
``diff.json`` in ``--out``.  It backs the expected values of
``tests/integration/test_relation_maxselect_null.py`` and
``tests/integration/test_relation_delete_and_paths.py``.

Only synthetic records are created.  PocketBase runs in a temporary data
directory on a random loopback port and is always stopped; PPBase runs
in-process (ASGI) on ``--database-url``, which must point to an empty,
disposable PostgreSQL database.

Usage (from the repository root)::

    python tests/pocketbase_parity/relation_multiplicity_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --out /tmp/relation-probe
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

ADMIN_EMAIL = "probe-admin@example.invalid"
ADMIN_PASSWORD = "probe-Password-123"
USER_PASSWORD = "probe-User-Password-1"
PUBLIC = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}
AUTH_OPTIONS = {
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

Inject = Callable[[str, str, str, Any], Awaitable[None]]


def _rid(prefix: str, index: int) -> str:
    """A valid 15-character synthetic record id."""
    return f"{prefix}{index:0{15 - len(prefix)}d}"


def text_field(name: str, *, required: bool = False) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "text",
        "required": required,
        "presentable": False,
        "unique": False,
        "options": {"min": None, "max": None, "pattern": ""},
    }


def select_field(name: str, values: list[str], max_select: int) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "select",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {"maxSelect": max_select, "values": values},
    }


def relation_field(
    name: str,
    collection_id: str,
    max_select: int | None,
    *,
    cascade: bool = False,
    required: bool = False,
) -> dict[str, Any]:
    """A relation exactly as PocketBase 0.22 exports it (nested options)."""
    return {
        "system": False,
        "name": name,
        "type": "relation",
        "required": required,
        "presentable": False,
        "unique": False,
        "options": {
            "collectionId": collection_id,
            "cascadeDelete": cascade,
            "minSelect": None,
            "maxSelect": max_select,
            "displayFields": None,
        },
    }


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"_text": response.text[:200]}


class Labels:
    """Replace synthetic ids by stable labels in the recorded output."""

    def __init__(self) -> None:
        self.by_id: dict[str, str] = {}

    def add(self, label: str, record_id: str) -> str:
        self.by_id[record_id] = label
        return record_id

    def __call__(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, str):
            return self.by_id.get(value, value)
        return value


def _error_shape(response: httpx.Response) -> dict[str, Any]:
    body = _json(response)
    data = body.get("data") if isinstance(body, dict) else None
    return {
        "status": response.status_code,
        "message": body.get("message") if isinstance(body, dict) else None,
        "data": {
            key: value.get("code") if isinstance(value, dict) else value
            for key, value in sorted((data or {}).items())
        },
    }


async def _collection(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    body: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        "/api/collections",
        headers=admin,
        json={**PUBLIC, "type": "base", **body},
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"collection {body.get('name')} failed: {response.text[:400]}"
        )
    return response.json()


async def _create(
    client: httpx.AsyncClient,
    name: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/collections/{name}/records",
        json=payload,
        headers=headers or {},
    )
    if response.status_code != 200:
        raise RuntimeError(f"create {name} failed: {response.text[:300]}")
    return response.json()


async def _titles(
    client: httpx.AsyncClient,
    name: str,
    expression: str,
    *,
    key: str = "title",
    headers: dict[str, str] | None = None,
) -> Any:
    response = await client.get(
        f"/api/collections/{name}/records",
        params={"filter": expression, "perPage": 200, "sort": key},
        headers=headers or {},
    )
    if response.status_code != 200:
        return {"status": response.status_code}
    return sorted(item[key] for item in response.json()["items"])


async def direct_scenarios(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    labels: Labels,
    inject: Inject,
) -> dict[str, Any]:
    """Values, modifiers, direct filters, expand and the BUMA wallet rules."""
    out: dict[str, Any] = {}
    targets = await _collection(
        client, admin, {"name": "pr_rtargets", "schema": [text_field("name")]}
    )
    await _collection(
        client,
        admin,
        {
            "name": "pr_rowners",
            "schema": [
                text_field("title"),
                relation_field("rels", targets["id"], None),
                relation_field("one", targets["id"], 1),
            ],
        },
    )
    t1, t2, t3 = (labels.add(f"r{i}", _rid("prrtarget", i)) for i in (1, 2, 3))
    for record_id, name in ((t1, "alpha"), (t2, "beta"), (t3, "gamma")):
        await _create(client, "pr_rtargets", {"id": record_id, "name": name})
    missing = labels.add("rmissing", _rid("prrmissing", 0))
    joined = f"{t1},{t2}"  # 31 characters: two ids joined in one element

    creates = {
        "empty_list": {"rels": []},
        "one_element": {"rels": [t1]},
        "two_ordered": {"rels": [t2, t1]},
        "string": {"rels": t3},
        "duplicates": {"rels": [t1, t1, t2]},
        "json_string": {"rels": json.dumps([t2, t3])},
        "absent": {},
        "null": {"rels": None},
        "empty_string": {"rels": ""},
        "single_gets_list": {"one": [t1, t2]},
    }
    out["creates"] = {}
    for title, payload in creates.items():
        record = await _create(client, "pr_rowners", {"title": title, **payload})
        out["creates"][title] = [labels(record["rels"]), labels(record["one"])]
    response = await client.post(
        "/api/collections/pr_rowners/records",
        files=[("title", (None, "mp")), ("rels", (None, t1)), ("rels", (None, t3))],
    )
    out["creates"]["multipart_two"] = labels(_json(response).get("rels"))
    response = await client.post(
        "/api/collections/pr_rowners/records",
        files=[("title", (None, "mp1")), ("rels", (None, t2))],
    )
    out["creates"]["multipart_one"] = labels(_json(response).get("rels"))

    out["orphans"] = {}
    for key, payload in (
        ("multi_missing", {"rels": [missing]}),
        ("multi_mixed", {"rels": [t1, missing]}),
        ("multi_joined", {"rels": [joined]}),
        ("single_missing", {"one": missing}),
    ):
        response = await client.post(
            "/api/collections/pr_rowners/records", json={"title": "bad", **payload}
        )
        out["orphans"][key] = _error_shape(response)["data"]

    modified = await _create(client, "pr_rowners", {"title": "mod", "rels": [t1]})
    out["modifiers"] = []
    for body in (
        {"rels+": [t2, t1]},
        {"rels-": [t1]},
        {"rels": t3},
        {"rels": [t3, t2, t3]},
        {"rels": []},
        {"one": [t3, t1]},
    ):
        response = await client.patch(
            f"/api/collections/pr_rowners/records/{modified['id']}", json=body
        )
        body_json = _json(response)
        out["modifiers"].append(
            [
                response.status_code,
                labels(body_json.get("rels")),
                labels(body_json.get("one")),
            ]
        )

    await _collection(
        client,
        admin,
        {
            "name": "pr_fowners",
            "schema": [
                text_field("title"),
                relation_field("rels", targets["id"], None),
            ],
        },
    )
    for title, rels in (
        ("dups", [t1, t2]),
        ("empty", []),
        ("one", [t1]),
        ("string", [t3]),
        ("two", [t2, t1]),
    ):
        await _create(client, "pr_fowners", {"title": title, "rels": rels})
    filters = {
        "eq": f'rels = "{t1}"',
        "neq": f'rels != "{t1}"',
        "any_eq": f'rels ?= "{t1}"',
        "any_neq": f'rels ?!= "{t1}"',
        "eq_empty": 'rels = ""',
        "neq_empty": 'rels != ""',
        "eq_null": "rels = null",
        "eq_json_empty": 'rels = "[]"',
        "eq_json_one": f"rels = '[\"{t1}\"]'",
        "like": f'rels ~ "{t1}"',
        "not_like": f'rels !~ "{t1}"',
        "any_like": f'rels ?~ "{t1}"',
        "traverse_any_name": 'rels.name ?= "alpha"',
        "traverse_any_like": 'rels.name ?~ "alp"',
        "traverse_any_id": f'rels.id ?= "{t1}"',
        "traverse_eq_name_match_all": 'rels.name = "alpha"',
        "traverse_eq_id_match_all": f'rels.id = "{t1}"',
        "traverse_neq_name_match_all": 'rels.name != "alpha"',
        "length_2": "rels:length = 2",
        "length_0": "rels:length = 0",
    }
    out["filters"] = {
        key: await _titles(client, "pr_fowners", expression)
        for key, expression in filters.items()
    }
    out["back_relation"] = await _titles(
        client,
        "pr_rtargets",
        'pr_fowners_via_rels.title ?= "one"',
        key="name",
    )

    expand_owner = await _create(
        client, "pr_fowners", {"title": "zexpand", "rels": [t2, t1]}
    )
    orphan_owner = await _create(client, "pr_fowners", {"title": "zorphan"})
    await inject("pr_fowners", orphan_owner["id"], "rels", [missing, t1])
    out["expand"] = {}
    for record in (expand_owner, orphan_owner):
        response = await client.get(
            f"/api/collections/pr_fowners/records/{record['id']}",
            params={"expand": "rels"},
        )
        body = _json(response)
        out["expand"][body["title"]] = {
            "rels": labels(body.get("rels")),
            "expand": [
                item["name"] for item in (body.get("expand") or {}).get("rels", [])
            ],
        }

    await _collection(
        client,
        admin,
        {
            "name": "pr_wpeople",
            "type": "auth",
            "schema": [text_field("name")],
            "options": AUTH_OPTIONS,
        },
    )
    people = await client.get("/api/collections/pr_wpeople", headers=admin)
    users: dict[str, str] = {}
    for label in ("u1", "u2", "u3"):
        record = await _create(
            client,
            "pr_wpeople",
            {
                "id": labels.add(f"w{label}", _rid(f"prwpeople{label}", 0)),
                "email": f"probe-w{label}@example.invalid",
                "password": USER_PASSWORD,
                "passwordConfirm": USER_PASSWORD,
            },
        )
        users[label] = record["id"]
    await _collection(
        client,
        admin,
        {
            "name": "pr_wallets",
            "schema": [
                text_field("name"),
                relation_field("managers", people.json()["id"], None),
            ],
            "listRule": "@request.auth.id != '' && (managers ~ @request.auth.id  )",
            "viewRule": "@request.auth.id != '' && (@request.auth.id ?= managers)",
            "createRule": None,
            "updateRule": None,
            "deleteRule": None,
        },
    )
    wallets: dict[str, str] = {}
    for name, managers in (
        ("w1", ["u1"]),
        ("w2", ["u2", "u1"]),
        ("w3", []),
        ("w4", ["u3"]),
    ):
        record = await _create(
            client,
            "pr_wallets",
            {"name": name, "managers": [users[m] for m in managers]},
            headers=admin,
        )
        wallets[name] = record["id"]
    await inject(
        "pr_wallets", wallets["w4"], "managers", [f"{users['u1']},{users['u3']}"]
    )
    out["wallets"] = {}
    for label in ("u1", "u2", "u3"):
        response = await client.post(
            "/api/collections/pr_wpeople/auth-with-password",
            json={
                "identity": f"probe-w{label}@example.invalid",
                "password": USER_PASSWORD,
            },
        )
        token = {"Authorization": response.json()["token"]}
        listed = await _titles(client, "pr_wallets", "", key="name", headers=token)
        views = {}
        for name, record_id in wallets.items():
            response = await client.get(
                f"/api/collections/pr_wallets/records/{record_id}", headers=token
            )
            views[name] = response.status_code
        out["wallets"][label] = {"list": listed, "view": views}
    return out


async def path_scenarios(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    labels: Labels,
) -> dict[str, Any]:
    """Filters whose path ends on, or iterates, a multi-value field."""
    out: dict[str, Any] = {}
    targets = await _collection(
        client, admin, {"name": "pr_targets", "schema": [text_field("name")]}
    )
    customers = await _collection(
        client,
        admin,
        {
            "name": "pr_customers",
            "schema": [
                text_field("name"),
                relation_field("pdv", targets["id"], None),
                relation_field("one", targets["id"], 1),
                select_field("tags", ["a", "b", "c", "ab"], 3),
            ],
        },
    )
    await _collection(
        client,
        admin,
        {
            "name": "pr_sales",
            "schema": [
                text_field("title"),
                relation_field("customer", customers["id"], 1),
            ],
        },
    )
    t1, t2, t3 = (labels.add(f"t{i}", _rid("prtarget", i)) for i in (1, 2, 3))
    for record_id, name in ((t1, "alpha"), (t2, "beta"), (t3, "gamma")):
        await _create(client, "pr_targets", {"id": record_id, "name": name})
    customer_rows = {
        "c1": {"pdv": [t1], "one": t1, "tags": ["a"]},
        "c2": {"pdv": [t2, t1], "one": "", "tags": ["a", "b"]},
        "c3": {"pdv": [], "one": t2, "tags": []},
        "c4": {"pdv": [t3], "one": "", "tags": ["ab"]},
    }
    customer_ids: dict[str, str] = {}
    for index, (name, values) in enumerate(customer_rows.items(), start=1):
        record_id = labels.add(name, _rid("prcust", index))
        customer_ids[name] = record_id
        await _create(client, "pr_customers", {"id": record_id, "name": name, **values})
    for title, customer in (
        ("s1", "c1"),
        ("s2", "c2"),
        ("s3", "c3"),
        ("s4", "c4"),
        ("s5", None),
    ):
        await _create(
            client,
            "pr_sales",
            {"title": title, "customer": customer_ids[customer] if customer else ""},
        )

    sale_filters = {
        "leaf_eq_id": f'customer.pdv = "{t1}"',
        "leaf_eq_json": f"customer.pdv = '[\"{t1}\"]'",
        "leaf_any_eq_id": f'customer.pdv ?= "{t1}"',
        "leaf_like": f'customer.pdv ~ "{t1}"',
        "leaf_not_like": f'customer.pdv !~ "{t1}"',
        "leaf_any_like": f'customer.pdv ?~ "{t1}"',
        "leaf_eq_json_empty": 'customer.pdv = "[]"',
        "leaf_eq_empty": 'customer.pdv = ""',
        "leaf_neq_empty": 'customer.pdv != ""',
        "leaf_length_2": "customer.pdv:length = 2",
        "leaf_length_0": "customer.pdv:length = 0",
        "leaf_length_gt_0": "customer.pdv:length > 0",
        "leaf_each_eq": f'customer.pdv:each = "{t1}"',
        "leaf_each_any_eq": f'customer.pdv:each ?= "{t1}"',
        "leaf_traverse_any_name": 'customer.pdv.name ?= "alpha"',
        "leaf_single_length_1": "customer.one:length = 1",
        "leaf_select_like": 'customer.tags ~ "b"',
        "leaf_select_each_any": 'customer.tags:each ?= "a"',
    }
    out["sales"] = {
        key: await _titles(client, "pr_sales", expression)
        for key, expression in sale_filters.items()
    }
    customer_filters = {
        "each_eq": f'pdv:each = "{t1}"',
        "each_any_eq": f'pdv:each ?= "{t1}"',
        "each_neq": f'pdv:each != "{t1}"',
        "each_any_neq": f'pdv:each ?!= "{t1}"',
        "each_like": 'pdv:each ~ "prtarget"',
        "each_eq_empty": 'pdv:each = ""',
        "single_each_eq": f'one:each = "{t1}"',
        "single_length_1": "one:length = 1",
        "single_length_0": "one:length = 0",
        "select_each_eq": 'tags:each = "a"',
        "select_each_any_eq": 'tags:each ?= "a"',
        "select_length_2": "tags:length = 2",
        "select_any_eq": 'tags ?= "a"',
        "select_eq": 'tags = "a"',
        "select_like": 'tags ~ "a"',
        "select_eq_json": "tags = '[\"a\"]'",
    }
    out["customers"] = {
        key: await _titles(client, "pr_customers", expression, key="name")
        for key, expression in customer_filters.items()
    }
    target_filters = {
        "back_any_name": 'pr_customers_via_pdv.name ?= "c2"',
        "back_leaf_any_like": f'pr_customers_via_pdv.pdv ?~ "{t2}"',
        "back_single_any_name": 'pr_customers_via_one.name ?= "c1"',
    }
    out["targets"] = {
        key: await _titles(client, "pr_targets", expression, key="name")
        for key, expression in target_filters.items()
    }
    collection_filters = {
        "collection_leaf_any_like": f'@collection.pr_customers.pdv ?~ "{t3}"',
        "collection_leaf_any_eq_json": f"@collection.pr_customers.pdv ?= '[\"{t3}\"]'",
        "collection_leaf_any_length": "@collection.pr_customers.pdv:length ?= 2",
        "collection_name_eq_match_all": '@collection.pr_customers.name = "c1"',
        "collection_name_any_eq": '@collection.pr_customers.name ?= "c1"',
    }
    out["collection"] = {
        key: await _titles(client, "pr_sales", expression, headers=admin)
        for key, expression in collection_filters.items()
    }
    return out


async def rule_scenarios(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    labels: Labels,
) -> dict[str, Any]:
    """Rules: @collection multi-match and list-valued @request.data."""
    out: dict[str, Any] = {}
    await _collection(
        client,
        admin,
        {
            "name": "pr_people",
            "type": "auth",
            "schema": [text_field("name")],
            "options": AUTH_OPTIONS,
        },
    )
    for label in ("u1", "u2"):
        await _create(
            client,
            "pr_people",
            {
                "id": labels.add(label, _rid(f"prpeople{label}", 0)),
                "email": f"probe-{label}@example.invalid",
                "password": USER_PASSWORD,
                "passwordConfirm": USER_PASSWORD,
            },
        )
    response = await client.post(
        "/api/collections/pr_people/auth-with-password",
        json={"identity": "probe-u1@example.invalid", "password": USER_PASSWORD},
    )
    response.raise_for_status()
    user = {"Authorization": response.json()["token"]}
    for name, rule in (
        ("pr_docs_eq", "@collection.pr_people.id = @request.auth.id"),
        ("pr_docs_any", "@collection.pr_people.id ?= @request.auth.id"),
    ):
        await _collection(
            client,
            admin,
            {"name": name, "schema": [text_field("title")], "listRule": rule},
        )
        for title in ("d1", "d2"):
            await _create(client, name, {"title": title}, headers=admin)
        out[name] = await _titles(client, name, "", headers=user)

    for name, rule in (
        ("pr_req_any", '@request.data.tags ?= "b"'),
        ("pr_req_json", '@request.data.tags = \'["a","b"]\''),
    ):
        await _collection(
            client,
            admin,
            {
                "name": name,
                "schema": [
                    text_field("title"),
                    select_field("tags", ["a", "b"], 2),
                ],
                "createRule": rule,
            },
        )
        response = await client.post(
            f"/api/collections/{name}/records",
            json={"title": "x", "tags": ["a", "b"]},
        )
        out[name] = response.status_code
    return out


async def delete_scenarios(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    labels: Labels,
) -> dict[str, Any]:
    """Deleting a referenced record cleans references like DeleteRecord."""
    out: dict[str, Any] = {}
    targets = await _collection(
        client, admin, {"name": "pr_dtargets", "schema": [text_field("name")]}
    )
    await _collection(
        client,
        admin,
        {
            "name": "pr_downers",
            "schema": [
                text_field("title"),
                relation_field("rels", targets["id"], None),
                relation_field("one", targets["id"], 1),
                relation_field("casc_multi", targets["id"], None, cascade=True),
                relation_field("casc_one", targets["id"], 1, cascade=True),
            ],
        },
    )
    await _collection(
        client,
        admin,
        {
            "name": "pr_drequired",
            "schema": [
                text_field("title"),
                relation_field("req_one", targets["id"], 1, required=True),
            ],
        },
    )
    d = {
        index: labels.add(f"d{index}", _rid("prdtarget", index))
        for index in range(1, 7)
    }
    for index, record_id in d.items():
        await _create(client, "pr_dtargets", {"id": record_id, "name": f"d{index}"})
    owners = {
        "o1": {"rels": [d[1], d[2]], "one": d[1]},
        "o3": {"casc_multi": [d[3], d[4]]},
        "o4": {"casc_one": d[5]},
        "o5": {"rels": [d[1]]},
    }
    owner_ids: dict[str, str] = {}
    for index, (title, values) in enumerate(owners.items(), start=1):
        owner_ids[title] = labels.add(title, _rid("prdowner", index))
        await _create(
            client, "pr_downers", {"id": owner_ids[title], "title": title, **values}
        )
    required_id = labels.add("r1", _rid("prdreq", 1))
    await _create(
        client, "pr_drequired", {"id": required_id, "title": "r1", "req_one": d[6]}
    )

    async def owner(title: str) -> dict[str, Any] | int:
        response = await client.get(
            f"/api/collections/pr_downers/records/{owner_ids[title]}"
        )
        if response.status_code != 200:
            return response.status_code
        return response.json()

    before = await owner("o1")
    await asyncio.sleep(0.01)
    response = await client.delete(
        f"/api/collections/pr_dtargets/records/{d[1]}", headers=admin
    )
    out["delete_d1"] = response.status_code
    after = await owner("o1")
    assert isinstance(before, dict) and isinstance(after, dict)
    out["o1_after_delete"] = {
        "rels": labels(after["rels"]),
        "one": labels(after["one"]),
        "updated_changed": after["updated"] != before["updated"],
    }
    o5 = await owner("o5")
    out["o5_after_delete"] = labels(o5["rels"]) if isinstance(o5, dict) else o5
    response = await client.patch(
        f"/api/collections/pr_downers/records/{owner_ids['o1']}",
        json={"title": "o1b"},
    )
    out["patch_o1_title"] = response.status_code

    response = await client.delete(
        f"/api/collections/pr_dtargets/records/{d[6]}", headers=admin
    )
    out["delete_required_target"] = _error_shape(response)
    response = await client.get(f"/api/collections/pr_dtargets/records/{d[6]}")
    out["required_target_still_exists"] = response.status_code

    await client.delete(f"/api/collections/pr_dtargets/records/{d[3]}", headers=admin)
    o3 = await owner("o3")
    out["o3_after_first_cascade"] = (
        labels(o3["casc_multi"]) if isinstance(o3, dict) else o3
    )
    await client.delete(f"/api/collections/pr_dtargets/records/{d[4]}", headers=admin)
    out["o3_after_last_cascade"] = await owner("o3")
    await client.delete(f"/api/collections/pr_dtargets/records/{d[5]}", headers=admin)
    out["o4_after_cascade"] = await owner("o4")
    return out


async def validation_scenarios(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    labels: Labels,
    inject: Inject,
) -> dict[str, Any]:
    """Errors returned together with a missing relation target."""
    out: dict[str, Any] = {}
    targets = await _collection(
        client, admin, {"name": "pr_vtargets", "schema": [text_field("name")]}
    )
    await _collection(
        client,
        admin,
        {
            "name": "pr_valid",
            "schema": [
                text_field("title", required=True),
                relation_field("rels", targets["id"], None),
                relation_field("lim", targets["id"], 2),
            ],
        },
    )
    t1, t2 = (labels.add(f"v{i}", _rid("prvtarget", i)) for i in (1, 2))
    for record_id in (t1, t2):
        await _create(client, "pr_vtargets", {"id": record_id, "name": record_id})
    missing = labels.add("missing", _rid("prvmissing", 0))

    response = await client.post(
        "/api/collections/pr_valid/records", json={"rels": [t1, missing]}
    )
    out["anonymous_create_orphan_and_required"] = _error_shape(response)
    response = await client.post(
        "/api/collections/pr_valid/records",
        json={"rels": [t1, missing]},
        headers=admin,
    )
    out["admin_create_orphan_and_required"] = _error_shape(response)
    response = await client.post(
        "/api/collections/pr_valid/records",
        json={"title": "x", "lim": [t1, t2, missing]},
    )
    out["create_too_many_with_orphan"] = _error_shape(response)
    # @request.data.<relation>:length counts list.ToUniqueStringSlice(value):
    # '["v1",1]' decodes partially to ["v1", '["v1",1]'] (2 items), so the
    # update rule passes and the form then reports the unknown second id.
    await _collection(
        client,
        admin,
        {
            "name": "pr_vlength",
            "schema": [
                text_field("title"),
                relation_field("rels", targets["id"], None),
            ],
            "updateRule": "@request.data.rels:length = 2",
        },
    )
    counted = await _create(
        client, "pr_vlength", {"title": "len", "rels": [t1]}, headers=admin
    )
    response = await client.patch(
        f"/api/collections/pr_vlength/records/{counted['id']}",
        json={"rels": json.dumps([t1, 1])},
    )
    out["length_of_partially_decoded_json"] = _error_shape(response)
    created = await _create(client, "pr_valid", {"title": "ok", "rels": [t1]})
    await inject("pr_valid", created["id"], "rels", [missing, t1])
    response = await client.patch(
        f"/api/collections/pr_valid/records/{created['id']}",
        json={"title": "ok2"},
    )
    out["patch_title_with_stored_orphan"] = _error_shape(response)
    return out


async def run_scenarios(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    inject: Inject,
) -> dict[str, Any]:
    labels = Labels()
    return {
        "direct": await direct_scenarios(client, admin, labels, inject),
        "paths": await path_scenarios(client, admin, labels),
        "rules": await rule_scenarios(client, admin, labels),
        "delete": await delete_scenarios(client, admin, labels),
        "validation": await validation_scenarios(client, admin, labels, inject),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def pocketbase_client(
    binary: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, Path]]:
    with tempfile.TemporaryDirectory(prefix="ppbase-pb-probe-") as root:
        data_dir = str(Path(root) / "pb_data")
        # Keep every PocketBase directory inside the temporary root and never
        # write migration files (the default migrations dir is a sibling of
        # the data dir).
        flags = [
            "--dir",
            data_dir,
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
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            async with httpx.AsyncClient(base_url=base, timeout=30) as client:
                while True:
                    try:
                        if (await client.get("/api/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("PocketBase did not start")
                    await asyncio.sleep(0.2)
                yield client, Path(data_dir) / "data.db"
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def run_pocketbase(binary: Path) -> dict[str, Any]:
    async with pocketbase_client(binary) as (client, data_db):

        async def inject(table: str, record_id: str, field: str, value: Any) -> None:
            with sqlite3.connect(data_db, timeout=10) as connection:
                connection.execute(
                    f'UPDATE "{table}" SET "{field}" = ? WHERE id = ?',
                    (json.dumps(value), record_id),
                )

        response = await client.post(
            "/api/admins/auth-with-password",
            json={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        response.raise_for_status()
        admin = {"Authorization": response.json()["token"]}
        return await run_scenarios(client, admin, inject)


async def run_ppbase(database_url: str) -> dict[str, Any]:
    from httpx import ASGITransport
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ppbase.app import create_app
    from ppbase.config import Settings
    from ppbase.db.bootstrap import bootstrap_system_collections
    from ppbase.db.engine import close_engine, init_engine
    from ppbase.db.system_tables import CollectionRecord, create_system_tables
    from ppbase.services.admin_service import create_admin
    from ppbase.services.auth_service import create_admin_token

    secret = "probe-secret-key-for-relation-multiplicity-0001"
    with tempfile.TemporaryDirectory(prefix="ppbase-probe-data-") as data_dir:
        settings = Settings(
            database_url=database_url,
            jwt_secret=secret,
            auto_migrate=False,
            data_dir=data_dir,
        )
        app = create_app(settings)
        engine = await init_engine(database_url, pool_size=5, max_overflow=5)
        try:
            await create_system_tables(engine)
            factory = async_sessionmaker(
                bind=engine, class_=AsyncSession, expire_on_commit=False
            )
            async with factory() as session, session.begin():
                await bootstrap_system_collections(session, engine)
            async with factory() as session:
                admin_record = await create_admin(session, ADMIN_EMAIL, ADMIN_PASSWORD)
                await session.commit()
                superusers = (
                    (
                        await session.execute(
                            select(CollectionRecord).where(
                                CollectionRecord.name == "_superusers"
                            )
                        )
                    )
                    .scalars()
                    .first()
                )

            class _TokenSettings:
                admin_token_duration = 86400

                def get_jwt_secret(self) -> str:
                    return secret

            token = create_admin_token(
                admin_record, _TokenSettings(), superusers_collection=superusers
            )

            async def inject(
                table: str, record_id: str, field: str, value: Any
            ) -> None:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(f'UPDATE "{table}" SET "{field}" = :value WHERE id = :id'),
                        {"value": value, "id": record_id},
                    )

            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver", timeout=60
            ) as client:
                return await run_scenarios(client, {"Authorization": token}, inject)
        finally:
            await close_engine()


def _diff(left: Any, right: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(left, dict) and isinstance(right, dict):
        rows: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            rows.extend(
                _diff(left.get(key), right.get(key), f"{path}.{key}" if path else key)
            )
        return rows
    if left != right:
        return [{"path": path, "pocketbase": left, "ppbase": right}]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    pocketbase = asyncio.run(run_pocketbase(args.pocketbase))
    ppbase = asyncio.run(run_ppbase(args.database_url))
    differences = _diff(pocketbase, ppbase)
    for name, payload in (
        ("pocketbase.json", pocketbase),
        ("ppbase.json", ppbase),
        ("diff.json", differences),
    ):
        (args.out / name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n"
        )
    print(json.dumps({"differences": len(differences)}, indent=1))
    for row in differences:
        print(json.dumps(row, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
