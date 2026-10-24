"""Relation paths, ``:each``/``:length``, rules, deletes and errors (D-0019).

Every expected value comes from the official PocketBase 0.22.34 binary: the
synthetic scenarios of ``tests/pocketbase_parity/relation_multiplicity_probe.py``
send the same requests to PocketBase and to PPBase.  The multi-match and
missing-relation (LEFT JOIN) cases follow PocketBase since the WS-A3 change.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ppbase.db.engine import get_engine
from ppbase.db.schema_manager import (
    MultiValueShapeMismatchError,
    find_multi_value_shape_mismatches,
    reconcile_multi_value_columns,
)
from ppbase.db.system_tables import Base
from ppbase.services.record_service import resolve_collection

pytestmark = pytest.mark.asyncio

_PUBLIC_RULES = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}
_PASSWORD = "ws-a-User-Password-1"


def _text(name: str, *, required: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "type": "text",
        "required": required,
        "options": {"min": None, "max": None, "pattern": ""},
    }


def _select(name: str, values: list[str], max_select: int) -> dict[str, Any]:
    return {
        "name": name,
        "type": "select",
        "required": False,
        "options": {"maxSelect": max_select, "values": values},
    }


def _relation(
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


def _rid(prefix: str, index: int) -> str:
    return f"{prefix}{index:0{15 - len(prefix)}d}"


class _Env:
    """Collections created for one test, deleted afterwards."""

    def __init__(self, client: AsyncClient, token: str) -> None:
        self.client = client
        self.admin = {"Authorization": token}
        self.suffix = uuid.uuid4().hex[:6]
        self.created: list[str] = []

    def name(self, base: str) -> str:
        return f"{base}_{self.suffix}"

    async def collection(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            "/api/collections",
            headers=self.admin,
            json={**_PUBLIC_RULES, "type": "base", **body},
        )
        assert response.status_code == 200, response.text
        self.created.append(response.json()["id"])
        return response.json()

    async def create(
        self,
        collection: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self.client.post(
            f"/api/collections/{collection}/records",
            json=payload,
            headers=headers or {},
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def titles(
        self,
        collection: str,
        expression: str,
        *,
        key: str = "title",
        headers: dict[str, str] | None = None,
    ) -> list[str]:
        response = await self.client.get(
            f"/api/collections/{collection}/records",
            params={"filter": expression, "perPage": 200, "sort": key},
            headers=headers or {},
        )
        assert response.status_code == 200, (expression, response.text)
        return sorted(item[key] for item in response.json()["items"])

    async def cleanup(self) -> None:
        for collection_id in reversed(self.created):
            await self.client.delete(
                f"/api/collections/{collection_id}", headers=self.admin
            )


@pytest_asyncio.fixture
async def env(app_client: AsyncClient, admin_token: str) -> AsyncIterator[_Env]:
    value = _Env(app_client, admin_token)
    try:
        yield value
    finally:
        await value.cleanup()


async def _path_env(env: _Env) -> dict[str, Any]:
    """targets <- customers.pdv (maxSelect null) <- sales.customer."""
    targets = await env.collection(
        {"name": env.name("pt_targets"), "schema": [_text("name")]}
    )
    customers = await env.collection(
        {
            "name": env.name("pt_customers"),
            "schema": [
                _text("name"),
                _relation("pdv", targets["id"], None),
                _relation("one", targets["id"], 1),
                _select("tags", ["a", "b", "c", "ab"], 3),
            ],
        }
    )
    sales = await env.collection(
        {
            "name": env.name("pt_sales"),
            "schema": [_text("title"), _relation("customer", customers["id"], 1)],
        }
    )
    ids = {
        key: _rid(f"pt{env.suffix}", index)
        for index, key in enumerate(("t1", "t2", "t3"))
    }
    for key, name in (("t1", "alpha"), ("t2", "beta"), ("t3", "gamma")):
        await env.create(targets["name"], {"id": ids[key], "name": name})
    t1, t2, t3 = ids["t1"], ids["t2"], ids["t3"]
    customer_ids: dict[str, str] = {}
    for name, values in (
        ("c1", {"pdv": [t1], "one": t1, "tags": ["a"]}),
        ("c2", {"pdv": [t2, t1], "one": "", "tags": ["a", "b"]}),
        ("c3", {"pdv": [], "one": t2, "tags": []}),
        ("c4", {"pdv": [t3], "one": "", "tags": ["ab"]}),
    ):
        customer_ids[name] = (
            await env.create(customers["name"], {"name": name, **values})
        )["id"]
    for title, customer in (
        ("s1", "c1"),
        ("s2", "c2"),
        ("s3", "c3"),
        ("s4", "c4"),
        ("s5", None),
    ):
        await env.create(
            sales["name"],
            {"title": title, "customer": customer_ids[customer] if customer else ""},
        )
    return {
        "targets": targets["name"],
        "customers": customers["name"],
        "sales": sales["name"],
        **ids,
    }


async def test_paths_ending_on_a_multi_value_field_match_pocketbase(
    env: _Env,
) -> None:
    """The last segment is compared like a direct field: JSON text, :length, :each."""
    p = await _path_env(env)
    t1, t3 = p["t1"], p["t3"]
    expected = {
        f'customer.pdv = "{t1}"': [],
        f"customer.pdv = '[\"{t1}\"]'": ["s1"],
        f'customer.pdv ?= "{t1}"': [],
        f'customer.pdv ~ "{t1}"': ["s1", "s2"],
        f'customer.pdv !~ "{t1}"': ["s3", "s4"],
        f'customer.pdv ?~ "{t1}"': ["s1", "s2"],
        'customer.pdv = "[]"': ["s3"],
        'customer.pdv != ""': ["s1", "s2", "s3", "s4"],
        "customer.pdv:length = 2": ["s2"],
        "customer.pdv:length > 0": ["s1", "s2", "s4"],
        f'customer.pdv:each = "{t1}"': ["s1"],
        f'customer.pdv:each ?= "{t1}"': ["s1", "s2"],
        'customer.pdv.name ?= "alpha"': ["s1", "s2"],
        "customer.one:length = 1": ["s1", "s3"],
        'customer.tags ~ "b"': ["s2", "s4"],
        'customer.tags:each ?= "a"': ["s1", "s2"],
    }
    for expression, titles in expected.items():
        assert await env.titles(p["sales"], expression) == titles, expression

    back = f"{p['customers']}_via_pdv"
    assert await env.titles(p["targets"], f'{back}.name ?= "c2"', key="name") == [
        "alpha",
        "beta",
    ]
    assert await env.titles(p["targets"], f'{back}.pdv ?~ "{p["t2"]}"', key="name") == [
        "alpha",
        "beta",
    ]

    all_sales = ["s1", "s2", "s3", "s4", "s5"]
    for expression in (
        f'@collection.{p["customers"]}.pdv ?~ "{t3}"',
        f"@collection.{p['customers']}.pdv ?= '[\"{t3}\"]'",
        f"@collection.{p['customers']}.pdv:length ?= 2",
        f'@collection.{p["customers"]}.name ?= "c1"',
    ):
        assert (
            await env.titles(p["sales"], expression, headers=env.admin) == all_sales
        ), expression


async def test_missing_relation_leaf_is_null_like_pocketbase(env: _Env) -> None:
    """PocketBase LEFT JOINs the relation: s5 (no customer) has a NULL leaf."""
    p = await _path_env(env)
    assert await env.titles(p["sales"], 'customer.pdv = ""') == ["s5"]
    assert await env.titles(p["sales"], "customer.pdv:length = 0") == ["s3", "s5"]


async def test_collection_reference_match_all_like_pocketbase(env: _Env) -> None:
    """A non-``?`` @collection comparison needs every row (multi-match)."""
    p = await _path_env(env)
    expression = f'@collection.{p["customers"]}.name = "c1"'
    assert await env.titles(p["sales"], expression, headers=env.admin) == []


async def test_each_and_length_follow_pocketbase(env: _Env) -> None:
    """``:each``: ``?`` operators match any item; others need a non-empty list
    whose items all match.  ``:length`` counts a single value as one item."""
    p = await _path_env(env)
    t1 = p["t1"]
    expected = {
        f'pdv:each = "{t1}"': ["c1"],
        f'pdv:each ?= "{t1}"': ["c1", "c2"],
        f'pdv:each != "{t1}"': ["c4"],
        f'pdv:each ?!= "{t1}"': ["c2", "c3", "c4"],
        f'pdv:each ~ "pt{env.suffix}"': ["c1", "c2", "c4"],
        'pdv:each = ""': ["c3"],
        f'one:each = "{t1}"': ["c1"],
        "one:length = 1": ["c1", "c3"],
        "one:length = 0": ["c2", "c4"],
        'tags:each = "a"': ["c1"],
        'tags:each ?= "a"': ["c1", "c2"],
        "tags:length = 2": ["c2"],
        # A direct multi-value field is its JSON text (current format too).
        'tags ?= "a"': [],
        'tags = "a"': [],
        'tags ~ "a"': ["c1", "c2", "c4"],
        "tags = '[\"a\"]'": ["c1"],
    }
    for expression, names in expected.items():
        assert await env.titles(p["customers"], expression, key="name") == names, (
            expression
        )


async def test_current_format_multi_value_fields_compare_json_text(
    env: _Env,
) -> None:
    """Behaviour change for existing collections (flat fields, maxSelect > 1).

    Same results as PocketBase 0.22.34 with ``maxSelect`` 3/5 (probe:
    ``tags ?= "a"`` -> [], ``tags = "a"`` -> [], ``tags ~ "a"`` -> substring):
    before D-0019, PPBase treated ``?=`` as an element match and answered 500
    for ``=``.
    """
    targets = await env.collection(
        {"name": env.name("cf_targets"), "schema": [_text("name")]}
    )
    flat = await env.collection(
        {
            "name": env.name("cf_items"),
            "fields": [
                {"name": "title", "type": "text"},
                {
                    "name": "tags",
                    "type": "select",
                    "maxSelect": 3,
                    "values": ["a", "b", "ab"],
                },
                {
                    "name": "many",
                    "type": "relation",
                    "collectionId": targets["id"],
                    "maxSelect": 5,
                },
            ],
        }
    )
    t1 = (await env.create(targets["name"], {"name": "alpha"}))["id"]
    t2 = (await env.create(targets["name"], {"name": "beta"}))["id"]
    await env.create(flat["name"], {"title": "x", "tags": ["a"], "many": [t1]})
    await env.create(
        flat["name"], {"title": "y", "tags": ["ab", "b"], "many": [t2, t1]}
    )
    await env.create(flat["name"], {"title": "z", "tags": [], "many": []})
    expected = {
        'tags ?= "a"': [],
        'tags = "a"': [],
        'tags ~ "a"': ["x", "y"],
        "tags = '[\"a\"]'": ["x"],
        f'many ?= "{t1}"': [],
        f'many = "{t1}"': [],
        f'many ~ "{t1}"': ["x", "y"],
        f'many.id ?= "{t1}"': ["x", "y"],
        'tags:each ?= "b"': ["y"],
    }
    for expression, titles in expected.items():
        assert await env.titles(flat["name"], expression) == titles, expression


async def _auth_env(env: _Env) -> tuple[str, dict[str, str]]:
    people = await env.collection(
        {
            "name": env.name("rp_people"),
            "type": "auth",
            "schema": [_text("name")],
        }
    )
    for label in ("u1", "u2"):
        await env.create(
            people["name"],
            {
                "email": f"{label}-{env.suffix}@example.invalid",
                "password": _PASSWORD,
                "passwordConfirm": _PASSWORD,
            },
        )
    response = await env.client.post(
        f"/api/collections/{people['name']}/auth-with-password",
        json={"identity": f"u1-{env.suffix}@example.invalid", "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text
    return people["name"], {"Authorization": response.json()["token"]}


async def test_request_list_values_are_json_text_in_rules(env: _Env) -> None:
    """PocketBase binds a list ``@request.data`` value as its JSON text."""
    for rule, status in (
        ('@request.data.tags ?= "b"', 400),
        ('@request.data.tags = \'["a","b"]\'', 200),
    ):
        collection = await env.collection(
            {
                "name": env.name(f"rq_{status}"),
                "schema": [_text("title"), _select("tags", ["a", "b"], 2)],
                "createRule": rule,
            }
        )
        response = await env.client.post(
            f"/api/collections/{collection['name']}/records",
            json={"title": "x", "tags": ["a", "b"]},
        )
        assert response.status_code == status, (rule, response.text)


async def test_collection_rule_any_match_is_unchanged(env: _Env) -> None:
    people, user = await _auth_env(env)
    docs = await env.collection(
        {
            "name": env.name("rp_docs_any"),
            "schema": [_text("title")],
            "listRule": f"@collection.{people}.id ?= @request.auth.id",
        }
    )
    for title in ("d1", "d2"):
        await env.create(docs["name"], {"title": title}, headers=env.admin)
    assert await env.titles(docs["name"], "", headers=user) == ["d1", "d2"]


async def test_collection_rule_match_all_like_pocketbase(env: _Env) -> None:
    """``@collection.X.id = @request.auth.id`` needs every X row to be the
    caller: nothing is listed once X has two records (the BUMA users
    createRule/updateRule shape)."""
    people, user = await _auth_env(env)
    docs = await env.collection(
        {
            "name": env.name("rp_docs_eq"),
            "schema": [_text("title")],
            "listRule": f"@collection.{people}.id = @request.auth.id",
        }
    )
    for title in ("d1", "d2"):
        await env.create(docs["name"], {"title": title}, headers=env.admin)
    assert await env.titles(docs["name"], "", headers=user) == []


async def _delete_env(env: _Env) -> dict[str, Any]:
    targets = await env.collection(
        {"name": env.name("dl_targets"), "schema": [_text("name")]}
    )
    owners = await env.collection(
        {
            "name": env.name("dl_owners"),
            "schema": [
                _text("title"),
                _relation("rels", targets["id"], None),
                _relation("one", targets["id"], 1),
                _relation("casc_multi", targets["id"], None, cascade=True),
                _relation("casc_one", targets["id"], 1, cascade=True),
            ],
        }
    )
    required = await env.collection(
        {
            "name": env.name("dl_required"),
            "schema": [
                _text("title"),
                _relation("req_one", targets["id"], 1, required=True),
            ],
        }
    )
    d = {index: _rid(f"dl{env.suffix}", index) for index in range(1, 7)}
    for index, record_id in d.items():
        await env.create(targets["name"], {"id": record_id, "name": f"d{index}"})
    owner_ids = {}
    for title, values in (
        ("o1", {"rels": [d[1], d[2]], "one": d[1]}),
        ("o3", {"casc_multi": [d[3], d[4]]}),
        ("o4", {"casc_one": d[5]}),
        ("o5", {"rels": [d[1]]}),
    ):
        owner_ids[title] = (
            await env.create(owners["name"], {"title": title, **values})
        )["id"]
    required_id = (
        await env.create(required["name"], {"title": "r1", "req_one": d[6]})
    )["id"]
    return {
        "targets": targets["name"],
        "owners": owners["name"],
        "required": required["name"],
        "d": d,
        "owner_ids": owner_ids,
        "required_id": required_id,
    }


async def _get(env: _Env, collection: str, record_id: str) -> dict[str, Any] | int:
    response = await env.client.get(
        f"/api/collections/{collection}/records/{record_id}"
    )
    return response.json() if response.status_code == 200 else response.status_code


async def test_delete_unsets_references_like_pocketbase(env: _Env) -> None:
    """``DeleteRecord`` removes the id from every referencing relation."""
    p = await _delete_env(env)
    d, owners = p["d"], p["owners"]
    o1 = p["owner_ids"]["o1"]
    before = await _get(env, owners, o1)
    assert isinstance(before, dict)
    dsn = (
        get_engine()
        .url.render_as_string(hide_password=False)
        .replace("postgresql+asyncpg://", "postgresql://")
    )
    listener = await asyncpg.connect(dsn)
    notified: list[str] = []
    try:
        await listener.add_listener(
            "record_changes",
            lambda _conn, _pid, _channel, payload: notified.append(payload),
        )
        response = await env.client.delete(
            f"/api/collections/{p['targets']}/records/{d[1]}", headers=env.admin
        )
        assert response.status_code == 204, response.text
        for _ in range(50):
            if len(notified) >= 3:
                break
            await asyncio.sleep(0.05)
    finally:
        await listener.close()
    after = await _get(env, owners, o1)
    assert isinstance(after, dict)
    assert after["rels"] == [d[2]]
    assert after["one"] == ""
    assert after["updated"] != before["updated"]
    o5 = await _get(env, owners, p["owner_ids"]["o5"])
    assert isinstance(o5, dict) and o5["rels"] == []
    # The cleaned record no longer blocks an unrelated update.
    response = await env.client.patch(
        f"/api/collections/{owners}/records/{o1}", json={"title": "o1b"}
    )
    assert response.status_code == 200, response.text
    # Realtime: the delete and both cleaned references were broadcast.
    events = [json.loads(item) for item in notified]
    assert {(event.get("action"), event.get("record_id")) for event in events} >= {
        ("delete", d[1]),
        ("update", o1),
        ("update", p["owner_ids"]["o5"]),
    }


async def test_cascade_waits_for_the_last_reference_like_pocketbase(
    env: _Env,
) -> None:
    p = await _delete_env(env)
    d, owners = p["d"], p["owners"]
    await env.client.delete(
        f"/api/collections/{p['targets']}/records/{d[3]}", headers=env.admin
    )
    o3 = await _get(env, owners, p["owner_ids"]["o3"])
    assert isinstance(o3, dict) and o3["casc_multi"] == [d[4]]
    await env.client.delete(
        f"/api/collections/{p['targets']}/records/{d[4]}", headers=env.admin
    )
    assert await _get(env, owners, p["owner_ids"]["o3"]) == 404
    await env.client.delete(
        f"/api/collections/{p['targets']}/records/{d[5]}", headers=env.admin
    )
    assert await _get(env, owners, p["owner_ids"]["o4"]) == 404


async def test_required_reference_blocks_the_delete_and_rolls_back(
    env: _Env,
) -> None:
    p = await _delete_env(env)
    d = p["d"]
    # d6 is required by r1; o1 also references d1 only, so nothing else changes.
    response = await env.client.delete(
        f"/api/collections/{p['targets']}/records/{d[6]}", headers=env.admin
    )
    assert response.status_code == 400, response.text
    assert response.json()["message"] == (
        "Failed to delete record. Make sure that the record is not part of a "
        "required relation reference."
    )
    assert response.json()["data"] == {}
    assert isinstance(await _get(env, p["targets"], d[6]), dict)
    required = await _get(env, p["required"], p["required_id"])
    assert isinstance(required, dict) and required["req_one"] == d[6]

    # A batch delete is refused the same way and rolled back as a whole.
    response = await env.client.post(
        "/api/batch",
        headers=env.admin,
        json={
            "requests": [
                {
                    "method": "DELETE",
                    "url": f"/api/collections/{p['targets']}/records/{d[2]}",
                },
                {
                    "method": "DELETE",
                    "url": f"/api/collections/{p['targets']}/records/{d[6]}",
                },
            ]
        },
    )
    assert response.status_code == 400, response.text
    assert isinstance(await _get(env, p["targets"], d[2]), dict)
    o1 = await _get(env, p["owners"], p["owner_ids"]["o1"])
    assert isinstance(o1, dict) and o1["rels"] == [d[1], d[2]]


async def test_relation_errors_are_merged_with_the_other_field_errors(
    env: _Env,
) -> None:
    """PocketBase validates the whole form: data lists every failing field.

    ``data`` matches PocketBase 0.22.34 exactly; the top-level message keeps
    PPBase's format (PocketBase: "Failed to create record." for a non-admin
    create, "Something went wrong while processing your request." for an
    admin create and for updates) and ``maxSelect`` overflow keeps PPBase's
    ``validation_max_select_constraint`` code (PocketBase:
    ``validation_too_many_values``); both are pre-existing PPBase envelopes.
    """
    targets = await env.collection(
        {"name": env.name("vl_targets"), "schema": [_text("name")]}
    )
    valid = await env.collection(
        {
            "name": env.name("vl_records"),
            "schema": [
                _text("title", required=True),
                _relation("rels", targets["id"], None),
                _relation("lim", targets["id"], 2),
            ],
        }
    )
    t1 = (await env.create(targets["name"], {"name": "alpha"}))["id"]
    t2 = (await env.create(targets["name"], {"name": "beta"}))["id"]
    missing = _rid(f"vlmiss{env.suffix}", 0)
    path = f"/api/collections/{valid['name']}/records"

    for headers in ({}, env.admin):
        response = await env.client.post(
            path, json={"rels": [t1, missing]}, headers=headers
        )
        assert response.status_code == 400, response.text
        body = response.json()
        assert {key: value["code"] for key, value in body["data"].items()} == {
            "rels": "validation_missing_rel_records",
            "title": "validation_required",
        }
        assert body["message"] == (
            "Failed to create record. Validation failed for: rels, title."
        )

    # maxSelect is checked before the target lookup, like checkRelationValue.
    response = await env.client.post(
        path, json={"title": "x", "lim": [t1, t2, missing]}
    )
    assert response.status_code == 400, response.text
    assert list(response.json()["data"]) == ["lim"]
    assert response.json()["data"]["lim"]["code"] == "validation_max_select_constraint"

    # @request.data.rels:length counts list.ToUniqueStringSlice: the
    # partially decoded '["t1",1]' has 2 items, so the rule passes (no 404)
    # and the form reports the unknown second id, like PocketBase 0.22.34.
    counted = await env.collection(
        {
            "name": env.name("vl_length"),
            "schema": [_text("title"), _relation("rels", targets["id"], None)],
            "updateRule": "@request.data.rels:length = 2",
        }
    )
    record = await env.create(
        counted["name"], {"title": "len", "rels": [t1]}, headers=env.admin
    )
    response = await env.client.patch(
        f"/api/collections/{counted['name']}/records/{record['id']}",
        json={"rels": json.dumps([t1, 1])},
    )
    assert response.status_code == 400, response.text
    assert response.json()["data"]["rels"]["code"] == "validation_missing_rel_records"

    created = await env.create(valid["name"], {"title": "ok", "rels": [t1]})
    collection = await resolve_collection(get_engine(), valid["name"])
    async with get_engine().begin() as conn:
        await conn.execute(
            text(f'UPDATE "{collection.name}" SET "rels" = :value WHERE id = :id'),
            {"value": [missing, t1], "id": created["id"]},
        )
    response = await env.client.patch(f"{path}/{created['id']}", json={"title": "ok2"})
    assert response.status_code == 400, response.text
    assert {key: value["code"] for key, value in response.json()["data"].items()} == {
        "rels": "validation_missing_rel_records"
    }


# ---------------------------------------------------------------------------
# Databases created before the multiplicity rule
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated_database(pg_url: str) -> AsyncIterator[AsyncEngine]:
    """An empty temporary database with PPBase system tables."""
    name = f"ppbase_shape_{uuid.uuid4().hex[:12]}"
    admin_url = make_url(pg_url)
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    target: AsyncEngine | None = None
    created = False
    try:
        try:
            async with admin_engine.connect() as conn:
                await conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
            created = True
        except DBAPIError as exc:
            if "permission denied" in str(exc).lower():
                pytest.skip("PostgreSQL user cannot CREATE DATABASE")
            raise
        target = create_async_engine(admin_url.set(database=name), poolclass=NullPool)
        async with target.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield target
    finally:
        if target is not None:
            await target.dispose()
        if created:
            async with admin_engine.connect() as conn:
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": name},
                )
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin_engine.dispose()


async def _start_ppbase(database_url: str, data_dir: Path) -> tuple[int, str]:
    """Run the real lifespan (bootstrap, migrations, checks) in a subprocess."""
    script = textwrap.dedent(
        f"""
        import asyncio
        from ppbase.app import create_app
        from ppbase.config import Settings

        async def main():
            settings = Settings(
                database_url={database_url!r},
                migrations_dir={str(data_dir / "migrations")!r},
                data_dir={str(data_dir)!r},
                jwt_secret="shape-check-secret",
                apply_migrations_on_start=True,
                generate_migrations=False,
            )
            app = create_app(settings)
            async with app.router.lifespan_context(app):
                print("STARTED")

        asyncio.run(main())
        """
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode or 0, output.decode("utf-8", errors="replace")


async def test_unreconciled_scalar_column_fails_explicitly_until_reconciled(
    isolated_database: AsyncEngine,
    tmp_path: Path,
) -> None:
    """A maxSelect-null relation stored in a pre-rule VARCHAR(15) column.

    PPBase refuses to start on such a database and refuses to alter the
    collection, naming ``reconcile_multi_value_columns``; after the explicit
    repair everything works.  (Without the check, reads would return a string
    and filters, writes and collection updates would fail with SQL errors.)
    """
    database_url = isolated_database.url.render_as_string(hide_password=False)
    code, output = await _start_ppbase(database_url, tmp_path)
    assert code == 0, output  # a fresh database starts

    # Recreate the pre-rule shape: VARCHAR(15) + btree for a null relation.
    async with isolated_database.begin() as conn:
        target_id, owner_id = "shapetargets001", "shapeowners0001"
        target_schema = [
            {"id": "f_name", "name": "name", "type": "text", "options": {}}
        ]
        owner_schema = [
            {"id": "f_title", "name": "title", "type": "text", "options": {}},
            {**_relation("pdv", target_id, None), "id": "f_pdv"},
        ]
        for collection_id, name, schema in (
            (target_id, "shape_targets", target_schema),
            (owner_id, "shape_owners", owner_schema),
        ):
            await conn.execute(
                text(
                    'INSERT INTO "_collections" (id, name, type, system, schema, '
                    "indexes, options, created, updated) VALUES (:id, :name, 'base', "
                    "false, CAST(:schema AS jsonb), CAST('[]' AS jsonb), "
                    "CAST('{}' AS jsonb), now(), now())"
                ),
                {"id": collection_id, "name": name, "schema": json.dumps(schema)},
            )
        await conn.execute(
            text(
                'CREATE TABLE "shape_targets" (id VARCHAR(15) PRIMARY KEY, '
                "created TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "updated TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "name TEXT NOT NULL DEFAULT '')"
            )
        )
        await conn.execute(
            text(
                'CREATE TABLE "shape_owners" (id VARCHAR(15) PRIMARY KEY, '
                "created TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "updated TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "title TEXT NOT NULL DEFAULT '', "
                "pdv VARCHAR(15) NOT NULL DEFAULT '')"
            )
        )
        await conn.execute(
            text(
                'CREATE INDEX "idx_shape_owners_pdv" ON "shape_owners" ("pdv") '
                "WHERE \"pdv\" != ''"
            )
        )
        await conn.execute(
            text("INSERT INTO shape_targets (id, name) VALUES ('shapetarget0001', 'a')")
        )
        await conn.execute(
            text(
                "INSERT INTO shape_owners (id, title, pdv) VALUES "
                "('shapeowner00001', 'empty', ''), "
                "('shapeowner00002', 'one', 'shapetarget0001')"
            )
        )

    mismatches = await find_multi_value_shape_mismatches(isolated_database)
    assert [
        (m.collection, m.field, m.expected_array, m.column_type) for m in mismatches
    ] == [("shape_owners", "pdv", True, "character varying(15)")]

    code, output = await _start_ppbase(database_url, tmp_path)
    assert code != 0
    assert "MultiValueShapeMismatchError" in output
    assert (
        "shape_owners.pdv (needs an array column, found character varying(15))"
        in output
    )
    assert "reconcile_multi_value_columns" in output

    owners = SimpleCollection("shape_owners", owner_schema)
    from ppbase.db.schema_manager import update_collection_table

    with pytest.raises(
        MultiValueShapeMismatchError, match="reconcile_multi_value_columns"
    ):
        async with isolated_database.begin() as conn:
            await update_collection_table(conn, owners, owners)

    # The repair runs from an application migration, inside its transaction:
    # a failing migration rolls the conversion back and PPBase still refuses.
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    failing = migrations / "1700000001_reconcile_shape_owners.py"
    failing.write_text(
        textwrap.dedent(
            """
            async def up(app):
                converted = await app.reconcile_multi_value_columns(
                    "shape_owners", ["pdv"]
                )
                assert converted == ["pdv"], converted
                raise RuntimeError("abort after the conversion")


            async def down(app):
                pass
            """
        )
    )
    code, output = await _start_ppbase(database_url, tmp_path)
    assert code != 0 and "abort after the conversion" in output, output
    assert [
        m.field for m in await find_multi_value_shape_mismatches(isolated_database)
    ] == ["pdv"]

    failing.write_text(
        textwrap.dedent(
            """
            async def up(app):
                converted = await app.reconcile_multi_value_columns(
                    "shape_owners", ["pdv"]
                )
                assert converted in (["pdv"], []), converted


            async def down(app):
                pass
            """
        )
    )
    code, output = await _start_ppbase(database_url, tmp_path)
    assert code == 0, output
    assert "STARTED" in output
    assert await find_multi_value_shape_mismatches(isolated_database) == []
    async with isolated_database.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT title, pdv FROM shape_owners ORDER BY title")
            )
        ).all()
        index = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'idx_shape_owners_pdv'"
                )
            )
        ).scalar_one()
    assert [tuple(row) for row in rows] == [("empty", []), ("one", ["shapetarget0001"])]
    assert "USING gin" in index
    async with isolated_database.begin() as conn:
        await update_collection_table(conn, owners, owners)  # no error any more
        # A second run converts nothing.
        assert await reconcile_multi_value_columns(conn, owners) == []
    code, output = await _start_ppbase(database_url, tmp_path)  # already applied
    assert code == 0, output


class SimpleCollection:
    """Minimal collection object accepted by the schema manager."""

    def __init__(self, name: str, schema: list[dict[str, Any]]) -> None:
        self.id = f"{name}_id"
        self.name = name
        self.type = "base"
        self.schema = schema
        self.indexes: list[str] = []
        self.options: dict[str, Any] = {}
