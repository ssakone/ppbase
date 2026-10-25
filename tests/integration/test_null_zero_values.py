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
        "src": sn,
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


async def test_filters_sorts_and_totals_follow_pocketbase(
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
    # The NULL of row c is "" for ``= ""`` and sorts first (WS-A11; before,
    # PostgreSQL's NULL semantics gave ["b"] and ["b", "a", "c"]).
    assert await titles(filter='txt = ""') == ["b", "c"]
    assert await titles(sort="txt,title") == ["c", "b", "a"]


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


# ---------------------------------------------------------------------------
# Filters and sorts on NULL columns (WS-A11)
# ---------------------------------------------------------------------------
#
# PocketBase 0.22.34 results measured by
# ``tests/pocketbase_parity/null_zero_values_probe.py`` (``null`` section) on
# the same rows: the titles of the matching view rows, sorted by title.  Row
# c is the unmatched LEFT JOIN (every joined column NULL), row b the empty
# target.  PocketBase builds ``=``/``!=`` with ``resolveEqualExpr``
# (v0.22.34 tools/search/filter.go l.284-368) and sorts on SQLite, where
# NULL comes first in ascending order.
_PB_VIEW_FILTERS: dict[str, str] = {
    '"" = day': "bc",
    '"" = doc': "bc",
    '"" = docs': "c",
    '"" = flag': "c",
    '"" = link': "bc",
    '"" = mail': "bc",
    '"" = meta': "",
    '"" = num': "c",
    '"" = rel': "bc",
    '"" = relnull': "c",
    '"" = rels': "c",
    '"" = rich': "bc",
    '"" = sel': "bc",
    '"" = sels': "c",
    '"" = tid': "c",
    '"" = txt': "bc",
    '"2030-01-01 00:00:00.000Z" != day': "abc",
    '"https://zz.invalid" != link': "abc",
    '"y" != sel': "abc",
    '"y" != sels': "abc",
    '"zz" != rich': "abc",
    '"zz" != txt': "abc",
    '"zz.txt" != doc': "abc",
    '"zz.txt" != docs': "abc",
    '"zz@example.invalid" != mail': "abc",
    '"zzzzzzzzzzzzzzz" != rel': "abc",
    '"zzzzzzzzzzzzzzz" != relnull': "abc",
    '"zzzzzzzzzzzzzzz" != rels': "abc",
    '"zzzzzzzzzzzzzzz" != tid': "abc",
    "7 != num": "abc",
    'day != ""': "a",
    'day != "2030-01-01 00:00:00.000Z"': "abc",
    "day != null": "a",
    'day = ""': "bc",
    'day = "2030-01-01 00:00:00.000Z"': "",
    "day = null": "bc",
    'day ?!= ""': "a",
    'day ?!= "2030-01-01 00:00:00.000Z"': "abc",
    "day ?!= null": "a",
    'day ?= ""': "bc",
    "day ?= null": "bc",
    'doc != ""': "a",
    'doc != "zz.txt"': "abc",
    "doc != null": "a",
    'doc = ""': "bc",
    'doc = "zz.txt"': "",
    "doc = null": "bc",
    'doc ?!= ""': "a",
    'doc ?!= "zz.txt"': "abc",
    "doc ?!= null": "a",
    'doc ?= ""': "bc",
    "doc ?= null": "bc",
    'docs != ""': "ab",
    'docs != "zz.txt"': "abc",
    "docs != null": "ab",
    'docs = ""': "c",
    'docs = "zz.txt"': "",
    "docs = null": "c",
    'docs ?!= ""': "ab",
    'docs ?!= "zz.txt"': "abc",
    "docs ?!= null": "ab",
    'docs ?= ""': "c",
    "docs ?= null": "c",
    'flag != ""': "ab",
    "flag != null": "ab",
    "flag != true": "bc",
    'flag = ""': "c",
    "flag = null": "c",
    "flag = true": "a",
    'flag ?!= ""': "ab",
    "flag ?!= null": "ab",
    "flag ?!= true": "bc",
    'flag ?= ""': "c",
    "flag ?= null": "c",
    'link != ""': "a",
    'link != "https://zz.invalid"': "abc",
    "link != null": "a",
    'link = ""': "bc",
    'link = "https://zz.invalid"': "",
    "link = null": "bc",
    'link ?!= ""': "a",
    'link ?!= "https://zz.invalid"': "abc",
    "link ?!= null": "a",
    'link ?= ""': "bc",
    "link ?= null": "bc",
    'mail != ""': "a",
    'mail != "zz@example.invalid"': "abc",
    "mail != null": "a",
    'mail = ""': "bc",
    'mail = "zz@example.invalid"': "",
    "mail = null": "bc",
    'mail ?!= ""': "a",
    'mail ?!= "zz@example.invalid"': "abc",
    "mail ?!= null": "a",
    'mail ?= ""': "bc",
    "mail ?= null": "bc",
    'meta != ""': "abc",
    "meta != null": "a",
    'meta = ""': "",
    "meta = null": "bc",
    'meta ?!= ""': "abc",
    "meta ?!= null": "a",
    'meta ?= ""': "",
    "meta ?= null": "bc",
    "meta.k != null": "a",
    "meta.k = null": "bc",
    "null != day": "a",
    "null != doc": "a",
    "null != docs": "ab",
    "null != flag": "ab",
    "null != link": "a",
    "null != mail": "a",
    "null != meta": "a",
    "null != num": "ab",
    "null != rel": "a",
    "null != relnull": "ab",
    "null != rels": "ab",
    "null != rich": "a",
    "null != sel": "a",
    "null != sels": "ab",
    "null != tid": "ab",
    "null != txt": "a",
    'num != ""': "ab",
    "num != 7": "abc",
    "num != null": "ab",
    "num < 9": "ab",
    "num <= 0": "b",
    'num = ""': "c",
    "num = 7": "",
    "num = null": "c",
    "num > 1": "a",
    'num ?!= ""': "ab",
    "num ?!= 7": "abc",
    "num ?!= null": "ab",
    'num ?= ""': "c",
    "num ?= null": "c",
    'rel != ""': "a",
    'rel != "zzzzzzzzzzzzzzz"': "abc",
    "rel != null": "a",
    'rel !~ "zz"': "ab",
    'rel = ""': "bc",
    'rel = "zzzzzzzzzzzzzzz"': "",
    "rel = null": "bc",
    "rel = tid": "c",
    'rel ?!= ""': "a",
    'rel ?!= "zzzzzzzzzzzzzzz"': "abc",
    "rel ?!= null": "a",
    'rel ?= ""': "bc",
    "rel ?= null": "bc",
    'relnull != ""': "ab",
    'relnull != "zzzzzzzzzzzzzzz"': "abc",
    "relnull != null": "ab",
    'relnull = ""': "c",
    'relnull = "zzzzzzzzzzzzzzz"': "",
    "relnull = null": "c",
    'relnull ?!= ""': "ab",
    'relnull ?!= "zzzzzzzzzzzzzzz"': "abc",
    "relnull ?!= null": "ab",
    'relnull ?= ""': "c",
    "relnull ?= null": "c",
    'rels != ""': "ab",
    'rels != "zzzzzzzzzzzzzzz"': "abc",
    "rels != null": "ab",
    'rels = ""': "c",
    'rels = "zzzzzzzzzzzzzzz"': "",
    "rels = null": "c",
    'rels ?!= ""': "ab",
    'rels ?!= "zzzzzzzzzzzzzzz"': "abc",
    "rels ?!= null": "ab",
    'rels ?= ""': "c",
    "rels ?= null": "c",
    'rich != ""': "a",
    'rich != "zz"': "abc",
    "rich != null": "a",
    'rich = ""': "bc",
    'rich = "zz"': "",
    "rich = null": "bc",
    'rich ?!= ""': "a",
    'rich ?!= "zz"': "abc",
    "rich ?!= null": "a",
    'rich ?= ""': "bc",
    "rich ?= null": "bc",
    'sel != ""': "a",
    'sel != "y"': "abc",
    "sel != null": "a",
    "sel != txt": "a",
    'sel = ""': "bc",
    'sel = "y"': "",
    "sel = null": "bc",
    'sel ?!= ""': "a",
    'sel ?!= "y"': "abc",
    "sel ?!= null": "a",
    'sel ?= ""': "bc",
    "sel ?= null": "bc",
    'sels != ""': "ab",
    'sels != "y"': "abc",
    "sels != null": "ab",
    'sels !~ "zz"': "ab",
    'sels = ""': "c",
    'sels = "y"': "",
    "sels = null": "c",
    'sels ?!= ""': "ab",
    'sels ?!= "y"': "abc",
    "sels ?!= null": "ab",
    'sels ?= ""': "c",
    "sels ?= null": "c",
    'tid != ""': "ab",
    'tid != "zzzzzzzzzzzzzzz"': "abc",
    "tid != null": "ab",
    'tid = ""': "c",
    'tid = "zzzzzzzzzzzzzzz"': "",
    "tid = null": "c",
    'tid ?!= ""': "ab",
    'tid ?!= "zzzzzzzzzzzzzzz"': "abc",
    "tid ?!= null": "ab",
    'tid ?= ""': "c",
    "tid ?= null": "c",
    "true != flag": "bc",
    'txt != ""': "a",
    'txt != "zz"': "abc",
    "txt != null": "a",
    "txt != title": "abc",
    'txt !~ "zz"': "ab",
    'txt < "zz"': "ab",
    'txt = ""': "bc",
    'txt = "zz"': "",
    "txt = null": "bc",
    "txt = sel": "bc",
    'txt > ""': "a",
    'txt ?!= ""': "a",
    'txt ?!= "zz"': "abc",
    "txt ?!= null": "a",
    'txt ?= ""': "bc",
    "txt ?= null": "bc",
    "txt ?= sel": "bc",
    'txt ~ ""': "ab",
}
_PB_VIEW_SORTS: dict[str, str] = {
    "-day,title": "abc",
    "-doc,title": "abc",
    "-docs,title": "bac",
    "-flag,title": "abc",
    "-link,title": "abc",
    "-mail,title": "abc",
    "-meta,title": "abc",
    "-num,title": "abc",
    "-rel,title": "abc",
    "-relnull,title": "bac",
    "-rels,title": "bac",
    "-rich,title": "abc",
    "-sel,title": "abc",
    "-sels,title": "bac",
    "-txt,title": "abc",
    "day,title": "cba",
    "doc,title": "cba",
    "docs,title": "cab",
    "flag,title": "cba",
    "link,title": "cba",
    "mail,title": "cba",
    "meta,title": "bca",
    "num,title": "cba",
    "rel,title": "cba",
    "relnull,title": "cab",
    "rels,title": "cab",
    "rich,title": "cba",
    "sel,title": "cba",
    "sels,title": "cab",
    "txt,title": "cba",
}
# Documented differences of non-NULL values (probe KNOWN_DIVERGENCES): the
# NULL row is placed like PocketBase, the rest keeps PPBase's order.
_PPBASE_VIEW_SORTS = {
    # PocketBase sorts a multi-value field by its JSON text ('[]' after
    # '["x"]'), PPBase by its array ('{}' first).
    **{
        f"{sign}{field},title": order
        for field in ("sels", "docs", "rels", "relnull")
        for sign, order in (("", "cba"), ("-", "abc"))
    },
    # The empty date of row b is NULL in PPBase (D-0032) and ties with the
    # NULL of row c; SQLite sorts NULL before ''.
    "day,title": "bca",
}


async def _titles(
    client: AsyncClient, name: str, headers: dict | None = None, **params: Any
) -> str:
    params.setdefault("perPage", 200)
    response = await client.get(
        f"/api/collections/{name}/records", params=params, headers=headers
    )
    assert response.status_code == 200, (params, response.text)
    body = response.json()
    titles = "".join(item["title"] for item in body["items"])
    # totalItems counts with the same WHERE clause.
    assert body["totalItems"] == len(titles), params
    return titles


async def test_view_null_filters_match_pocketbase(
    app_client: AsyncClient, zv_env: dict
) -> None:
    mismatches = {}
    for expression, expected in _PB_VIEW_FILTERS.items():
        got = await _titles(app_client, zv_env["view"], sort="title", filter=expression)
        if got != expected:
            mismatches[expression] = {"pocketbase": expected, "ppbase": got}
    assert mismatches == {}


async def test_view_null_sorts_match_pocketbase(
    app_client: AsyncClient, zv_env: dict
) -> None:
    mismatches = {}
    for sort, expected in {**_PB_VIEW_SORTS, **_PPBASE_VIEW_SORTS}.items():
        got = await _titles(app_client, zv_env["view"], sort=sort)
        if got != expected:
            mismatches[sort] = {"expected": expected, "ppbase": got}
    assert mismatches == {}


async def test_relation_and_collection_paths_into_a_view_compare_null(
    app_client: AsyncClient, admin_token: str, zv_env: dict
) -> None:
    # PocketBase 0.22.34 (probe ``null.path_filters``).
    expected_vref = {
        'vref.txt = ""': "bc",
        'vref.txt != "zz"': "abc",
        "vref.num != 7": "abc",
        'vref.rel = ""': "bc",
        'vref.sel ?= ""': "bc",
        'vref.sels ?= ""': "c",
        "vref.flag != true": "bc",
        "vref.meta = null": "bc",
        "vref.meta != null": "a",
        "vref.meta.k = null": "bc",
        'vref.tid = ""': "c",
    }
    for expression, expected in expected_vref.items():
        got = await _titles(
            app_client, zv_env["view_ref"], sort="title", filter=expression
        )
        assert got == expected, expression
    view = zv_env["view"]
    expected_collection = {
        f'@collection.{view}.txt ?= ""': "bc",
        f'@collection.{view}.txt ?!= "zz"': "abc",
        f"@collection.{view}.num ?!= 7": "abc",
        f"@collection.{view}.meta ?= null": "bc",
        f"@collection.{view}.meta ?!= null": "a",
        f'@collection.{view}.rels ?= ""': "c",
        # Without ``?`` every row of the view must match: none.
        f'@collection.{view}.sel = ""': "",
    }
    headers = {"Authorization": admin_token}
    for condition, expected in expected_collection.items():
        expression = f"@collection.{view}.id ?= id && {condition}"
        got = await _titles(
            app_client, zv_env["src"], headers, sort="title", filter=expression
        )
        assert got == expected, expression


async def test_auth_records_through_a_left_join_compare_null(
    app_client: AsyncClient, admin_token: str, zv_env: dict
) -> None:
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
    headers = {"Authorization": admin_token}
    uview = None
    try:
        password = "probe-User-Password-1"
        for nick in ("a", "b"):
            await _create(
                app_client,
                people["name"],
                {
                    "username": f"zvnull{nick}{suffix}",
                    "email": f"zvnull{nick}{suffix}@example.invalid",
                    "nick": nick,
                    "score": 3,
                    "password": password,
                    "passwordConfirm": password,
                },
            )
        uview = await _post_collection(
            app_client,
            admin_token,
            {
                "name": f"zv_uview_{suffix}",
                "type": "view",
                "schema": [
                    _f("title", "text"),
                    _f("uid", "relation", collectionId=people["id"], maxSelect=1),
                    _f("uemail", "email"),
                    _f("uver", "bool"),
                    _f("uscore", "number"),
                    _f("utags", "select", maxSelect=2, values=["x", "y"]),
                ],
                "options": {
                    "query": (
                        "SELECT s.id, s.title, u.id AS uid, u.email AS uemail, "
                        "u.verified AS uver, u.score AS uscore, u.tags AS utags "
                        f"FROM {zv_env['src']} s LEFT JOIN {people['name']} u "
                        "ON u.nick = s.title"
                    )
                },
            },
        )
        # PocketBase 0.22.34 (probe ``null.auth_view_*``).
        expected = {
            'uemail = ""': "c",
            'uemail != ""': "ab",
            'uemail != "zz@example.invalid"': "abc",
            "uver = false": "ab",
            "uver != true": "abc",
            'uid = ""': "c",
            "uscore != 1": "abc",
            'utags = ""': "c",
            'utags ?= ""': "c",
        }
        for expression, titles in expected.items():
            got = await _titles(
                app_client, uview["name"], sort="title", filter=expression
            )
            assert got == titles, expression
        sorts = {
            "uemail,title": "cab",
            "-uemail,title": "bac",
            "uver,title": "cab",
            "-uscore,title": "abc",
        }
        for sort, titles in sorts.items():
            assert await _titles(app_client, uview["name"], sort=sort) == titles, sort
    finally:
        if uview is not None:
            await app_client.delete(f"/api/collections/{uview['id']}", headers=headers)
        await app_client.delete(f"/api/collections/{people['id']}", headers=headers)


async def test_json_null_filters_of_a_base_collection(
    app_client: AsyncClient, admin_token: str, zv_env: dict
) -> None:
    """A stored JSON null is null for ``= null`` (BUMA D-0029 filter part)."""
    tn = zv_env["targets"]
    jnull = await _create(app_client, tn, {"txt": "jn", "meta": {"k": None}})
    labels = {
        zv_env["full"]: "t_full",
        zv_env["empty"]: "t_empty",
        jnull["id"]: "t_jnull",
    }
    try:

        async def matching(expression: str) -> list[str]:
            items = await _items(app_client, tn, sort="txt", filter=expression)
            return [labels[item["id"]] for item in items]

        # PocketBase 0.22.34 (probe ``read.base_filters``, ``null.base_filters``).
        assert await matching("meta = null") == ["t_empty"]
        assert await matching("meta != null") == ["t_jnull", "t_full"]
        assert await matching("meta.k = null") == ["t_empty", "t_jnull"]
        assert await matching("meta.k != null") == ["t_full"]
        assert await matching('meta = ""') == []
        assert await matching('meta != ""') == ["t_empty", "t_jnull", "t_full"]
        # A date compared with json is its text: the empty date of t_empty is
        # '', not NULL (probe ``types.json_base``).
        everyone = ["t_empty", "t_jnull", "t_full"]
        for expression in ("meta = day", "meta ?= day", "day ?= meta"):
            assert await matching(expression) == [], expression
        for expression in ("meta != day", "meta ?!= day", "day ?!= meta"):
            assert await matching(expression) == everyone, expression
    finally:
        await app_client.delete(
            f"/api/collections/{tn}/records/{jnull['id']}",
            headers={"Authorization": admin_token},
        )


async def _title_list(
    client: AsyncClient, name: str, headers: dict | None = None, **params: Any
) -> list[str]:
    params.setdefault("perPage", 200)
    response = await client.get(
        f"/api/collections/{name}/records", params=params, headers=headers
    )
    assert response.status_code == 200, (params, response.text)
    body = response.json()
    titles = [item["title"] for item in body["items"]]
    assert body["totalItems"] == len(titles), params
    return titles


# PocketBase 0.22.34 (probe ``null.constants``, rows a to c): a comparison
# without any column is '' = v or v = w compared by SQLite without affinity.
_PB_CONSTANTS = {
    "1 = null": "",
    "1 != null": "abc",
    "0 != null": "abc",
    "null = 0": "",
    "true = null": "",
    "true != null": "abc",
    "false != null": "abc",
    '1 = ""': "",
    '1 != ""': "abc",
    '"" = 0': "",
    'true = ""': "",
    'true != ""': "abc",
    "null = null": "abc",
    '"" != null': "",
    '"x" = null': "",
    '"x" != ""': "abc",
    "1 = 1": "abc",
    '1 = "1"': "",
    "true = 1": "abc",
    '"a" = "b"': "",
    "1 ?= null": "",
    "1 ?!= null": "abc",
    "null ?= null": "abc",
    '"a" ?= "a"': "abc",
    'title = "a" || 1 = null': "a",
    'title = "a" && 1 != null': "a",
}


async def test_comparisons_without_a_column_follow_pocketbase(
    app_client: AsyncClient, zv_env: dict
) -> None:
    """``1 = null`` and the like are folded (they were HTTP 500 in 8fc3e9c)."""
    mismatches = {}
    for expression, expected in _PB_CONSTANTS.items():
        got = await _titles(app_client, zv_env["src"], sort="title", filter=expression)
        if got != expected:
            mismatches[expression] = {"pocketbase": expected, "ppbase": got}
    assert mismatches == {}


async def test_a_null_operand_matches_a_stored_empty_value(
    app_client: AsyncClient, zv_env: dict
) -> None:
    """``x = null`` is ``(x = '' OR x IS NULL)``: a stored '' matches.

    Rule-visible since WS-A11 (before: ``x IS NULL``, never true on a NOT NULL
    base column); PocketBase 0.22.34 probe ``null.base_filters``.
    """
    labels = {zv_env["full"]: "t_full", zv_env["empty"]: "t_empty"}
    for expression, expected in (
        ("txt = null", ["t_empty"]),
        ("null = txt", ["t_empty"]),
        ("txt != null", ["t_full"]),
    ):
        items = await _items(
            app_client, zv_env["targets"], sort="txt", filter=expression
        )
        assert [labels[item["id"]] for item in items] == expected, expression


_JSON_ROWS: tuple[tuple[str, Any], ...] = (
    ("n", None),
    ("kn", {"k": None}),
    ("knu", {"k": "nu"}),
    ("k1", {"k": 1}),
    ("sx", "x"),
    ("es", ""),
    ("num5", 5),
    ("tr", True),
    ("arr", ["nu", "x"]),
    ("o", {"a": 1}),
)
# PocketBase 0.22.34 (probe ``null.json_like``): JSON_EXTRACT reads a JSON
# null as NULL, which matches neither ~ nor !~.
_PB_JSON_LIKE = {
    'meta ~ "nu"': ["arr", "kn", "knu"],
    'meta ~ "x"': ["arr", "sx"],
    'meta !~ "x"': ["es", "k1", "kn", "knu", "num5", "o", "tr"],
    'meta !~ "zz"': ["arr", "es", "k1", "kn", "knu", "num5", "o", "sx", "tr"],
    'meta.k ~ "nu"': ["knu"],
    'meta.k !~ "x"': ["k1", "knu"],
    'meta.k !~ "zz"': ["k1", "knu"],
    'meta ?~ "nu"': ["arr", "kn", "knu"],
    'meta ?!~ "zz"': ["arr", "es", "k1", "kn", "knu", "num5", "o", "sx", "tr"],
}
_MULTI_ROWS: tuple[tuple[str, tuple[str, ...], Any], ...] = (
    ("ma", ("kn", "knu"), {"k": 1}),
    ("mb", ("n",), None),
    ("mc", (), None),
    ("md", ("sx",), "x"),
    ("me", ("knu",), {"k": "nu"}),
    ("mf", ("n", "knu"), None),
)
_ALL_MULTI = ["ma", "mb", "mc", "md", "me", "mf"]
# PocketBase 0.22.34 (probe ``null.multi_json``): the multi-match value of a
# json leaf is JSON_EXTRACT(...), NULL for a JSON null, which fails the
# "IS NULL" clause of every operator but ``=``.
_PB_MULTI_JSON = {
    'ts.meta != ""': ["ma", "md", "me"],
    'ts.meta = ""': [],
    "ts.meta = null": ["mb", "mc"],
    "ts.meta != null": ["ma", "md", "me"],
    'ts.meta ~ "nu"': ["ma", "me"],
    'ts.meta !~ "zz"': ["ma", "md", "me"],
    "ts.meta.k = null": ["mb", "mc", "md"],
    "ts.meta.k != null": ["me"],
    'ts.meta.k ~ "nu"': ["me"],
    'ts.meta.k !~ "zz"': ["me"],
    'ts.meta ?!= ""': _ALL_MULTI,
    'ts.meta ?~ "nu"': ["ma", "me", "mf"],
    "ts.meta = meta": ["mb", "mc", "md", "me"],
    "ts.meta != meta": ["ma"],
    "ts.meta ?= meta": ["mb", "mc", "md", "me", "mf"],
    '@collection.{json}.meta != ""': [],
    '@collection.{targets}.meta != ""': [],
    '@collection.{json}.meta !~ "zz"': [],
    "@collection.{json}.meta ?= null": _ALL_MULTI,
}


async def test_json_nulls_in_like_and_multi_match_follow_pocketbase(
    app_client: AsyncClient, admin_token: str, zv_env: dict
) -> None:
    suffix = uuid.uuid4().hex[:8]
    headers = {"Authorization": admin_token}
    json_coll = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zv_json_{suffix}",
            "schema": [_f("title", "text"), _f("meta", "json")],
        },
    )
    multi = None
    try:
        ids = {}
        for title, meta in _JSON_ROWS:
            payload: dict[str, Any] = {"title": title}
            if title != "n":
                payload["meta"] = meta
            ids[title] = (await _create(app_client, json_coll["name"], payload))["id"]
        for expression, expected in _PB_JSON_LIKE.items():
            got = await _title_list(
                app_client, json_coll["name"], sort="title", filter=expression
            )
            assert got == expected, expression

        multi = await _post_collection(
            app_client,
            admin_token,
            {
                "name": f"zv_multi_{suffix}",
                "schema": [
                    _f("title", "text"),
                    _f("meta", "json"),
                    _f("ts", "relation", collectionId=json_coll["id"], maxSelect=5),
                ],
            },
        )
        for title, targets, meta in _MULTI_ROWS:
            payload = {"title": title, "ts": [ids[t] for t in targets]}
            if meta is not None:
                payload["meta"] = meta
            await _create(app_client, multi["name"], payload)
        mismatches = {}
        for template, expected in _PB_MULTI_JSON.items():
            expression = template.format(
                json=json_coll["name"], targets=zv_env["targets"]
            )
            got = await _title_list(
                app_client, multi["name"], headers, sort="title", filter=expression
            )
            if got != expected:
                mismatches[template] = {"pocketbase": expected, "ppbase": got}
        assert mismatches == {}
    finally:
        if multi is not None:
            await app_client.delete(f"/api/collections/{multi['id']}", headers=headers)
        await app_client.delete(f"/api/collections/{json_coll['id']}", headers=headers)


