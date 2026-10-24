"""PostgreSQL integration tests for relations with ``maxSelect: null`` (D-0019).

PocketBase 0.22 stores such relations as JSON arrays (``RelationOptions``
``MaxSelect *int``, ``IsMultiple() = MaxSelect == nil || *MaxSelect > 1``,
v0.22.34 ``models/schema/schema_field.go`` l.715-716).  Every expected value
below was observed on the official PocketBase 0.22.34 binary with the same
requests (synthetic data only).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ppbase.db.engine import get_engine
from ppbase.db.schema_manager import reconcile_multi_value_columns
from ppbase.ext.registry import HOOK_RECORD_CREATE_REQUEST, ExtensionRegistry
from ppbase.services.realtime_service import (
    SubscriptionManager,
    broadcast_record_change,
)
from ppbase.services.record_service import (
    create_record,
    get_record,
    resolve_collection,
    update_record,
)

pytestmark = pytest.mark.asyncio

_PUBLIC_RULES = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}


def _relation(name: str, collection_id: str, max_select: Any) -> dict[str, Any]:
    """A relation field exactly as PocketBase 0.22 exports it (nested options)."""
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


async def _post_collection(client: AsyncClient, token: str, body: dict) -> dict:
    response = await client.post(
        "/api/collections",
        headers={"Authorization": token},
        json={**_PUBLIC_RULES, "type": "base", **body},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _column_types(table: str) -> dict[str, tuple[str, str | None]]:
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), "
                "pg_catalog.pg_get_expr(d.adbin, d.adrelid) "
                "FROM pg_catalog.pg_attribute a "
                "LEFT JOIN pg_catalog.pg_attrdef d "
                "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = CAST(:table AS regclass) AND a.attnum > 0 "
                "AND NOT a.attisdropped"
            ),
            {"table": f'"{table}"'},
        )
        return {row[0]: (row[1], row[2]) for row in rows}


async def _index_methods(table: str) -> dict[str, str]:
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT i.relname, am.amname FROM pg_catalog.pg_index x "
                "JOIN pg_catalog.pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_catalog.pg_am am ON am.oid = i.relam "
                "WHERE x.indrelid = CAST(:table AS regclass)"
            ),
            {"table": f'"{table}"'},
        )
        return {row[0]: row[1] for row in rows}


@pytest.fixture
async def relation_env(app_client: AsyncClient, admin_token: str) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    targets = await _post_collection(
        app_client,
        admin_token,
        {"name": f"rel_targets_{suffix}", "schema": [{"name": "name", "type": "text"}]},
    )
    owners = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"rel_owners_{suffix}",
            "schema": [
                {"name": "title", "type": "text"},
                _relation("rels", targets["id"], None),
                _relation("one", targets["id"], 1),
            ],
        },
    )
    ids = {}
    for key, name in (("t1", "alpha"), ("t2", "beta"), ("t3", "gamma")):
        rid = f"{key}{suffix}{'0' * (13 - len(suffix))}"[:15]
        response = await app_client.post(
            f"/api/collections/{targets['name']}/records",
            json={"id": rid, "name": name},
        )
        assert response.status_code == 200, response.text
        ids[key] = rid
    yield {"targets": targets, "owners": owners, "suffix": suffix, **ids}
    for collection in (owners, targets):
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def _create(client: AsyncClient, env: dict, payload: dict) -> dict:
    response = await client.post(
        f"/api/collections/{env['owners']['name']}/records",
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _list(client: AsyncClient, env: dict, **params: Any) -> list[dict]:
    response = await client.get(
        f"/api/collections/{env['owners']['name']}/records",
        params={"sort": "title", "perPage": 200, **params},
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def test_null_max_select_creates_array_column_and_keeps_metadata(
    app_client: AsyncClient,
    admin_token: str,
    relation_env: dict,
) -> None:
    owners = relation_env["owners"]
    columns = await _column_types(owners["name"])
    assert columns["rels"] == ("text[]", "'{}'::text[]")
    assert columns["one"] == ("text", "''::text")

    indexes = await _index_methods(owners["name"])
    assert indexes[f"idx_{owners['name']}_rels"] == "gin"
    assert indexes[f"idx_{owners['name']}_one"] == "btree"

    response = await app_client.get(
        f"/api/collections/{owners['name']}",
        headers={"Authorization": admin_token},
    )
    assert response.status_code == 200
    body = response.json()
    schema = {field["name"]: field for field in body["schema"]}
    fields = {field["name"]: field for field in body["fields"]}
    assert "maxSelect" in schema["rels"]["options"]
    assert schema["rels"]["options"]["maxSelect"] is None
    assert "maxSelect" in fields["rels"] and fields["rels"]["maxSelect"] is None
    assert schema["one"]["options"]["maxSelect"] == 1


async def test_create_values_follow_pocketbase_prepare_value(
    app_client: AsyncClient,
    relation_env: dict,
) -> None:
    t1, t2, t3 = relation_env["t1"], relation_env["t2"], relation_env["t3"]
    cases = [
        ({"rels": []}, []),
        ({"rels": [t1]}, [t1]),
        ({"rels": [t2, t1]}, [t2, t1]),  # order kept
        ({"rels": t3}, [t3]),  # one id -> one-element list
        ({"rels": [t1, t1, t2]}, [t1, t2]),  # PrepareValue de-duplicates
        ({"rels": json.dumps([t2, t3])}, [t2, t3]),  # JSON string decoded
        ({}, []),
        ({"rels": None}, []),
        ({"rels": ""}, []),
    ]
    for payload, expected in cases:
        record = await _create(app_client, relation_env, {"title": "x", **payload})
        assert record["rels"] == expected, payload
        assert record["one"] == ""

    single = await _create(app_client, relation_env, {"title": "s", "one": [t1, t2]})
    assert single["one"] == t2  # single relation keeps the last id
    assert single["rels"] == []

    multipart_two = await app_client.post(
        f"/api/collections/{relation_env['owners']['name']}/records",
        files=[("title", (None, "mp")), ("rels", (None, t1)), ("rels", (None, t3))],
    )
    assert multipart_two.status_code == 200, multipart_two.text
    assert multipart_two.json()["rels"] == [t1, t3]
    multipart_one = await app_client.post(
        f"/api/collections/{relation_env['owners']['name']}/records",
        files=[("title", (None, "mp1")), ("rels", (None, t2))],
    )
    assert multipart_one.status_code == 200, multipart_one.text
    assert multipart_one.json()["rels"] == [t2]


async def test_api_rejects_missing_relation_targets_like_pocketbase(
    app_client: AsyncClient,
    relation_env: dict,
) -> None:
    t1 = relation_env["t1"]
    missing = "missing" + relation_env["suffix"]
    joined = f"{relation_env['t1']},{relation_env['t2']}"  # 31-character orphan
    owners = relation_env["owners"]["name"]
    for payload, field in (
        ({"rels": [missing]}, "rels"),
        ({"rels": [t1, missing]}, "rels"),
        ({"rels": [joined]}, "rels"),
        ({"one": missing}, "one"),
    ):
        response = await app_client.post(
            f"/api/collections/{owners}/records",
            json={"title": "bad", **payload},
        )
        assert response.status_code == 400, response.text
        assert response.json()["data"] == {
            field: {
                "code": "validation_missing_rel_records",
                "message": "Failed to find all relation records with the provided ids.",
            }
        }

    record = await _create(app_client, relation_env, {"title": "ok", "rels": [t1]})
    response = await app_client.patch(
        f"/api/collections/{owners}/records/{record['id']}",
        json={"rels": [missing]},
    )
    assert response.status_code == 400, response.text
    assert response.json()["data"]["rels"]["code"] == "validation_missing_rel_records"


async def test_internal_writes_keep_orphans_and_long_elements_unchanged(
    app_client: AsyncClient,
    relation_env: dict,
) -> None:
    """record_service without validate_relations mirrors dao.SaveRecord.

    PocketBase 0.22 hooks save through the DAO, which never checks relation
    targets, so historical orphans (D-0021) stay importable and writable.
    """
    engine = get_engine()
    owners = await resolve_collection(engine, relation_env["owners"]["name"])
    joined = f"{relation_env['t1']},{relation_env['t3']}"
    record = await create_record(
        engine,
        owners,
        {"title": "orphan", "rels": [joined, relation_env["t2"]]},
    )
    assert record["rels"] == [joined, relation_env["t2"]]
    assert len(joined) == 31

    # A string written by internal code becomes a one-element list.
    updated = await update_record(
        engine, owners, record["id"], {"rels": relation_env["t1"]}
    )
    assert updated is not None and updated["rels"] == [relation_env["t1"]]
    # A list written to a single relation keeps the last id.
    updated = await update_record(
        engine, owners, record["id"], {"one": [relation_env["t1"], relation_env["t3"]]}
    )
    assert updated is not None and updated["one"] == relation_env["t3"]

    # The API validates the whole record on update, like PocketBase's form:
    # an untouched orphan relation still fails.
    await update_record(engine, owners, record["id"], {"rels": [joined]})
    response = await app_client.patch(
        f"/api/collections/{owners.name}/records/{record['id']}",
        json={"title": "touch"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["data"]["rels"]["code"] == "validation_missing_rel_records"


async def test_update_modifiers_and_replacements(
    app_client: AsyncClient,
    relation_env: dict,
) -> None:
    t1, t2, t3 = relation_env["t1"], relation_env["t2"], relation_env["t3"]
    record = await _create(app_client, relation_env, {"title": "mod", "rels": [t1]})
    path = f"/api/collections/{relation_env['owners']['name']}/records/{record['id']}"
    steps = [
        ({"rels+": [t2, t1]}, [t1, t2]),
        ({"rels-": [t1]}, [t2]),
        ({"rels": t3}, [t3]),
        ({"rels": [t3, t2, t3]}, [t3, t2]),
        ({"rels": []}, []),
    ]
    for body, expected in steps:
        response = await app_client.patch(path, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["rels"] == expected, body
    response = await app_client.patch(path, json={"one": [t3, t1]})
    assert response.status_code == 200, response.text
    assert response.json()["one"] == t1


async def test_filters_match_pocketbase_on_direct_and_nested_paths(
    app_client: AsyncClient,
    relation_env: dict,
) -> None:
    t1, t2, t3 = relation_env["t1"], relation_env["t2"], relation_env["t3"]
    for title, rels in (
        ("dups", [t1, t2]),
        ("empty", []),
        ("one", [t1]),
        ("string", [t3]),
        ("two", [t2, t1]),
    ):
        await _create(app_client, relation_env, {"title": title, "rels": rels})

    all_titles = ["dups", "empty", "one", "string", "two"]
    expected = {
        # A direct multi-value field compares its stored JSON text.
        f'rels = "{t1}"': [],
        f'rels != "{t1}"': all_titles,
        f'rels ?= "{t1}"': [],
        f'rels ?!= "{t1}"': all_titles,
        'rels = ""': [],
        'rels != ""': all_titles,
        "rels = null": [],
        'rels = "[]"': ["empty"],
        f"rels = '[\"{t1}\"]'": ["one"],
        f'rels ~ "{t1}"': ["dups", "one", "two"],
        f'rels !~ "{t1}"': ["empty", "string"],
        f'rels ?~ "{t1}"': ["dups", "one", "two"],
        # Any-match traversal through the multiple relation.
        'rels.name ?= "alpha"': ["dups", "one", "two"],
        'rels.name ?~ "alp"': ["dups", "one", "two"],
        f'rels.id ?= "{t1}"': ["dups", "one", "two"],
        "rels:length = 2": ["dups", "two"],
        "rels:length = 0": ["empty"],
    }
    for expression, titles in expected.items():
        items = await _list(app_client, relation_env, filter=expression)
        assert [item["title"] for item in items] == titles, expression

    back = f"{relation_env['owners']['name']}_via_rels"
    response = await app_client.get(
        f"/api/collections/{relation_env['targets']['name']}/records",
        params={"filter": f'{back}.title ?= "one"', "sort": "name"},
    )
    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()["items"]] == ["alpha"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known PPBase divergence (not introduced by D-0019): traversals and "
        ":each through a multiple relation use any-match EXISTS, while "
        "PocketBase applies its match-all multi-match subquery."
    ),
)
async def test_match_all_traversal_semantics_like_pocketbase(
    app_client: AsyncClient,
    relation_env: dict,
) -> None:
    t1, t2 = relation_env["t1"], relation_env["t2"]
    for title, rels in (("dups", [t1, t2]), ("empty", []), ("one", [t1])):
        await _create(app_client, relation_env, {"title": title, "rels": rels})
    items = await _list(app_client, relation_env, filter='rels.name = "alpha"')
    assert [item["title"] for item in items] == ["one"]  # PocketBase 0.22.34


async def test_expand_returns_lists_in_field_order(
    app_client: AsyncClient,
    relation_env: dict,
) -> None:
    t1, t2 = relation_env["t1"], relation_env["t2"]
    missing = "missing" + relation_env["suffix"]
    await _create(
        app_client, relation_env, {"title": "two", "rels": [t2, t1], "one": t2}
    )
    engine = get_engine()
    owners = await resolve_collection(engine, relation_env["owners"]["name"])
    await create_record(engine, owners, {"title": "zorphan", "rels": [missing, t1]})

    items = await _list(app_client, relation_env, expand="rels,one")
    two, orphan = items
    assert [record["name"] for record in two["expand"]["rels"]] == ["beta", "alpha"]
    assert two["expand"]["one"]["name"] == "beta"
    # Missing targets are skipped, the list shape is kept.
    assert [record["name"] for record in orphan["expand"]["rels"]] == ["alpha"]
    assert orphan["rels"] == [missing, t1]


async def test_realtime_payload_carries_the_relation_list(
    app_client: AsyncClient,
    relation_env: dict,
) -> None:
    t1, t3 = relation_env["t1"], relation_env["t3"]
    record = await _create(app_client, relation_env, {"title": "rt", "rels": [t3, t1]})
    engine = get_engine()
    owners = await resolve_collection(engine, relation_env["owners"]["name"])
    manager = SubscriptionManager()
    client_id = manager.register_client()
    await manager.add_subscription(
        client_id,
        f"{owners.name}/*",
        auth_payload={"id": "admin", "type": "admin"},
    )
    record_data = await get_record(engine, owners, record["id"])
    await broadcast_record_change(
        manager,
        owners.name,
        record["id"],
        "update",
        record_data,
        engine=engine,
        collection=owners,
    )
    session = manager.get_session(client_id)
    assert session is not None and not session.response_queue.empty()
    event = session.response_queue.get_nowait()
    assert event["data"]["record"]["rels"] == [t3, t1]


async def test_collection_update_without_change_keeps_type_and_values(
    app_client: AsyncClient,
    admin_token: str,
    relation_env: dict,
) -> None:
    t1, t2 = relation_env["t1"], relation_env["t2"]
    await _create(app_client, relation_env, {"title": "two", "rels": [t2, t1]})
    owners = relation_env["owners"]
    current = await app_client.get(
        f"/api/collections/{owners['name']}", headers={"Authorization": admin_token}
    )
    before = await _list(app_client, relation_env)
    # Once with the nested 0.22 schema, once with the flat fields the dashboard sends back.
    for body in (
        {"schema": current.json()["schema"]},
        {"schema": current.json()["fields"]},
    ):
        response = await app_client.patch(
            f"/api/collections/{owners['id']}",
            headers={"Authorization": admin_token},
            json=body,
        )
        assert response.status_code == 200, response.text
        options = {field["name"]: field for field in response.json()["schema"]}["rels"][
            "options"
        ]
        assert options["maxSelect"] is None
        assert (await _column_types(owners["name"]))["rels"][0] == "text[]"
        assert await _list(app_client, relation_env) == before


async def test_multiplicity_changes_convert_values_like_pocketbase(
    app_client: AsyncClient,
    admin_token: str,
    relation_env: dict,
) -> None:
    t1, t2, t3 = relation_env["t1"], relation_env["t2"], relation_env["t3"]
    for title, rels in (("a", [t1, t2]), ("b", []), ("c", [t2, t3, t1])):
        await _create(app_client, relation_env, {"title": title, "rels": rels})
    owners = relation_env["owners"]
    schema = (
        await app_client.get(
            f"/api/collections/{owners['name']}", headers={"Authorization": admin_token}
        )
    ).json()["schema"]
    single = json.loads(json.dumps(schema))
    for field in single:
        if field["name"] == "rels":
            field["options"]["maxSelect"] = 1
    response = await app_client.patch(
        f"/api/collections/{owners['id']}",
        headers={"Authorization": admin_token},
        json={"schema": single},
    )
    assert response.status_code == 200, response.text
    assert (await _column_types(owners["name"]))["rels"] == ("text", "''::text")
    assert (await _index_methods(owners["name"]))[
        f"idx_{owners['name']}_rels"
    ] == "btree"
    # multiple -> single keeps the last element (PocketBase json_extract '$[#-1]').
    assert [(i["title"], i["rels"]) for i in await _list(app_client, relation_env)] == [
        ("a", t2),
        ("b", ""),
        ("c", t1),
    ]

    response = await app_client.patch(
        f"/api/collections/{owners['id']}",
        headers={"Authorization": admin_token},
        json={"schema": schema},
    )
    assert response.status_code == 200, response.text
    assert (await _column_types(owners["name"]))["rels"] == ("text[]", "'{}'::text[]")
    assert (await _index_methods(owners["name"]))[f"idx_{owners['name']}_rels"] == "gin"
    # single -> multiple wraps the id, '' becomes [].
    assert [(i["title"], i["rels"]) for i in await _list(app_client, relation_env)] == [
        ("a", [t2]),
        ("b", []),
        ("c", [t1]),
    ]


async def test_current_format_relations_are_unchanged(
    app_client: AsyncClient,
    admin_token: str,
    relation_env: dict,
) -> None:
    target_id = relation_env["targets"]["id"]
    flat = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"rel_flat_{relation_env['suffix']}",
            "fields": [
                {
                    "name": "zero",
                    "type": "relation",
                    "collectionId": target_id,
                    "maxSelect": 0,
                },
                {
                    "name": "one",
                    "type": "relation",
                    "collectionId": target_id,
                    "maxSelect": 1,
                },
                {"name": "absent", "type": "relation", "collectionId": target_id},
                {
                    "name": "two",
                    "type": "relation",
                    "collectionId": target_id,
                    "maxSelect": 2,
                },
            ],
        },
    )
    try:
        columns = await _column_types(flat["name"])
        assert columns["zero"][0] == "text"
        assert columns["one"][0] == "text"
        assert columns["absent"][0] == "text"
        assert columns["two"][0] == "text[]"
        t1, t2, t3 = relation_env["t1"], relation_env["t2"], relation_env["t3"]
        response = await app_client.post(
            f"/api/collections/{flat['name']}/records",
            json={"zero": [t1, t2], "one": t1, "absent": [t2], "two": [t1, t2]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["zero"], body["one"], body["absent"], body["two"]) == (
            t2,
            t1,
            t2,
            [t1, t2],
        )
        too_many = await app_client.post(
            f"/api/collections/{flat['name']}/records",
            json={"two": [t1, t2, t3]},
        )
        assert too_many.status_code == 400
        assert (
            too_many.json()["data"]["two"]["code"] == "validation_max_select_constraint"
        )
    finally:
        await app_client.delete(
            f"/api/collections/{flat['id']}", headers={"Authorization": admin_token}
        )


async def test_legacy_varchar_relation_columns_keep_working(
    app_client: AsyncClient,
    admin_token: str,
    relation_env: dict,
) -> None:
    """Columns created before TEXT relations (VARCHAR(15)/VARCHAR(15)[]) work."""
    target_id = relation_env["targets"]["id"]
    legacy = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"rel_legacy_{relation_env['suffix']}",
            "schema": [
                {"name": "title", "type": "text"},
                _relation("many", target_id, 5),
                _relation("rels", target_id, None),
                _relation("one", target_id, 1),
            ],
        },
    )
    table = legacy["name"]
    try:
        async with get_engine().begin() as conn:
            for column, ddl_type, default in (
                ("many", "VARCHAR(15)[]", "'{}'"),
                ("rels", "VARCHAR(15)[]", "'{}'"),
                ("one", "VARCHAR(15)", "''"),
            ):
                await conn.execute(text(f'DROP INDEX IF EXISTS "idx_{table}_{column}"'))
                await conn.execute(
                    text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT')
                )
                await conn.execute(
                    text(
                        f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE {ddl_type} '
                        f'USING "{column}"::{ddl_type}'
                    )
                )
                await conn.execute(
                    text(
                        f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {default}'
                    )
                )
        columns = await _column_types(table)
        assert columns["rels"][0] == "character varying(15)[]"
        assert columns["one"][0] == "character varying(15)"

        # A shape-compatible legacy column is never rewritten.
        collection = await resolve_collection(get_engine(), table)
        assert await reconcile_multi_value_columns(get_engine(), collection) == []
        assert (await _column_types(table))["rels"][0] == "character varying(15)[]"

        t1, t2, t3 = relation_env["t1"], relation_env["t2"], relation_env["t3"]
        path = f"/api/collections/{table}/records"
        first = await app_client.post(
            path, json={"title": "a", "many": [t1, t2], "rels": [t2, t1], "one": t3}
        )
        assert first.status_code == 200, first.text
        second = await app_client.post(path, json={"title": "b", "rels": t3})
        assert second.status_code == 200, second.text
        assert second.json()["rels"] == [t3]

        patched = await app_client.patch(
            f"{path}/{first.json()['id']}", json={"rels+": [t3], "many-": [t1]}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["rels"] == [t2, t1, t3]
        assert patched.json()["many"] == [t2]

        expected = {
            f'rels ~ "{t1}"': ["a"],
            f'rels ?= "{t1}"': [],
            f'rels.id ?= "{t3}"': ["a", "b"],
            'rels.name ?= "gamma"': ["a", "b"],
            f'one = "{t3}"': ["a"],
            'one.name = "gamma"': ["a"],
            f'many ~ "{t2}"': ["a"],
            "rels:length = 1": ["b"],
        }
        for expression, titles in expected.items():
            response = await app_client.get(
                path,
                params={"filter": expression, "sort": "title", "expand": "rels,one"},
            )
            assert response.status_code == 200, (expression, response.text)
            assert [item["title"] for item in response.json()["items"]] == titles, (
                expression
            )

        listed = await app_client.get(
            path, params={"sort": "title", "expand": "rels,one"}
        )
        item = listed.json()["items"][0]
        assert [record["name"] for record in item["expand"]["rels"]] == [
            "beta",
            "alpha",
            "gamma",
        ]
        assert item["expand"]["one"]["name"] == "gamma"

        # A 31-character orphan does not fit the legacy column: it is refused
        # by PostgreSQL rather than silently truncated.
        joined = f"{t1},{t2}"
        with pytest.raises(DBAPIError):
            await update_record(
                get_engine(), collection, first.json()["id"], {"rels": [joined]}
            )
        unchanged = await get_record(get_engine(), collection, first.json()["id"])
        assert unchanged is not None and unchanged["rels"] == [t2, t1, t3]
    finally:
        await app_client.delete(
            f"/api/collections/{legacy['id']}", headers={"Authorization": admin_token}
        )


async def test_reconcile_converts_a_pre_rule_scalar_relation_column(
    app_client: AsyncClient,
    admin_token: str,
    relation_env: dict,
) -> None:
    """A null-maxSelect relation created as VARCHAR(15) before the rule."""
    target_id = relation_env["targets"]["id"]
    created = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"rel_scalar_{relation_env['suffix']}",
            "schema": [
                {"name": "title", "type": "text"},
                _relation("rels", target_id, 1),
            ],
        },
    )
    table = created["name"]
    t1 = relation_env["t1"]
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP INDEX IF EXISTS "idx_{table}_rels"'))
            await conn.execute(
                text(f'ALTER TABLE "{table}" ALTER COLUMN "rels" DROP DEFAULT')
            )
            await conn.execute(
                text(f'ALTER TABLE "{table}" ALTER COLUMN "rels" TYPE VARCHAR(15)')
            )
            await conn.execute(
                text(f'ALTER TABLE "{table}" ALTER COLUMN "rels" SET DEFAULT \'\'')
            )
            await conn.execute(
                text(
                    f'CREATE INDEX "idx_{table}_rels" ON "{table}" ("rels") '
                    "WHERE \"rels\" != ''"
                )
            )
            for rid, value in (
                ("aaaaaaaaaaaaaa1", ""),
                ("aaaaaaaaaaaaaa2", t1),
                ("aaaaaaaaaaaaaa3", "[]"),
            ):
                await conn.execute(
                    text(
                        f'INSERT INTO "{table}" (id, title, rels) VALUES (:id, :title, :v)'
                    ),
                    {"id": rid, "title": rid, "v": value},
                )
            # The pre-rule metadata already said maxSelect: null (PocketBase 0.22 export).
            schema = [
                {"name": "title", "type": "text"},
                _relation("rels", target_id, None),
            ]
            await conn.execute(
                text(
                    'UPDATE "_collections" SET schema = CAST(:s AS jsonb) WHERE id = :id'
                ),
                {"s": json.dumps(schema), "id": created["id"]},
            )
        collection = await resolve_collection(engine, table)

        # Rollback leaves the column untouched.
        async with engine.connect() as conn:
            transaction = await conn.begin()
            assert await reconcile_multi_value_columns(conn, collection) == ["rels"]
            await transaction.rollback()
        assert (await _column_types(table))["rels"][0] == "character varying(15)"

        async with engine.begin() as conn:
            assert await reconcile_multi_value_columns(
                conn, collection, field_names=["rels"]
            ) == ["rels"]
        assert (await _column_types(table))["rels"] == ("text[]", "'{}'::text[]")
        assert (await _index_methods(table))[f"idx_{table}_rels"] == "gin"
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text(f'SELECT id, rels FROM "{table}" ORDER BY id'))
            ).all()
        assert [tuple(row) for row in rows] == [
            ("aaaaaaaaaaaaaa1", []),
            ("aaaaaaaaaaaaaa2", [t1]),
            ("aaaaaaaaaaaaaa3", []),
        ]
        # Replay is a no-op.
        assert await reconcile_multi_value_columns(engine, collection) == []
        with pytest.raises(ValueError):
            await reconcile_multi_value_columns(
                engine, collection, field_names=["nope"]
            )
    finally:
        await app_client.delete(
            f"/api/collections/{created['id']}", headers={"Authorization": admin_token}
        )


async def test_buma_wallet_manager_rules_match_pocketbase(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    """The two buma_wallet rule shapes that read a maxSelect-null relation.

    Observed on PocketBase 0.22.34: ``managers ~ @request.auth.id`` is a
    substring LIKE on the JSON text (a joined two-id orphan element matches
    both ids); ``@request.auth.id ?= managers`` compares the id with the JSON
    text and therefore never matches (view returns 404).
    """
    suffix = uuid.uuid4().hex[:8]
    auth = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            **_PUBLIC_RULES,
            "name": f"rel_people_{suffix}",
            "type": "auth",
            "schema": [{"name": "name", "type": "text"}],
        },
    )
    assert auth.status_code == 200, auth.text
    auth_name = auth.json()["name"]
    wallets = None
    try:
        users = {}
        for label in ("u1", "u2", "u3"):
            response = await app_client.post(
                f"/api/collections/{auth_name}/records",
                json={
                    "email": f"{label}-{suffix}@example.invalid",
                    "password": "ws-a-Password-123",
                    "passwordConfirm": "ws-a-Password-123",
                },
            )
            assert response.status_code == 200, response.text
            users[label] = response.json()["id"]
        wallets = await _post_collection(
            app_client,
            admin_token,
            {
                "name": f"rel_wallets_{suffix}",
                "schema": [
                    {"name": "name", "type": "text"},
                    _relation("managers", auth.json()["id"], None),
                ],
                "listRule": "@request.auth.id != '' && (managers ~ @request.auth.id  )",
                "viewRule": "@request.auth.id != '' && (@request.auth.id ?= managers)",
                "createRule": None,
                "updateRule": None,
                "deleteRule": None,
            },
        )
        ids = {}
        for name, managers in (
            ("w1", ["u1"]),
            ("w2", ["u2", "u1"]),
            ("w3", []),
            ("w4", ["u3"]),
        ):
            response = await app_client.post(
                f"/api/collections/{wallets['name']}/records",
                headers={"Authorization": admin_token},
                json={"name": name, "managers": [users[m] for m in managers]},
            )
            assert response.status_code == 200, response.text
            ids[name] = response.json()["id"]
        engine = get_engine()
        collection = await resolve_collection(engine, wallets["name"])
        await update_record(
            engine,
            collection,
            ids["w4"],
            {"managers": [f"{users['u1']},{users['u3']}"]},
        )

        expected_lists = {"u1": ["w1", "w2", "w4"], "u2": ["w2"], "u3": ["w4"]}
        for label, names in expected_lists.items():
            token = (
                await app_client.post(
                    f"/api/collections/{auth_name}/auth-with-password",
                    json={
                        "identity": f"{label}-{suffix}@example.invalid",
                        "password": "ws-a-Password-123",
                    },
                )
            ).json()["token"]
            listed = await app_client.get(
                f"/api/collections/{wallets['name']}/records",
                headers={"Authorization": token},
                params={"sort": "name"},
            )
            assert listed.status_code == 200, listed.text
            assert [item["name"] for item in listed.json()["items"]] == names, label
            for rid in ids.values():
                viewed = await app_client.get(
                    f"/api/collections/{wallets['name']}/records/{rid}",
                    headers={"Authorization": token},
                )
                assert viewed.status_code == 404, viewed.text
    finally:
        if wallets is not None:
            await app_client.delete(
                f"/api/collections/{wallets['id']}",
                headers={"Authorization": admin_token},
            )
        await app_client.delete(
            f"/api/collections/{auth.json()['id']}",
            headers={"Authorization": admin_token},
        )


async def test_hook_values_skip_the_relation_check_like_pocketbase(
    app_client: AsyncClient,
    relation_env: dict,
) -> None:
    """PocketBase validates the submitted form before request hooks run.

    A value written by an ``OnRecordBeforeCreateRequest`` hook (BUMA sets
    ``author`` this way) is saved without a target lookup, while an orphan
    sent by the client is refused before any hook runs.
    """
    owners = relation_env["owners"]["name"]
    orphan = "orphan" + relation_env["suffix"] + "0"
    app = app_client._transport.app
    previous_registry = getattr(app.state, "extension_registry", None)
    registry = previous_registry or ExtensionRegistry()
    app.state.extension_registry = registry
    hook = registry.hooks.get(HOOK_RECORD_CREATE_REQUEST)
    seen: list[dict] = []

    async def write_orphan(event):
        if getattr(event.collection, "name", "") == owners:
            seen.append(dict(event.data))
            event.data["rels"] = [orphan]
        return await event.next()

    hook_id = hook.bind_func(write_orphan)
    try:
        accepted = await app_client.post(
            f"/api/collections/{owners}/records",
            json={"title": "hooked", "rels": [relation_env["t1"]]},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["rels"] == [orphan]

        refused = await app_client.post(
            f"/api/collections/{owners}/records",
            json={"title": "client", "rels": [orphan]},
        )
        assert refused.status_code == 400, refused.text
        assert (
            refused.json()["data"]["rels"]["code"] == "validation_missing_rel_records"
        )
        assert len(seen) == 1  # the hook did not run for the refused request
    finally:
        hook.unbind(hook_id)
        app.state.extension_registry = previous_registry


async def test_access_errors_come_before_relation_validation(
    app_client: AsyncClient,
    admin_token: str,
    relation_env: dict,
) -> None:
    """403 for superuser-only rules and 404 for hidden records come first."""
    target_id = relation_env["targets"]["id"]
    suffix = relation_env["suffix"]
    orphan = "orphan" + suffix + "0"
    locked = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"rel_locked_{suffix}",
            "schema": [
                {"name": "title", "type": "text"},
                _relation("rels", target_id, None),
            ],
            "listRule": None,
            "viewRule": None,
            "createRule": None,
            "updateRule": "title = 'open'",
            "deleteRule": None,
        },
    )
    try:
        response = await app_client.post(
            f"/api/collections/{locked['name']}/records", json={"rels": [orphan]}
        )
        assert response.status_code == 403, response.text

        hidden = await app_client.post(
            f"/api/collections/{locked['name']}/records",
            headers={"Authorization": admin_token},
            json={"title": "closed"},
        )
        assert hidden.status_code == 200, hidden.text
        response = await app_client.patch(
            f"/api/collections/{locked['name']}/records/{hidden.json()['id']}",
            json={"rels": [orphan]},
        )
        assert response.status_code == 404, response.text

        opened = await app_client.patch(
            f"/api/collections/{locked['name']}/records/{hidden.json()['id']}",
            headers={"Authorization": admin_token},
            json={"title": "open"},
        )
        assert opened.status_code == 200, opened.text
        response = await app_client.patch(
            f"/api/collections/{locked['name']}/records/{hidden.json()['id']}",
            json={"rels": [orphan]},
        )
        assert response.status_code == 400, response.text
        assert (
            response.json()["data"]["rels"]["code"] == "validation_missing_rel_records"
        )
    finally:
        await app_client.delete(
            f"/api/collections/{locked['id']}", headers={"Authorization": admin_token}
        )


async def test_batch_writes_check_relation_targets(
    app_client: AsyncClient,
    admin_token: str,
    relation_env: dict,
) -> None:
    owners = relation_env["owners"]["name"]
    orphan = "orphan" + relation_env["suffix"] + "0"
    response = await app_client.post(
        "/api/batch",
        headers={"Authorization": admin_token},
        json={
            "requests": [
                {
                    "method": "POST",
                    "url": f"/api/collections/{owners}/records",
                    "body": {"title": "batch", "rels": [orphan]},
                }
            ]
        },
    )
    assert response.status_code == 400, response.text
    failure = response.json()["data"]["requests"]["0"]["response"]
    assert failure["data"]["rels"]["code"] == "validation_missing_rel_records"
