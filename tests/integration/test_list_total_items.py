"""totalItems counts distinct ids, like PocketBase (PostgreSQL).

PocketBase answers a records list with ``COUNT(DISTINCT [[<table>.id]])``
(v0.22.34 ``tools/search/provider.go`` l.236-256, v0.40.6 l.298-327) and
computes ``totalPages = ceil(totalItems / perPage)`` from it, while every
page still serves the rows of the query.  A base collection has unique ids
(nothing changes); a view may repeat an id.  On the real BUMA backup the
public ``buma_article_stockage_list_view`` serves 989 rows for 42 ids:
PocketBase answers ``totalItems`` 42.  Values paired with the 0.22.34 binary
by the ``totals`` group of ``tests/pocketbase_parity/empty_date_probe.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

PUBLIC = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}


def _text(name: str) -> dict:
    return {"name": name, "type": "text"}


async def _collection(app_client: AsyncClient, admin_token: str, body: dict) -> dict:
    payload = {**PUBLIC, "type": "base", **body}
    if payload["type"] == "view":
        payload.update(createRule=None, updateRule=None, deleteRule=None)
    response = await app_client.post(
        "/api/collections", headers={"Authorization": admin_token}, json=payload
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create(app_client: AsyncClient, name: str, payload: dict) -> str:
    response = await app_client.post(f"/api/collections/{name}/records", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _page(
    app_client: AsyncClient,
    name: str,
    headers: dict | None = None,
    **params: object,
) -> dict:
    response = await app_client.get(
        f"/api/collections/{name}/records", params=params, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {
        "totals": (
            body["page"],
            body["perPage"],
            body["totalItems"],
            body["totalPages"],
        ),
        "rows": [(item.get("name"), item.get("title")) for item in body["items"]],
    }


@pytest.fixture
async def dataset(app_client: AsyncClient, admin_token: str) -> AsyncIterator[dict]:
    suffix = uuid.uuid4().hex[:8]
    targets = await _collection(
        app_client,
        admin_token,
        {"name": f"lt_targets_{suffix}", "schema": [_text("name")]},
    )
    children = await _collection(
        app_client,
        admin_token,
        {
            "name": f"lt_children_{suffix}",
            "schema": [
                _text("title"),
                {
                    "name": "target",
                    "type": "relation",
                    "options": {"collectionId": targets["id"], "maxSelect": 1},
                },
            ],
        },
    )
    tn, cn = targets["name"], children["name"]
    t1 = await _create(app_client, tn, {"name": "t1"})
    t2 = await _create(app_client, tn, {"name": "t2"})
    for title, target in (
        ("a", t1),
        ("b", t2),
        ("c", t1),
        ("d", t2),
        ("e", t2),
        ("f", t1),
    ):
        await _create(app_client, cn, {"title": title, "target": target})
    join = f"FROM {tn} t JOIN {cn} c ON c.target = t.id"
    repeated = await _collection(
        app_client,
        admin_token,
        {
            "name": f"lt_repeated_{suffix}",
            "type": "view",
            "schema": [_text("name"), _text("title")],
            "options": {"query": f"SELECT t.id, t.name, c.title {join}"},
        },
    )
    identical = await _collection(
        app_client,
        admin_token,
        {
            "name": f"lt_identical_{suffix}",
            "type": "view",
            "schema": [_text("name")],
            "options": {"query": f"SELECT t.id, t.name {join}"},
        },
    )
    yield {
        "targets": tn,
        "children": cn,
        "repeated": repeated["name"],
        "identical": identical["name"],
    }
    for collection in (identical, repeated, children, targets):
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_base_collection_totals_count_its_records(
    app_client: AsyncClient, dataset: dict
) -> None:
    name = dataset["children"]
    pages = [
        await _page(app_client, name, perPage=4, page=page, sort="title")
        for page in (1, 2, 3)
    ]
    assert [page["totals"] for page in pages] == [
        (1, 4, 6, 2),
        (2, 4, 6, 2),
        (3, 4, 6, 2),
    ]
    assert [[title for _, title in page["rows"]] for page in pages] == [
        ["a", "b", "c", "d"],
        ["e", "f"],
        [],
    ]
    filtered = await _page(
        app_client, name, perPage=2, sort="title", filter='title > "b"'
    )
    assert filtered["totals"] == (1, 2, 4, 2)
    skipped = await _page(app_client, name, perPage=4, sort="title", skipTotal=1)
    assert skipped["totals"] == (1, 4, -1, -1)
    assert skipped["rows"] == pages[0]["rows"]


async def test_view_repeating_ids_counts_distinct_ids_and_serves_every_row(
    app_client: AsyncClient, dataset: dict
) -> None:
    name = dataset["repeated"]
    pages = [
        await _page(app_client, name, perPage=4, page=page, sort="title")
        for page in (1, 2, 3)
    ]
    # 6 rows, 2 ids: totalItems 2 and totalPages ceil(2 / 4) = 1, yet page 2
    # still serves the last two rows (PocketBase 0.22.34 does the same).
    assert [page["totals"] for page in pages] == [
        (1, 4, 2, 1),
        (2, 4, 2, 1),
        (3, 4, 2, 1),
    ]
    assert [page["rows"] for page in pages] == [
        [("t1", "a"), ("t2", "b"), ("t1", "c"), ("t2", "d")],
        [("t2", "e"), ("t1", "f")],
        [],
    ]
    one = await _page(app_client, name, perPage=1, sort="title")
    assert one["totals"] == (1, 1, 2, 2)
    assert one["rows"] == [("t1", "a")]
    filtered = await _page(
        app_client, name, perPage=2, sort="title", filter='name = "t1"'
    )
    assert filtered["totals"] == (1, 2, 1, 1)
    assert filtered["rows"] == [("t1", "a"), ("t1", "c")]
    skipped = await _page(app_client, name, perPage=4, sort="title", skipTotal="true")
    assert skipped["totals"] == (1, 4, -1, -1)
    assert skipped["rows"] == pages[0]["rows"]


async def test_identical_view_rows_under_a_join_filter_are_not_collapsed(
    app_client: AsyncClient, admin_token: str, dataset: dict
) -> None:
    """Pins an open divergence (docs/request-static-fields.md).

    A filter or rule that joins (``@collection``, relation path,
    ``@request.auth.<field>``) makes PocketBase select ``DISTINCT`` rows
    (v0.22.34 resolvers/record_field_resolver.go l.121-138): the 6 rows of a
    view that repeats identical rows become 2.  PPBase renders joins as
    ``EXISTS`` and keeps serving the 6 rows; totalItems is 2 in both.
    """
    name = dataset["identical"]
    plain = await _page(app_client, name, perPage=10, sort="name")
    assert plain["totals"] == (1, 10, 2, 1)
    assert [row[0] for row in plain["rows"]] == ["t1"] * 3 + ["t2"] * 3
    joined = await _page(
        app_client,
        name,
        {"Authorization": admin_token},
        perPage=10,
        sort="name",
        filter=f'@collection.{dataset["targets"]}.name ?= "t1"',
    )
    assert joined["totals"] == (1, 10, 2, 1)
    assert [row[0] for row in joined["rows"]] == ["t1"] * 3 + ["t2"] * 3