# PocketBase 0.22.34 (probe ``null.jview_*``): json columns computed by a
# view (PocketBase infers ``json`` for an expression), NULL for row c.
_PB_JVIEW_FILTERS = {
    'jtxt = ""': "b",
    'jtxt != ""': "ac",
    "jtxt = null": "c",
    "jtxt != null": "ab",
    'jtxt = "tx"': "a",
    'jtxt != "tx"': "bc",
    '"tx" != jtxt': "bc",
    'jtxt ?= "tx"': "a",
    'jtxt ?!= "tx"': "bc",
    'jtxt ~ "t"': "a",
    'jtxt !~ "zz"': "ab",
    "jnum = 0": "b",
    "jnum != 0": "ac",
    "jnum != 4.5": "bc",
    "jnum > 1": "a",
    "jnum = null": "c",
    "jtxt = txt": "abc",
    "jtxt != txt": "",
}
_PB_JVIEW_SORTS = {
    "jtxt,title": "cba",
    "-jtxt,title": "abc",
    "jnum,title": "cba",
    "-jnum,title": "abc",
}


async def test_view_json_columns_compare_null_like_pocketbase(
    app_client: AsyncClient, admin_token: str, zv_env: dict
) -> None:
    """``json != value`` keeps the NULL row (``IS NOT``), as in BUMA views."""
    suffix = uuid.uuid4().hex[:8]
    jview = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zv_jview_{suffix}",
            "type": "view",
            "schema": [
                _f("title", "text"),
                _f("jtxt", "json"),
                _f("jnum", "json"),
                _f("txt", "text"),
            ],
            "options": {
                "query": (
                    "SELECT s.id, s.title, (t.txt || '') AS jtxt, "
                    f"(t.num + 0) AS jnum, t.txt FROM {zv_env['src']} s "
                    f"LEFT JOIN {zv_env['targets']} t ON t.id = s.target"
                )
            },
        },
    )
    try:
        mismatches = {}
        for expression, expected in _PB_JVIEW_FILTERS.items():
            got = await _titles(
                app_client, jview["name"], sort="title", filter=expression
            )
            if got != expected:
                mismatches[expression] = {"pocketbase": expected, "ppbase": got}
        for sort, expected in _PB_JVIEW_SORTS.items():
            got = await _titles(app_client, jview["name"], sort=sort)
            if got != expected:
                mismatches[sort] = {"pocketbase": expected, "ppbase": got}
        assert mismatches == {}
    finally:
        await app_client.delete(
            f"/api/collections/{jview['id']}",
            headers={"Authorization": admin_token},
        )


