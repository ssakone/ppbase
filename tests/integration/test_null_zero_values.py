"""PostgreSQL integration tests: NULL values are served as zero values.

PocketBase reads a SQL NULL as ``nil`` and serves each schema field through
its type's ``PrepareValue`` (v0.22.34 ``models/record.go`` l.48-91, l.328-359
and l.528-531, ``models/schema/schema_field.go`` l.304-390), so a view column
left NULL by a LEFT JOIN without match is served "" (text, email, url,
editor, date, single select/file/relation), 0 (number), false (bool), []
(multiple select/file/relation) and null (json).  The expected values are
the PocketBase 0.22.34 results of
``tests/pocketbase_parity/null_zero_values_probe.py`` on the same synthetic
data (rows ``a`` to ``c``, ``t_full`` and ``t_empty``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from ppbase.db.engine import get_engine
from ppbase.services.realtime_service import (
    SubscriptionManager,
    broadcast_record_change,
)
from ppbase.services.record_service import get_record, resolve_collection

pytestmark = pytest.mark.asyncio

_PUBLIC = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}
_FIELDS = (
    "txt",
    "mail",
    "link",
    "rich",
    "num",
    "flag",
    "meta",
    "sel",
    "sels",
    "doc",
    "docs",
    "rel",
    "rels",
    "relnull",
    "day",
)
# PocketBase 0.22.34 values of the unmatched LEFT JOIN row (probe row "c").
_ZERO = {
    "tid": "",
    "txt": "",
    "mail": "",
    "link": "",
    "rich": "",
    "num": 0,
    "flag": False,
    "meta": None,
    "sel": "",
    "sels": [],
    "doc": "",
    "docs": [],
    "rel": "",
    "rels": [],
    "relnull": [],
    "day": "",
}


def _f(name: str, field_type: str, **options: Any) -> dict[str, Any]:
    return {"name": name, "type": field_type, "required": False, "options": options}


def _target_schema(refs_id: str) -> list[dict[str, Any]]:
    return [
        _f("txt", "text"),
        _f("mail", "email"),
        _f("link", "url"),
        _f("rich", "editor"),
        _f("num", "number"),
        _f("flag", "bool"),
        _f("meta", "json"),
        _f("sel", "select", maxSelect=1, values=["x", "y"]),
        _f("sels", "select", maxSelect=2, values=["x", "y"]),
        _f("doc", "file", maxSelect=1),
        _f("docs", "file", maxSelect=3),
        _f("rel", "relation", collectionId=refs_id, maxSelect=1),
        _f("rels", "relation", collectionId=refs_id, maxSelect=5),
        # PocketBase 0.22 unlimited relation (D-0019): multiple.
        _f("relnull", "relation", collectionId=refs_id, maxSelect=None),
        _f("day", "date"),
    ]


async def _post_collection(client: AsyncClient, token: str, body: dict) -> dict:
    payload = {**_PUBLIC, "type": "base", **body}
    if payload["type"] == "view":
        payload.update(createRule=None, updateRule=None, deleteRule=None)
    response = await client.post(
        "/api/collections", headers={"Authorization": token}, json=payload
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create(client: AsyncClient, name: str, payload: dict) -> dict:
    response = await client.post(f"/api/collections/{name}/records", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def _items(client: AsyncClient, name: str, **params: Any) -> list[dict]:
    params.setdefault("perPage", 200)
    response = await client.get(f"/api/collections/{name}/records", params=params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _values(item: dict, keys: Any = _ZERO) -> dict:
    return {key: item.get(key, "<absent>") for key in keys}


@pytest.fixture(scope="module")
async def zv_env(app_client: AsyncClient, admin_token: str) -> Any:
    suffix = uuid.uuid4().hex[:8]
    refs = await _post_collection(
        app_client,
        admin_token,
        {"name": f"zv_refs_{suffix}", "schema": [_f("name", "text")]},
    )
    targets = await _post_collection(
        app_client,
        admin_token,
        {"name": f"zv_targets_{suffix}", "schema": _target_schema(refs["id"])},
    )
    src = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zv_src_{suffix}",
            "schema": [
                _f("title", "text"),
                _f("target", "relation", collectionId=targets["id"], maxSelect=1),
            ],
        },
    )
    rn, tn, sn = refs["name"], targets["name"], src["name"]
    ra = (await _create(app_client, rn, {"name": "ra"}))["id"]
    rb = (await _create(app_client, rn, {"name": "rb"}))["id"]
    full = await _create(
        app_client,
        tn,
        {
            "txt": "tx",
            "mail": "full@example.invalid",
            "link": "https://example.invalid/x",
            "rich": "<p>rich</p>",
            "num": 4.5,
            "flag": True,
            "meta": {"k": 1},
            "sel": "x",
            "sels": ["x", "y"],
            "rel": ra,
            "rels": [ra, rb],
            "relnull": [rb],
            "day": "2024-01-02 03:04:05.000Z",
        },
    )
    # File names only (no bytes are needed to serve the names).
    async with get_engine().begin() as conn:
        await conn.execute(
            text(f'UPDATE "{tn}" SET "doc" = :doc, "docs" = :docs WHERE "id" = :id'),
            {"doc": "one.txt", "docs": ["two.txt", "three.txt"], "id": full["id"]},
        )
    empty = await _create(app_client, tn, {})
    sources = {}
    for title, target in (("a", full["id"]), ("b", empty["id"]), ("c", "")):
        sources[title] = (
            await _create(app_client, sn, {"title": title, "target": target})
        )["id"]
    columns = ", ".join(f"t.{name}" for name in _FIELDS)
    view = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zv_view_{suffix}",
            "type": "view",
            # The schema PocketBase 0.22.34 infers for this query.
            "schema": [
                _f("title", "text"),
                _f("tid", "relation", collectionId=targets["id"], maxSelect=1),
                *_target_schema(refs["id"]),
            ],
            "options": {
                "query": (
                    f"SELECT s.id, s.title, t.id AS tid, {columns} FROM {sn} s "
                    f"LEFT JOIN {tn} t ON t.id = s.target"
                )
            },
        },
    )
    view_ref = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zv_view_ref_{suffix}",
            "type": "view",
            "schema": [
                _f("title", "text"),
                _f("vref", "relation", collectionId=view["id"], maxSelect=1),
            ],
            "options": {
                "query": (
                    f"SELECT s.id, s.title, v.id AS vref FROM {sn} s "
                    f"LEFT JOIN {view['name']} v ON v.id = s.id"
                )
            },
        },
    )
    yield {
        "refs": rn,
        "targets": tn,
        "view": view["name"],
        "view_ref": view_ref["name"],
        "ra": ra,
        "rb": rb,
        "full": full["id"],
        "empty": empty["id"],
        "sources": sources,
    }
    headers = {"Authorization": admin_token}
    for collection in (view_ref, view, src, targets, refs):
        await app_client.delete(f"/api/collections/{collection['id']}", headers=headers)


def _full_values(env: dict) -> dict:
    return {
        "tid": env["full"],
        "txt": "tx",
        "mail": "full@example.invalid",
        "link": "https://example.invalid/x",
        "rich": "<p>rich</p>",
        "num": 4.5,
        "flag": True,
        "meta": {"k": 1},
        "sel": "x",
        "sels": ["x", "y"],
        "doc": "one.txt",
        "docs": ["two.txt", "three.txt"],
        "rel": env["ra"],
        "rels": [env["ra"], env["rb"]],
        "relnull": [env["rb"]],
        "day": "2024-01-02 03:04:05.000Z",
    }


async def test_view_left_join_nulls_are_served_as_zero_values(
    app_client: AsyncClient, zv_env: dict
) -> None:
    items = {item["title"]: item for item in await _items(app_client, zv_env["view"])}
    assert _values(items["c"]) == _ZERO
    for key in ("num", "flag"):
        assert type(items["c"][key]) is type(_ZERO[key])
    # Non-NULL values are unchanged: the stored defaults of an empty target
    # and the values of a full one.
    assert _values(items["b"]) == {**_ZERO, "tid": zv_env["empty"]}
    assert _values(items["a"]) == _full_values(zv_env)

    record_id = zv_env["sources"]["c"]
    response = await app_client.get(
        f"/api/collections/{zv_env['view']}/records/{record_id}"
    )
    assert response.status_code == 200, response.text
    assert _values(response.json()) == _ZERO
    assert "created" not in response.json() and "updated" not in response.json()

    items = await _items(
        app_client, zv_env["view"], sort="title", fields="title,num,rels,meta"
    )
    assert items[2] == {"title": "c", "num": 0, "rels": [], "meta": None}


async def test_expand_from_and_into_view_records(
    app_client: AsyncClient, zv_env: dict
) -> None:
    items = {
        item["title"]: item
        for item in await _items(
            app_client, zv_env["view"], expand="tid,rel,rels,relnull"
        )
    }
    assert sorted(items["a"]["expand"]) == ["rel", "relnull", "rels", "tid"]
    assert sorted(items["b"]["expand"]) == ["tid"]
    assert "expand" not in items["c"]

    items = {
        item["title"]: item
        for item in await _items(app_client, zv_env["view_ref"], expand="vref")
    }
    assert _values(items["c"]["expand"]["vref"]) == _ZERO
    assert _values(items["a"]["expand"]["vref"]) == _full_values(zv_env)


async def test_filters_sorts_and_totals_are_unchanged(
    app_client: AsyncClient, zv_env: dict
) -> None:
    async def titles(**params: Any) -> list[str]:
        params.setdefault("sort", "title")
        return [
            item["title"] for item in await _items(app_client, zv_env["view"], **params)
        ]

    response = await app_client.get(
        f"/api/collections/{zv_env['view']}/records",
        params={"perPage": 2, "sort": "title"},
    )
    body = response.json()
    assert [body["totalItems"], body["totalPages"], len(body["items"])] == [3, 2, 2]
    # Same results as PocketBase 0.22.34 (probe).
    assert await titles(filter='txt != ""') == ["a"]
    assert await titles(filter="num = 0") == ["b"]
    assert await titles(filter="flag = false") == ["b"]
    # Open divergences, unchanged by the NULL serialization (probe
    # KNOWN_DIVERGENCES): PocketBase 0.22.34 gives ["b", "c"] and ["c", "b",
    # "a"]; PostgreSQL keeps the SQL NULL semantics.
    assert await titles(filter='txt = ""') == ["b"]
    assert await titles(sort="txt,title") == ["b", "a", "c"]


async def test_base_columns_are_not_null_and_serve_their_defaults(
    app_client: AsyncClient, admin_token: str, zv_env: dict
) -> None:
    tn = zv_env["targets"]
    async with get_engine().connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :table"
                ),
                {"table": tn},
            )
        ).all()
    nullable = {name for name, is_nullable in rows if is_nullable == "YES"}
    # Only the date column can hold NULL (served "", D-0032).
    assert nullable == {"day"}

    items = {item["id"]: item for item in await _items(app_client, tn)}
    empty = items[zv_env["empty"]]
    assert _values(empty, _FIELDS) == {key: _ZERO[key] for key in _FIELDS}

    # A field added to a collection that already has records: PostgreSQL
    # fills the existing rows with the column default, never NULL.
    headers = {"Authorization": admin_token}
    response = await app_client.get(f"/api/collections/{tn}", headers=headers)
    collection = response.json()
    schema = [
        *collection["schema"],
        _f("late_txt", "text"),
        _f("late_num", "number"),
        _f("late_sels", "select", maxSelect=3, values=["x"]),
    ]
    response = await app_client.patch(
        f"/api/collections/{tn}", headers=headers, json={"schema": schema}
    )
    assert response.status_code == 200, response.text
    async with get_engine().connect() as conn:
        late_nulls = (
            await conn.execute(
                text(
                    f'SELECT count(*) FROM "{tn}" WHERE "late_txt" IS NULL '
                    'OR "late_num" IS NULL OR "late_sels" IS NULL'
                )
            )
        ).scalar_one()
    assert late_nulls == 0
    items = {item["id"]: item for item in await _items(app_client, tn)}
    late = items[zv_env["full"]]
    assert [late["late_txt"], late["late_num"], late["late_sels"]] == ["", 0, []]


async def test_a_forced_null_is_served_as_zero_value_on_every_path(
    app_client: AsyncClient, admin_token: str
) -> None:
    """A NULL in a base/auth table (only possible outside PPBase's DDL, e.g.

    a column made nullable by hand) is served like PocketBase on every
    record path: write responses, list, view, auth and realtime.
    """
    suffix = uuid.uuid4().hex[:8]
    people = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zv_people_{suffix}",
            "type": "auth",
            "schema": [
                _f("nick", "text"),
                _f("score", "number"),
                _f("tags", "select", maxSelect=2, values=["x", "y"]),
                _f("meta", "json"),
            ],
            "options": {
                "allowEmailAuth": True,
                "allowUsernameAuth": True,
                "allowOAuth2Auth": False,
                "minPasswordLength": 8,
                "requireEmail": False,
            },
        },
    )
    name = people["name"]
    password = "probe-User-Password-1"
    email = f"zv{suffix}@example.invalid"
    zero = {"nick": "", "score": 0, "tags": [], "meta": None}
    try:
        created = await _create(
            app_client,
            name,
            {
                "username": f"zv{suffix}",
                "email": email,
                "password": password,
                "passwordConfirm": password,
                "nick": "n",
                "score": 3,
                "tags": ["x"],
            },
        )
        async with get_engine().begin() as conn:
            for column in ("nick", "score", "tags", "meta"):
                await conn.execute(
                    text(f'ALTER TABLE "{name}" ALTER COLUMN "{column}" DROP NOT NULL')
                )
            await conn.execute(
                text(
                    f'UPDATE "{name}" SET "nick" = NULL, "score" = NULL, '
                    '"tags" = NULL, "meta" = NULL WHERE "id" = :id'
                ),
                {"id": created["id"]},
            )
        response = await app_client.post(
            f"/api/collections/{name}/auth-with-password",
            json={"identity": email, "password": password},
        )
        assert response.status_code == 200, response.text
        assert _values(response.json()["record"], zero) == zero
        token = {"Authorization": response.json()["token"]}
        response = await app_client.post(
            f"/api/collections/{name}/auth-refresh", headers=token
        )
        assert response.status_code == 200, response.text
        assert _values(response.json()["record"], zero) == zero
        response = await app_client.get(
            f"/api/collections/{name}/records/{created['id']}", headers=token
        )
        assert _values(response.json(), zero) == zero
        response = await app_client.get(
            f"/api/collections/{name}/records",
            headers={"Authorization": admin_token},
        )
        assert response.status_code == 200, response.text
        assert _values(response.json()["items"][0], zero) == zero
        # A write response re-reads the row: an unrelated update still
        # serves the other NULL columns as zero values.
        response = await app_client.patch(
            f"/api/collections/{name}/records/{created['id']}",
            headers={"Authorization": admin_token},
            json={"nick": "m"},
        )
        assert response.status_code == 200, response.text
        assert _values(response.json(), zero) == {**zero, "nick": "m"}

        engine = get_engine()
        collection = await resolve_collection(engine, name)
        manager = SubscriptionManager()
        client_id = manager.register_client()
        await manager.add_subscription(
            client_id, f"{name}/*", auth_payload={"id": "admin", "type": "admin"}
        )
        await broadcast_record_change(
            manager,
            name,
            created["id"],
            "update",
            await get_record(engine, collection, created["id"]),
            engine=engine,
            collection=collection,
        )
        session = manager.get_session(client_id)
        assert session is not None and not session.response_queue.empty()
        record = session.response_queue.get_nowait()["data"]["record"]
        assert _values(record, zero) == {**zero, "nick": "m"}
    finally:
        await app_client.delete(
            f"/api/collections/{people['id']}", headers={"Authorization": admin_token}
        )
