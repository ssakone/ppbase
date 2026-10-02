"""PocketBase parity: escaped quotes in filter strings, merged expands, ``""`` vs NULL.

- Escaped quotes: the JS SDK ``pb.filter("name ~ {:n}", {n: "N'Diaye"})`` produces
  ``name ~ 'N\\'Diaye'``; PocketBase (fexpr) unescapes ``\\'``, ``\\"``, ``\\\\``, ``\\n``,
  ``\\t``, ``\\r`` inside quoted text and keeps any other backslash as is.
- Expand: ``expand=a.b,a`` must keep ``expand.a.expand.b`` whatever the order (PocketBase merges
  the expansions of the same field).
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