# PocketBase 0.22.34 (probe ``null.rules_*``): records ``oa`` (owned by the
# user) and ``ob`` (no owner).  An anonymous ``@request.auth.id`` is NULL: it
# equals an empty owner, and a ``?op`` without multi-match is ``op``.
_PB_RULES = {
    "@request.auth.id ?= owner": (["ob"], ["oa"]),
    "@request.auth.id = owner": (["ob"], ["oa"]),
    "owner ?= @request.auth.id": (["ob"], ["oa"]),
    "@request.auth.id ?!= owner": (["oa"], ["ob"]),
    "@request.auth.id != owner": (["oa"], ["ob"]),
    '@request.auth.id ?= ""': (["oa", "ob"], []),
    '@request.auth.id ?!= ""': ([], ["oa", "ob"]),
    "@request.auth.id ?= null": (["oa", "ob"], []),
}


async def test_any_equal_rules_on_an_empty_owner_follow_pocketbase(
    app_client: AsyncClient, admin_token: str
) -> None:
    """An anonymous caller lists the records without owner under
    ``@request.auth.id ?= owner`` (none before WS-A11), like PocketBase."""
    suffix = uuid.uuid4().hex[:8]
    headers = {"Authorization": admin_token}
    people = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zv_owners_{suffix}",
            "type": "auth",
            "schema": [],
            "options": {
                "allowEmailAuth": True,
                "allowUsernameAuth": True,
                "allowOAuth2Auth": False,
                "minPasswordLength": 8,
                "requireEmail": False,
            },
        },
    )
    owned = None
    try:
        password = "probe-User-Password-1"
        email = f"zvowner{suffix}@example.invalid"
        user = await _create(
            app_client,
            people["name"],
            {
                "username": f"zvowner{suffix}",
                "email": email,
                "password": password,
                "passwordConfirm": password,
            },
        )
        response = await app_client.post(
            f"/api/collections/{people['name']}/auth-with-password",
            json={"identity": email, "password": password},
        )
        assert response.status_code == 200, response.text
        user_headers = {"Authorization": response.json()["token"]}
        owned = await _post_collection(
            app_client,
            admin_token,
            {
                "name": f"zv_owned_{suffix}",
                "schema": [
                    _f("title", "text"),
                    _f("owner", "relation", collectionId=people["id"], maxSelect=1),
                ],
            },
        )
        await _create(app_client, owned["name"], {"title": "oa", "owner": user["id"]})
        await _create(app_client, owned["name"], {"title": "ob", "owner": ""})
        mismatches = {}
        for rule, (anonymous, owner) in _PB_RULES.items():
            response = await app_client.patch(
                f"/api/collections/{owned['id']}",
                headers=headers,
                json={"listRule": rule},
            )
            assert response.status_code == 200, response.text
            got = (
                await _title_list(app_client, owned["name"], sort="title"),
                await _title_list(
                    app_client, owned["name"], user_headers, sort="title"
                ),
            )
            if got != (anonymous, owner):
                mismatches[rule] = {"pocketbase": (anonymous, owner), "ppbase": got}
        assert mismatches == {}
    finally:
        if owned is not None:
            await app_client.delete(f"/api/collections/{owned['id']}", headers=headers)
        await app_client.delete(f"/api/collections/{people['id']}", headers=headers)


