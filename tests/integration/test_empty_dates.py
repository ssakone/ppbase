"""PostgreSQL integration tests for empty date values.

PocketBase stores an empty date as '' and serves it as "" (v0.22.34
``tools/types/datetime.go`` l.47-58); PPBase keeps storing NULL and must
serve "" everywhere, filter and sort that NULL like the SQLite ''.  The
expected values are the PocketBase 0.22.34 results of
``tests/pocketbase_parity/empty_date_probe.py`` on the same synthetic data
(rows ``a`` to ``g`` below are the probe rows ``a_empty`` to ``g_orphan``).
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

MID = "2024-06-01 00:00:00.000Z"
_PUBLIC = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}


def _date(name: str) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "date",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {"min": "", "max": ""},
    }


def _text(name: str) -> dict[str, Any]:
    return {"name": name, "type": "text", "required": False}


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


async def _titles(
    client: AsyncClient,
    name: str,
    *,
    filter_expr: str | None = None,
    sort: str | None = None,
) -> str:
    params: dict[str, Any] = {"perPage": 200, "sort": sort or "title"}
    if filter_expr is not None:
        params["filter"] = filter_expr
    response = await client.get(f"/api/collections/{name}/records", params=params)
    assert response.status_code == 200, response.text
    return "".join(item["title"] for item in response.json()["items"])


@pytest.fixture(scope="module")
async def dates_env(app_client: AsyncClient, admin_token: str) -> Any:
    suffix = uuid.uuid4().hex[:8]
    targets = await _post_collection(
        app_client,
        admin_token,
        {"name": f"ed_targets_{suffix}", "schema": [_text("name"), _date("td")]},
    )
    dates = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"ed_dates_{suffix}",
            "schema": [
                _text("title"),
                _date("d"),
                _date("e"),
                {
                    "name": "other",
                    "type": "relation",
                    "required": False,
                    "options": {"collectionId": targets["id"], "maxSelect": 1},
                },
            ],
        },
    )
    tn, dn = targets["name"], dates["name"]
    t_empty = (await _create(app_client, tn, {"name": "t_empty", "td": ""}))["id"]
    t_full = (
        await _create(app_client, tn, {"name": "t_full", "td": "2024-03-01 10:00:00.000Z"})
    )["id"]
    rows = (
        ("a", {"d": "", "e": "", "other": t_empty}),
        ("b", {"d": None, "e": "2024-01-01 00:00:00.000Z", "other": t_full}),
        ("c", {"other": t_empty}),
        ("d", {"d": "2023-06-15 08:30:00.000Z", "e": "", "other": t_full}),
        ("e", {"d": MID, "e": "2023-01-01 00:00:00.000Z", "other": t_full}),
        (
            "f",
            {
                "d": "2025-01-01 12:00:00.123Z",
                "e": "2025-01-01 12:00:00.123Z",
                "other": t_empty,
            },
        ),
        ("g", {"d": ""}),
    )
    ids = {}
    for title, payload in rows:
        ids[title] = (await _create(app_client, dn, {"title": title, **payload}))["id"]
    view = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"ed_view_{suffix}",
            "type": "view",
            "schema": [_text("title"), _date("d"), _date("td")],
            "options": {
                "query": (
                    f"SELECT p.id, p.title, p.d, t.td FROM {dn} p "
                    f"LEFT JOIN {tn} t ON t.id = p.other"
                )
            },
        },
    )
    view_created = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"ed_view_created_{suffix}",
            "type": "view",
            "schema": [_text("title"), _date("d")],
            "options": {"query": f"SELECT p.id, p.created, p.title, p.d FROM {dn} p"},
        },
    )
    yield {
        "targets": tn,
        "dates": dn,
        "view": view["name"],
        "view_created": view_created["name"],
        "ids": ids,
    }
    for collection in (view_created, view, dates, targets):
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_writes_serve_empty_dates_as_empty_strings(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    writes = await _post_collection(
        app_client,
        admin_token,
        {"name": f"ed_writes_{suffix}", "schema": [_text("title"), _date("d")]},
    )
    name = writes["name"]
    try:
        for payload in ({"d": ""}, {"d": None}, {}, {"d": "   "}):
            record = await _create(app_client, name, {"title": "w", **payload})
            assert record["d"] == ""
        response = await app_client.post(
            f"/api/collections/{name}/records",
            files=[("title", (None, "mp")), ("d", (None, ""))],
        )
        assert response.status_code == 200, response.text
        assert response.json()["d"] == ""
        record = await _create(
            app_client, name, {"title": "upd", "d": "2024-02-03 04:05:06.789Z"}
        )
        assert record["d"] == "2024-02-03 04:05:06.789Z"
        url = f"/api/collections/{name}/records/{record['id']}"
        seen = []
        for payload in ({"d": ""}, {"d": "2024-05-06 07:08:09.000Z"}, {"d": None}, {}):
            response = await app_client.patch(url, json=payload)
            assert response.status_code == 200, response.text
            seen.append(response.json()["d"])
        assert seen == ["", "2024-05-06 07:08:09.000Z", "", ""]
        assert (await app_client.get(url)).json()["d"] == ""
        # The storage is unchanged: an empty date is still NULL.
        async with get_engine().connect() as conn:
            stored = (
                await conn.execute(
                    text(
                        'SELECT COUNT(*) FILTER (WHERE "d" IS NULL), COUNT(*) '
                        f'FROM "{name}"'
                    )
                )
            ).one()
        assert tuple(stored) == (6, 6)
    finally:
        await app_client.delete(
            f"/api/collections/{writes['id']}", headers={"Authorization": admin_token}
        )


async def test_reads_and_expand_serve_empty_dates(
    app_client: AsyncClient,
    dates_env: dict,
) -> None:
    response = await app_client.get(
        f"/api/collections/{dates_env['dates']}/records",
        params={"perPage": 200, "sort": "title", "expand": "other"},
    )
    assert response.status_code == 200, response.text
    values = {
        item["title"]: [
            item["d"],
            item["e"],
            (item.get("expand") or {}).get("other", {}).get("td"),
        ]
        for item in response.json()["items"]
    }
    assert values == {
        "a": ["", "", ""],
        "b": ["", "2024-01-01 00:00:00.000Z", "2024-03-01 10:00:00.000Z"],
        "c": ["", "", ""],
        "d": ["2023-06-15 08:30:00.000Z", "", "2024-03-01 10:00:00.000Z"],
        "e": [MID, "2023-01-01 00:00:00.000Z", "2024-03-01 10:00:00.000Z"],
        "f": ["2025-01-01 12:00:00.123Z", "2025-01-01 12:00:00.123Z", ""],
        "g": ["", "", None],
    }
    url = f"/api/collections/{dates_env['dates']}/records/{dates_env['ids']['a']}"
    body = (await app_client.get(url)).json()
    assert [body["d"], body["e"]] == ["", ""]
    assert (await app_client.get(url, params={"fields": "d"})).json() == {"d": ""}


# PocketBase 0.22.34 results (rows a..g) of the probe filters.
FILTERS = {
    'd = ""': "abcg",
    "d = ''": "abcg",
    "d = null": "abcg",
    'd != ""': "def",
    "d != null": "def",
    f'd = "{MID}"': "e",
    f'd != "{MID}"': "abcdfg",
    f'd < "{MID}"': "abcdg",
    f'd <= "{MID}"': "abcdeg",
    f'd > "{MID}"': "f",
    f'd >= "{MID}"': "ef",
    f'"{MID}" > d': "abcdg",
    f'"{MID}" < d': "f",
    'd < ""': "",
    'd <= ""': "abcg",
    'd > ""': "def",
    'd >= ""': "abcdefg",
    '"" < d': "def",
    '"" >= d': "abcg",
    "d < null": "",
    "d <= null": "",
    "d > null": "",
    "d >= null": "",
    "d < @now": "abcdefg",
    "d > @now": "",
    "d = e": "acfg",
    "d != e": "bde",
    "d < e": "b",
    "d <= e": "abcfg",
    "d > e": "de",
    "d >= e": "acdefg",
    'd ~ "2024"': "e",
    'd !~ "2024"': "abcdfg",
    'd ~ ""': "abcdefg",
    'd !~ ""': "",
    "d ~ null": "",
    "d !~ null": "",
    f'd ~ "{MID}"': "e",
    f'd >= "2023-01-01 00:00:00.000Z" && d <= "{MID}"': "de",
    'd = "" || d > "2024-12-31 00:00:00.000Z"': "abcfg",
    'other != "" && other.td = ""': "acf",
    'other != "" && other.td != ""': "bde",
    f'other != "" && other.td < "{MID}"': "abcdef",
    'other != "" && other.td > "2024-01-01 00:00:00.000Z"': "bde",
    'other != "" && other.td !~ "2024"': "acf",
}


@pytest.mark.parametrize(("expression", "expected"), list(FILTERS.items()))
async def test_filters_match_pocketbase(
    app_client: AsyncClient,
    dates_env: dict,
    expression: str,
    expected: str,
) -> None:
    assert await _titles(app_client, dates_env["dates"], filter_expr=expression) == (
        expected
    )


async def test_collection_reference_filters_treat_null_dates_as_empty(
    app_client: AsyncClient,
    dates_env: dict,
) -> None:
    targets = dates_env["targets"]
    dates = dates_env["dates"]
    for expression, expected in (
        (f'@collection.{targets}.td = "" && @collection.{targets}.id = other', "acf"),
        (f'@collection.{targets}.td < "{MID}" && @collection.{targets}.id = other', "abcdef"),
    ):
        assert await _titles(app_client, dates, filter_expr=expression) == expected


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("d,title", "abcgdef"),
        ("-d,title", "fedabcg"),
        ("-d,-title", "fedgcba"),
        ("e,-d,title", "dacgebf"),
    ],
)
async def test_sort_matches_pocketbase(
    app_client: AsyncClient,
    dates_env: dict,
    sort: str,
    expected: str,
) -> None:
    assert await _titles(app_client, dates_env["dates"], sort=sort) == expected


async def test_view_records_match_pocketbase(
    app_client: AsyncClient,
    dates_env: dict,
) -> None:
    view = dates_env["view"]
    response = await app_client.get(
        f"/api/collections/{view}/records", params={"perPage": 200, "sort": "title"}
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    # No created/updated column: PocketBase omits both keys for a view.
    assert sorted(items[0]) == ["collectionId", "collectionName", "d", "id", "td", "title"]
    assert {item["title"]: [item["d"], item["td"]] for item in items} == {
        "a": ["", ""],
        "b": ["", "2024-03-01 10:00:00.000Z"],
        "c": ["", ""],
        "d": ["2023-06-15 08:30:00.000Z", "2024-03-01 10:00:00.000Z"],
        "e": [MID, "2024-03-01 10:00:00.000Z"],
        "f": ["2025-01-01 12:00:00.123Z", ""],
        "g": ["", ""],
    }
    one = (await app_client.get(f"/api/collections/{view}/records/{items[0]['id']}")).json()
    assert "created" not in one and "updated" not in one
    created_items = (
        await app_client.get(
            f"/api/collections/{dates_env['view_created']}/records",
            params={"perPage": 200},
        )
    ).json()["items"]
    assert all(item["created"] and "updated" not in item for item in created_items)

    for expression, expected in (
        ('td = ""', "acfg"),
        ('td != ""', "bde"),
        ('td > "2024-01-01 00:00:00.000Z"', "bde"),
        ('d = ""', "abcg"),
        (f'd < "{MID}"', "abcdg"),
    ):
        assert await _titles(app_client, view, filter_expr=expression) == expected
    for sort, expected in (
        ("d,title", "abcgdef"),
        ("-d,title", "fedabcg"),
        ("-td,title", "bdeacfg"),
    ):
        assert await _titles(app_client, view, sort=sort) == expected


async def test_view_left_join_null_is_compared_as_an_empty_date(
    app_client: AsyncClient,
    dates_env: dict,
) -> None:
    """Documented divergence: a view NULL (row ``g``) is the empty date.

    PocketBase keeps the LEFT JOIN NULL apart from '' in SQLite: it excludes
    ``g`` from ``td < MID`` and ``td !~ "2024"`` ("abcdef", "acf") and sorts
    it first ("gacfbde").  PPBase cannot tell this NULL from an empty date.
    """
    view = dates_env["view"]
    assert await _titles(app_client, view, filter_expr=f'td < "{MID}"') == "abcdefg"
    assert await _titles(app_client, view, filter_expr='td !~ "2024"') == "acfg"
    assert await _titles(app_client, view, sort="td,title") == "acfgbde"


async def test_auth_records_serve_empty_dates(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    people = await _post_collection(
        app_client,
        admin_token,
        {
            "name": f"ed_people_{suffix}",
            "type": "auth",
            "schema": [_text("title"), _date("birth")],
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
    email = f"ed{suffix}@example.invalid"
    try:
        created = await _create(
            app_client,
            name,
            {
                "username": f"ed{suffix}",
                "email": email,
                "password": password,
                "passwordConfirm": password,
                "birth": "",
            },
        )
        assert created["birth"] == ""
        response = await app_client.post(
            f"/api/collections/{name}/auth-with-password",
            json={"identity": email, "password": password},
        )
        assert response.status_code == 200, response.text
        assert response.json()["record"]["birth"] == ""
        token = {"Authorization": response.json()["token"]}
        response = await app_client.post(
            f"/api/collections/{name}/auth-refresh", headers=token
        )
        assert response.status_code == 200, response.text
        assert response.json()["record"]["birth"] == ""
        response = await app_client.patch(
            f"/api/collections/{name}/records/{created['id']}",
            headers=token,
            json={"birth": None},
        )
        assert response.status_code == 200, response.text
        assert response.json()["birth"] == ""
        response = await app_client.get(
            f"/api/collections/{name}/records",
            params={"filter": 'birth = ""'},
            headers={"Authorization": admin_token},
        )
        assert response.status_code == 200, response.text
        assert response.json()["totalItems"] == 1
    finally:
        await app_client.delete(
            f"/api/collections/{people['id']}", headers={"Authorization": admin_token}
        )


async def test_batch_and_realtime_serve_empty_dates(
    app_client: AsyncClient,
    admin_token: str,
    dates_env: dict,
) -> None:
    dates = dates_env["dates"]
    response = await app_client.post(
        "/api/batch",
        headers={"Authorization": admin_token},
        json={
            "requests": [
                {
                    "method": "POST",
                    "url": f"/api/collections/{dates}/records",
                    "body": {"title": "batch", "d": ""},
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    created = response.json()[0]["body"]
    assert [created["d"], created["e"]] == ["", ""]
    try:
        engine = get_engine()
        collection = await resolve_collection(engine, dates)
        manager = SubscriptionManager()
        client_id = manager.register_client()
        await manager.add_subscription(
            client_id, f"{dates}/*", auth_payload={"id": "admin", "type": "admin"}
        )
        await broadcast_record_change(
            manager,
            dates,
            created["id"],
            "update",
            await get_record(engine, collection, created["id"]),
            engine=engine,
            collection=collection,
        )
        session = manager.get_session(client_id)
        assert session is not None and not session.response_queue.empty()
        record = session.response_queue.get_nowait()["data"]["record"]
        assert [record["d"], record["e"]] == ["", ""]
    finally:
        await app_client.delete(
            f"/api/collections/{dates}/records/{created['id']}",
            headers={"Authorization": admin_token},
        )
