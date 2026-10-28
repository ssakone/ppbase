"""PocketBase parity: escaped quotes in filter strings, merged expands, ``""`` vs NULL.

- Escaped quotes: the JS SDK ``pb.filter("name ~ {:n}", {n: "N'Diaye"})`` produces
  ``name ~ 'N\\'Diaye'``; PocketBase (fexpr) unescapes ``\\'``, ``\\"``, ``\\\\``, ``\\n``,
  ``\\t``, ``\\r`` inside quoted text and keeps any other backslash as is.
- Expand: ``expand=a.b,a`` must keep ``expand.a.expand.b`` whatever the order (PocketBase merges
  the expansions of the same field).
- Empty string: PocketBase compares ``x = ""`` as ``(x = '' OR x IS NULL)`` and ``x != ""`` as
  ``(x IS NOT '' AND x IS NOT NULL)`` (``null`` behaves like ``""``), so NULL columns of views
  built with ``LEFT JOIN`` match ``= ""``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


async def _collection(client: AsyncClient, token: str, name: str, schema: list[dict], **extra) -> dict:
    payload = {
        "name": name,
        "type": "base",
        "schema": schema,
        "listRule": "",
        "viewRule": "",
        "createRule": "",
        "updateRule": "",
        "deleteRule": "",
        **extra,
    }
    response = await client.post("/api/collections", headers={"Authorization": token}, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def _record(client: AsyncClient, token: str, collection: str, data: dict) -> dict:
    response = await client.post(
        f"/api/collections/{collection}/records", headers={"Authorization": token}, json=data
    )
    assert response.status_code == 200, response.text
    return response.json()


def _rel(name: str, collection_id: str) -> dict:
    return {"name": name, "type": "relation", "options": {"collectionId": collection_id, "maxSelect": 1}}


@pytest.mark.asyncio
async def test_filter_strings_accept_escaped_quotes(app_client: AsyncClient, admin_token: str) -> None:
    s = uuid.uuid4().hex[:8]
    teams = await _collection(app_client, admin_token, f"teams_{s}", [{"name": "name", "type": "text"}])
    people = f"people_{s}"
    await _collection(
        app_client, admin_token, people, [{"name": "username", "type": "text"}, _rel("team", teams["id"])]
    )
    blue = await _record(app_client, admin_token, teams["name"], {"name": "Blue"})
    for username, team in (
        ("N'Diaye", ""),
        ('Say "hi"', ""),
        ("back\\slash", ""),
        ("line\nbreak", ""),
        ("plain", blue["id"]),
        ("other", ""),
    ):
        await _record(app_client, admin_token, people, {"username": username, "team": team})

    async def usernames(filter_expr: str) -> list[str]:
        response = await app_client.get(
            f"/api/collections/{people}/records", params={"filter": filter_expr, "sort": "username"}
        )
        assert response.status_code == 200, (filter_expr, response.text)
        return sorted(item["username"] for item in response.json()["items"])

    # Form produced by the JS SDK: pb.filter("username ~ {:n}", {n: "N'Diaye"}).
    assert await usernames(r"username ~ 'N\'Diaye'") == ["N'Diaye"]
    assert await usernames(r"username = 'N\'Diaye'") == ["N'Diaye"]
    assert await usernames(r'username = "Say \"hi\""') == ['Say "hi"']
    assert await usernames(r"username ~ 'Say \"hi'") == ['Say "hi"']
    assert await usernames("username = \"N'Diaye\"") == ["N'Diaye"]
    # Unknown escapes keep their backslash; \\ and \n are unescaped (fexpr).
    assert await usernames(r"username = 'back\slash'") == ["back\\slash"]
    assert await usernames(r"username = 'back\\slash'") == ["back\\slash"]
    assert await usernames(r"username = 'line\nbreak'") == ["line\nbreak"]
    # An escaped quote does not hide a relation path that follows it.
    assert await usernames(r"username = 'N\'Diaye' || team.name = 'Blue'") == ["N'Diaye", "plain"]
    # Unterminated string stays a 400.
    response = await app_client.get(
        f"/api/collections/{people}/records", params={"filter": r"username = 'N\'"}
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_expand_merges_nested_and_plain_paths_of_same_field(
    app_client: AsyncClient, admin_token: str
) -> None:
    s = uuid.uuid4().hex[:8]
    networks = await _collection(app_client, admin_token, f"networks_{s}", [{"name": "name", "type": "text"}])
    links = await _collection(
        app_client,
        admin_token,
        f"agent_networks_{s}",
        [{"name": "label", "type": "text"}, _rel("network", networks["id"]), _rel("backup", networks["id"])],
    )
    agents = f"agents_{s}"
    await _collection(
        app_client, admin_token, agents, [{"name": "name", "type": "text"}, _rel("agent_network", links["id"])]
    )
    main = await _record(app_client, admin_token, networks["name"], {"name": "main"})
    spare = await _record(app_client, admin_token, networks["name"], {"name": "spare"})
    link = await _record(
        app_client, admin_token, links["name"], {"label": "L1", "network": main["id"], "backup": spare["id"]}
    )
    agent = await _record(app_client, admin_token, agents, {"name": "A", "agent_network": link["id"]})

    async def expanded(expand: str) -> list[dict]:
        listed = await app_client.get(f"/api/collections/{agents}/records", params={"expand": expand})
        assert listed.status_code == 200, listed.text
        viewed = await app_client.get(f"/api/collections/{agents}/records/{agent['id']}", params={"expand": expand})
        assert viewed.status_code == 200, viewed.text
        return [listed.json()["items"][0]["expand"]["agent_network"], viewed.json()["expand"]["agent_network"]]

    for expand in ("agent_network.network,agent_network", "agent_network,agent_network.network"):
        for link_expanded in await expanded(expand):
            assert link_expanded["label"] == "L1", expand
            assert link_expanded["expand"]["network"]["name"] == "main", expand

    for link_expanded in await expanded("agent_network.network,agent_network.backup"):
        assert link_expanded["expand"]["network"]["name"] == "main"
        assert link_expanded["expand"]["backup"]["name"] == "spare"


@pytest.mark.asyncio
async def test_empty_string_comparison_matches_null(app_client: AsyncClient, admin_token: str) -> None:
    s = uuid.uuid4().hex[:8]
    people = f"cpeople_{s}"
    profiles = f"cprofiles_{s}"
    people_coll = await _collection(
        app_client,
        admin_token,
        people,
        [
            {"name": "name", "type": "text"},
            {"name": "nick", "type": "text"},
            {"name": "active", "type": "bool"},
            {"name": "score", "type": "number"},
            {"name": "born", "type": "date"},
            {"name": "tags", "type": "select", "options": {"values": ["x", "y"], "maxSelect": 2}},
        ],
    )
    await _collection(
        app_client,
        admin_token,
        profiles,
        [_rel("person", people_coll["id"]), {"name": "bio", "type": "text"}, {"name": "level", "type": "number"}],
    )
    rows = {
        "full": {"nick": "f", "active": True, "score": 3, "born": "2020-01-02 00:00:00.000Z", "tags": ["x"]},
        "blank": {"nick": "", "active": False, "score": 0, "born": "", "tags": []},
        "orphan": {"nick": "", "active": False, "score": 0, "born": "", "tags": ["y"]},
    }
    ids = {}
    for name, data in rows.items():
        ids[name] = (await _record(app_client, admin_token, people, {"name": name, **data}))["id"]
    await _record(app_client, admin_token, profiles, {"person": ids["full"], "bio": "hello", "level": 2})
    await _record(app_client, admin_token, profiles, {"person": ids["blank"], "bio": "", "level": 0})
    # "orphan" has no profile: bio and level are NULL in the LEFT JOIN view below.

    view = f"cview_{s}"
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": view,
            "type": "view",
            "schema": [
                {"name": "id", "type": "text"},
                {"name": "name", "type": "text"},
                {"name": "bio", "type": "text"},
                {"name": "level", "type": "number"},
            ],
            "options": {
                "query": (
                    f'SELECT p.id, p.name, pr.bio, pr.level FROM "{people}" p '
                    f'LEFT JOIN "{profiles}" pr ON pr.person = p.id'
                ),
            },
            "listRule": "",
            "viewRule": "",
        },
    )
    assert response.status_code == 200, response.text

    async def names(collection: str, filter_expr: str) -> list[str]:
        response = await app_client.get(
            f"/api/collections/{collection}/records", params={"filter": filter_expr, "perPage": 100}
        )
        assert response.status_code == 200, (filter_expr, response.text)
        return sorted(item["name"] for item in response.json()["items"])

    # View with LEFT JOIN: NULL counts as empty.
    assert await names(view, 'bio = ""') == ["blank", "orphan"]
    assert await names(view, "bio = ''") == ["blank", "orphan"]
    assert await names(view, '"" = bio') == ["blank", "orphan"]
    assert await names(view, "bio = null") == ["blank", "orphan"]
    assert await names(view, 'bio != ""') == ["full"]
    assert await names(view, "bio != null") == ["full"]
    assert await names(view, 'bio = "hello"') == ["full"]
    assert await names(view, 'level = ""') == ["orphan"]
    assert await names(view, 'level != ""') == ["blank", "full"]
    assert await names(view, "level = 0") == ["blank"]
    assert await names(view, 'bio = "" && level = 0') == ["blank"]

    # Base collection: empty text, booleans, numbers, dates and multi-values are not broken.
    assert await names(people, 'nick = ""') == ["blank", "orphan"]
    assert await names(people, 'nick != ""') == ["full"]
    assert await names(people, "nick = null") == ["blank", "orphan"]
    assert await names(people, "active = false") == ["blank", "orphan"]
    assert await names(people, "active = true") == ["full"]
    assert await names(people, "active != false") == ["full"]
    assert await names(people, "score = 0") == ["blank", "orphan"]
    assert await names(people, "score != 0") == ["full"]
    assert await names(people, 'score = ""') == []
    assert await names(people, 'born = ""') == ["blank", "orphan"]
    assert await names(people, 'born != ""') == ["full"]
    assert await names(people, "born = null") == ["blank", "orphan"]
    assert await names(people, 'born > "2019-01-01 00:00:00.000Z"') == ["full"]
    assert await names(people, 'tags ?= "x"') == ["full"]
    assert await names(people, 'tags ?= "y"') == ["orphan"]
    assert await names(people, 'tags ?!= "x"') == ["orphan"]
    assert await names(people, 'name = "full" && nick != ""') == ["full"]
    assert await names(people, '"" = ""') == ["blank", "full", "orphan"]
    assert await names(people, '"" != null') == []