# Operands of different types (review of WS-A11).  PocketBase 0.22.34 builds
# a current-record ``?=``/``?!=`` like ``=``/``!=`` and SQLite compares a
# column with a value through the column affinity, two columns through
# ``COALESCE(x, '')`` without affinity (``docs/null-values.md``).  Values of
# the probe ``types`` section (rows a to i, view row n without target);
# PPBase answered HTTP 500 to every one of them before this change, except
# the operands of one type.
_TYPE_DAY = "2024-01-02 03:04:05.000Z"
_TYPE_ROWS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "a",
        {
            "txt": "1",
            "num": 1,
            "flag": True,
            "day": _TYPE_DAY,
            "sel": "x",
            "sels": ["x"],
            "rel": "ra",
            "rels": ["ra"],
        },
    ),
    (
        "b",
        {
            "txt": "1.0",
            "num": 4.5,
            "sel": "y",
            "sels": ["x", "y"],
            "rels": ["ra", "rb"],
        },
    ),
    ("c", {"txt": "4.5", "flag": True}),
    ("d", {"txt": "true", "num": 2}),
    ("e", {"txt": "0.1", "num": 0.1}),
    ("f", {}),
    ("g", {"txt": _TYPE_DAY, "day": _TYPE_DAY}),
    ("h", {"txt": "0.0", "num": -1, "flag": True}),
    ("i", {"txt": "0", "num": 1}),
)
_TYPE_FIELDS = ("txt", "num", "flag", "day", "sel", "sels", "rel", "rels")
_TYPE_OPERANDS = (
    "0",
    "1",
    "4.5",
    "0.1",
    "-1",
    "true",
    "false",
    '"1"',
    '"1.0"',
    '"4.5"',
    '"0.1"',
    '"true"',
    '"x"',
    '" 1 "',
    '"1e0"',
    '"+1"',
    f'"{_TYPE_DAY}"',
    "txt",
    "num",
    "flag",
    "day",
    "sel",
    "sels",
    "title",
)


@pytest.fixture(scope="module")
async def zt_env(app_client: AsyncClient, admin_token: str) -> Any:
    suffix = uuid.uuid4().hex[:8]
    refs = await _post_collection(
        app_client,
        admin_token,
        {"name": f"zt_refs_{suffix}", "schema": [_f("name", "text")]},
    )
    ref_ids = {
        name: (await _create(app_client, refs["name"], {"name": name}))["id"]
        for name in ("ra", "rb")
    }
    types = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zt_types_{suffix}",
            "schema": [
                _f("title", "text"),
                _f("txt", "text"),
                _f("num", "number"),
                _f("flag", "bool"),
                _f("day", "date"),
                _f("sel", "select", maxSelect=1, values=["x", "y"]),
                _f("sels", "select", maxSelect=2, values=["x", "y"]),
                _f("rel", "relation", collectionId=refs["id"], maxSelect=1),
                _f("rels", "relation", collectionId=refs["id"], maxSelect=5),
            ],
        },
    )
    link = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zt_link_{suffix}",
            "schema": [
                _f("title", "text"),
                _f("target", "relation", collectionId=types["id"], maxSelect=1),
            ],
        },
    )
    for title, values in _TYPE_ROWS:
        payload: dict[str, Any] = {"title": title, **values}
        if "rel" in payload:
            payload["rel"] = ref_ids[payload["rel"]]
        if "rels" in payload:
            payload["rels"] = [ref_ids[name] for name in payload["rels"]]
        created = await _create(app_client, types["name"], payload)
        await _create(
            app_client, link["name"], {"title": title, "target": created["id"]}
        )
    await _create(app_client, link["name"], {"title": "n", "target": ""})
    view = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"zt_view_{suffix}",
            "type": "view",
            "schema": [
                field
                for field in types["schema"]
                if field["name"] in {"title", *_TYPE_FIELDS}
            ],
            "options": {
                "query": (
                    "SELECT l.id, l.title, "
                    + ", ".join(f"t.{name}" for name in _TYPE_FIELDS)
                    + f" FROM {link['name']} l LEFT JOIN {types['name']} t "
                    "ON t.id = l.target"
                )
            },
        },
    )
    yield {"types": types["name"], "view": view["name"]}
    headers = {"Authorization": admin_token}
    for collection in (view, link, types, refs):
        await app_client.delete(f"/api/collections/{collection['id']}", headers=headers)


_PB_TYPES_BASE = {
    "txt ?= 0": "h",
    "txt ?= 1": "b",
    "txt ?= 4.5": "c",
    "txt ?= true": "a",
    'txt ?= "1"': "a",
    'txt ?= "1.0"': "b",
    'txt ?= "x"': "",
    'txt ?= " 1 "': "",
    "txt ?!= 0": "abcdefgi",
    "txt ?!= 1": "acdefghi",
    "txt ?!= 4.5": "abdefghi",
    "txt ?!= true": "bcdefghi",
    'txt ?!= "1"': "bcdefghi",
    'txt ?!= "1.0"': "acdefghi",
    'txt ?!= "x"': "abcdefghi",
    'txt ?!= " 1 "': "abcdefghi",
    "num ?= 0": "cfg",
    "num ?= 1": "ai",
    "num ?= 4.5": "b",
    "num ?= true": "ai",
    'num ?= "1"': "ai",
    'num ?= "1.0"': "ai",
    'num ?= "x"': "",
    'num ?= " 1 "': "ai",
    "num ?!= 0": "abdehi",
    "num ?!= 1": "bcdefgh",
    "num ?!= 4.5": "acdefghi",
    "num ?!= true": "bcdefgh",
    'num ?!= "1"': "bcdefgh",
    'num ?!= "1.0"': "bcdefgh",
    'num ?!= "x"': "abcdefghi",
    'num ?!= " 1 "': "bcdefgh",
    "flag ?= 0": "bdefgi",
    "flag ?= 1": "ach",
    "flag ?= 4.5": "",
    "flag ?= true": "ach",
    'flag ?= "1"': "ach",
    'flag ?= "1.0"': "ach",
    'flag ?= "x"': "",
    'flag ?= " 1 "': "ach",
    "flag ?!= 0": "ach",
    "flag ?!= 1": "bdefgi",
    "flag ?!= 4.5": "abcdefghi",
    "flag ?!= true": "bdefgi",
    'flag ?!= "1"': "bdefgi",
    'flag ?!= "1.0"': "bdefgi",
    'flag ?!= "x"': "abcdefghi",
    'flag ?!= " 1 "': "bdefgi",
    "day ?= 0": "",
    "day ?= 1": "",
    "day ?= 4.5": "",
    "day ?= true": "",
    'day ?= "1"': "",
    'day ?= "1.0"': "",
    'day ?= "x"': "",
    'day ?= " 1 "': "",
    "day ?!= 0": "abcdefghi",
    "day ?!= 1": "abcdefghi",
    "day ?!= 4.5": "abcdefghi",
    "day ?!= true": "abcdefghi",
    'day ?!= "1"': "abcdefghi",
    'day ?!= "1.0"': "abcdefghi",
    'day ?!= "x"': "abcdefghi",
    'day ?!= " 1 "': "abcdefghi",
    "sel ?= 0": "",
    "sel ?= 1": "",
    "sel ?= 4.5": "",
    "sel ?= true": "",
    'sel ?= "1"': "",
    'sel ?= "1.0"': "",
    'sel ?= "x"': "a",
    'sel ?= " 1 "': "",
    "sel ?!= 0": "abcdefghi",
    "sel ?!= 1": "abcdefghi",
    "sel ?!= 4.5": "abcdefghi",
    "sel ?!= true": "abcdefghi",
    'sel ?!= "1"': "abcdefghi",
    'sel ?!= "1.0"': "abcdefghi",
    'sel ?!= "x"': "bcdefghi",
    'sel ?!= " 1 "': "abcdefghi",
    "sels ?= 0": "",
    "sels ?= 1": "",
    "sels ?= 4.5": "",
    "sels ?= true": "",
    'sels ?= "1"': "",
    'sels ?= "1.0"': "",
    'sels ?= "x"': "",
    'sels ?= " 1 "': "",
    "sels ?!= 0": "abcdefghi",
    "sels ?!= 1": "abcdefghi",
    "sels ?!= 4.5": "abcdefghi",
    "sels ?!= true": "abcdefghi",
    'sels ?!= "1"': "abcdefghi",
    'sels ?!= "1.0"': "abcdefghi",
    'sels ?!= "x"': "abcdefghi",
    'sels ?!= " 1 "': "abcdefghi",
    "rel ?= 0": "",
    "rel ?= 1": "",
    "rel ?= 4.5": "",
    "rel ?= true": "",
    'rel ?= "1"': "",
    'rel ?= "1.0"': "",
    'rel ?= "x"': "",
    'rel ?= " 1 "': "",
    "rel ?!= 0": "abcdefghi",
    "rel ?!= 1": "abcdefghi",
    "rel ?!= 4.5": "abcdefghi",
    "rel ?!= true": "abcdefghi",
    'rel ?!= "1"': "abcdefghi",
    'rel ?!= "1.0"': "abcdefghi",
    'rel ?!= "x"': "abcdefghi",
    'rel ?!= " 1 "': "abcdefghi",
    "rels ?= 0": "",
    "rels ?= 1": "",
    "rels ?= 4.5": "",
    "rels ?= true": "",
    'rels ?= "1"': "",
    'rels ?= "1.0"': "",
    'rels ?= "x"': "",
    'rels ?= " 1 "': "",
    "rels ?!= 0": "abcdefghi",
    "rels ?!= 1": "abcdefghi",
    "rels ?!= 4.5": "abcdefghi",
    "rels ?!= true": "abcdefghi",
    'rels ?!= "1"': "abcdefghi",
    'rels ?!= "1.0"': "abcdefghi",
    'rels ?!= "x"': "abcdefghi",
    'rels ?!= " 1 "': "abcdefghi",
    "num ?= txt": "",
    "num ?!= txt": "abcdefghi",
    "txt ?= day": "fg",
    "day ?= sel": "cdefhi",
    "num ?= flag": "afg",
    "flag ?!= num": "bcdehi",
    "sels ?= num": "",
    "rel ?= day": "bcdefhi",
    "txt = 1": "b",
    'num = "1"': "ai",
    "flag = 1": "ach",
    "day != 1": "abcdefghi",
    "num != txt": "abcdefghi",
    "1 ?= txt": "b",
    '"1" ?!= num': "bcdefgh",
    "true ?= flag": "ach",
    'sels:length ?= "1"': "",
    "sels:length ?!= txt": "abcdefghi",
    '@now ?= "x"': "",
    "@now = txt": "",
}
_PB_TYPES_VIEW = {
    "txt ?= 0": "h",
    "txt ?= 1": "b",
    "txt ?= 4.5": "c",
    "txt ?= true": "a",
    'txt ?= "1"': "a",
    'txt ?= "1.0"': "b",
    'txt ?= "x"': "",
    'txt ?= " 1 "': "",
    "txt ?!= 0": "abcdefgin",
    "txt ?!= 1": "acdefghin",
    "txt ?!= 4.5": "abdefghin",
    "txt ?!= true": "bcdefghin",
    'txt ?!= "1"': "bcdefghin",
    'txt ?!= "1.0"': "acdefghin",
    'txt ?!= "x"': "abcdefghin",
    'txt ?!= " 1 "': "abcdefghin",
    "num ?= 0": "cfg",
    "num ?= 1": "ai",
    "num ?= 4.5": "b",
    "num ?= true": "ai",
    'num ?= "1"': "ai",
    'num ?= "1.0"': "ai",
    'num ?= "x"': "",
    'num ?= " 1 "': "ai",
    "num ?!= 0": "abdehin",
    "num ?!= 1": "bcdefghn",
    "num ?!= 4.5": "acdefghin",
    "num ?!= true": "bcdefghn",
    'num ?!= "1"': "bcdefghn",
    'num ?!= "1.0"': "bcdefghn",
    'num ?!= "x"': "abcdefghin",
    'num ?!= " 1 "': "bcdefghn",
    "flag ?= 0": "bdefgi",
    "flag ?= 1": "ach",
    "flag ?= 4.5": "",
    "flag ?= true": "ach",
    'flag ?= "1"': "ach",
    'flag ?= "1.0"': "ach",
    'flag ?= "x"': "",
    'flag ?= " 1 "': "ach",
    "flag ?!= 0": "achn",
    "flag ?!= 1": "bdefgin",
    "flag ?!= 4.5": "abcdefghin",
    "flag ?!= true": "bdefgin",
    'flag ?!= "1"': "bdefgin",
    'flag ?!= "1.0"': "bdefgin",
    'flag ?!= "x"': "abcdefghin",
    'flag ?!= " 1 "': "bdefgin",
    "day ?= 0": "",
    "day ?= 1": "",
    "day ?= 4.5": "",
    "day ?= true": "",
    'day ?= "1"': "",
    'day ?= "1.0"': "",
    'day ?= "x"': "",
    'day ?= " 1 "': "",
    "day ?!= 0": "abcdefghin",
    "day ?!= 1": "abcdefghin",
    "day ?!= 4.5": "abcdefghin",
    "day ?!= true": "abcdefghin",
    'day ?!= "1"': "abcdefghin",
    'day ?!= "1.0"': "abcdefghin",
    'day ?!= "x"': "abcdefghin",
    'day ?!= " 1 "': "abcdefghin",
    "sel ?= 0": "",
    "sel ?= 1": "",
    "sel ?= 4.5": "",
    "sel ?= true": "",
    'sel ?= "1"': "",
    'sel ?= "1.0"': "",
    'sel ?= "x"': "a",
    'sel ?= " 1 "': "",
    "sel ?!= 0": "abcdefghin",
    "sel ?!= 1": "abcdefghin",
    "sel ?!= 4.5": "abcdefghin",
    "sel ?!= true": "abcdefghin",
    'sel ?!= "1"': "abcdefghin",
    'sel ?!= "1.0"': "abcdefghin",
    'sel ?!= "x"': "bcdefghin",
    'sel ?!= " 1 "': "abcdefghin",
    "sels ?= 0": "",
    "sels ?= 1": "",
    "sels ?= 4.5": "",
    "sels ?= true": "",
    'sels ?= "1"': "",
    'sels ?= "1.0"': "",
    'sels ?= "x"': "",
    'sels ?= " 1 "': "",
    "sels ?!= 0": "abcdefghin",
    "sels ?!= 1": "abcdefghin",
    "sels ?!= 4.5": "abcdefghin",
    "sels ?!= true": "abcdefghin",
    'sels ?!= "1"': "abcdefghin",
    'sels ?!= "1.0"': "abcdefghin",
    'sels ?!= "x"': "abcdefghin",
    'sels ?!= " 1 "': "abcdefghin",
    "rel ?= 0": "",
    "rel ?= 1": "",
    "rel ?= 4.5": "",
    "rel ?= true": "",
    'rel ?= "1"': "",
    'rel ?= "1.0"': "",
    'rel ?= "x"': "",
    'rel ?= " 1 "': "",
    "rel ?!= 0": "abcdefghin",
    "rel ?!= 1": "abcdefghin",
    "rel ?!= 4.5": "abcdefghin",
    "rel ?!= true": "abcdefghin",
    'rel ?!= "1"': "abcdefghin",
    'rel ?!= "1.0"': "abcdefghin",
    'rel ?!= "x"': "abcdefghin",
    'rel ?!= " 1 "': "abcdefghin",
    "rels ?= 0": "",
    "rels ?= 1": "",
    "rels ?= 4.5": "",
    "rels ?= true": "",
    'rels ?= "1"': "",
    'rels ?= "1.0"': "",
    'rels ?= "x"': "",
    'rels ?= " 1 "': "",
    "rels ?!= 0": "abcdefghin",
    "rels ?!= 1": "abcdefghin",
    "rels ?!= 4.5": "abcdefghin",
    "rels ?!= true": "abcdefghin",
    'rels ?!= "1"': "abcdefghin",
    'rels ?!= "1.0"': "abcdefghin",
    'rels ?!= "x"': "abcdefghin",
    'rels ?!= " 1 "': "abcdefghin",
    "num ?= txt": "n",
    "num ?!= txt": "abcdefghi",
    "txt ?= day": "fgn",
    "day ?= sel": "cdefhin",
    "num ?= flag": "afgn",
    "flag ?!= num": "bcdehi",
    "sels ?= num": "n",
    "rel ?= day": "bcdefhin",
    "txt = 1": "b",
    'num = "1"': "ai",
    "flag = 1": "ach",
    "day != 1": "abcdefghin",
    "num != txt": "abcdefghi",
    "1 ?= txt": "b",
    '"1" ?!= num': "bcdefghn",
    "true ?= flag": "ach",
    'sels:length ?= "1"': "",
    "sels:length ?!= txt": "abcdefghin",
    '@now ?= "x"': "",
    "@now = txt": "",
}
_PB_TYPE_QUERIES = {
    ("1", "@request.query.q ?= num"): "ai",
    ("1", "@request.query.q ?!= flag"): "bdefgin",
    ("1.0", "@request.query.q ?= txt"): "b",
    ("x", "@request.query.q ?!= num"): "abcdefghin",
    ("1", "@request.query.q = day"): "",
    ("x", "@request.query.q != sels"): "abcdefghin",
}
_PB_JSON_COLUMNS = {
    "meta ?= txt": "c",
    "meta ?!= txt": "ab",
    "meta ?= meta": "abc",
    "meta ?!= meta": "",
    "txt ?= meta": "c",
    "num ?!= meta": "ab",
    "meta ?= sels": "c",
    "meta ?!= tid": "ab",
    "meta = flag": "c",
    "meta != rel": "ab",
}


async def test_operands_of_different_types_compare_like_sqlite(
    app_client: AsyncClient, zt_env: dict
) -> None:
    mismatches = {}
    for name, expected_values in (
        (zt_env["types"], _PB_TYPES_BASE),
        (zt_env["view"], _PB_TYPES_VIEW),
    ):
        for expression, expected in expected_values.items():
            got = await _titles(app_client, name, sort="title", filter=expression)
            if got != expected:
                mismatches[(name, expression)] = {
                    "pocketbase": expected,
                    "ppbase": got,
                }
    assert mismatches == {}


async def test_request_values_of_another_type_compare_like_sqlite(
    app_client: AsyncClient, admin_token: str, zt_env: dict
) -> None:
    # ``@request.*`` in a list filter is an admin-only feature.
    headers = {"Authorization": admin_token}
    mismatches = {}
    for (q, expression), expected in _PB_TYPE_QUERIES.items():
        got = await _titles(
            app_client, zt_env["view"], headers, sort="title", filter=expression, q=q
        )
        if got != expected:
            mismatches[(q, expression)] = {"pocketbase": expected, "ppbase": got}
    assert mismatches == {}


async def test_any_equal_never_fails_on_operands_of_another_type(
    app_client: AsyncClient, zt_env: dict
) -> None:
    """``?=``/``?!=`` with every operand type answer 200 (they compared
    texts before WS-A11 and turned into HTTP 500 as ``=``/``!=``)."""
    failures = {}
    for field in _TYPE_FIELDS:
        for op in ("?=", "?!="):
            for operand in _TYPE_OPERANDS:
                expression = f"{field} {op} {operand}"
                response = await app_client.get(
                    f"/api/collections/{zt_env['view']}/records",
                    params={"filter": expression, "perPage": 200},
                )
                if response.status_code != 200:
                    failures[expression] = response.status_code
    assert failures == {}


async def test_json_any_equal_to_a_column_follows_pocketbase(
    app_client: AsyncClient, zv_env: dict
) -> None:
    """``json ?= column``/``?!=`` are ``=``/``!=``: two NULLs are equal (view
    row c), a NULL differs from any value (PocketBase 0.22.34, probe
    ``types.json_columns``)."""
    mismatches = {}
    for expression, expected in _PB_JSON_COLUMNS.items():
        got = await _titles(app_client, zv_env["view"], sort="title", filter=expression)
        if got != expected:
            mismatches[expression] = {"pocketbase": expected, "ppbase": got}
    assert mismatches == {}
