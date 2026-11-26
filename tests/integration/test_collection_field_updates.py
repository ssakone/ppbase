"""PATCH /api/collections/{id} with a ``fields`` list.

Existing fields are only removed when they are explicitly marked with
``"_toDelete": true``; omitting them is rejected so columns and their data are
never dropped by accident.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _field_names(app_client: AsyncClient, admin_token: str, coll_id: str) -> list[str]:
    resp = await app_client.get(
        f"/api/collections/{coll_id}",
        headers={"Authorization": admin_token},
    )
    assert resp.status_code == 200, resp.text
    return [f["name"] for f in resp.json()["fields"]]


async def _record(app_client: AsyncClient, name: str, record_id: str) -> dict:
    # The list endpoint re-prepares statements after DDL on the table.
    resp = await app_client.get(
        f"/api/collections/{name}/records",
        params={"filter": f'id = "{record_id}"'},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    return items[0]


async def test_patch_fields_keeps_existing_columns(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    headers = {"Authorization": admin_token}
    name = f"notes_{uuid.uuid4().hex[:8]}"
    created = await app_client.post(
        "/api/collections",
        headers=headers,
        json={
            "name": name,
            "type": "base",
            "fields": [
                {"name": "title", "type": "text"},
                {"name": "score", "type": "number"},
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
        },
    )
    assert created.status_code == 200, created.text
    coll_id = created.json()["id"]

    try:
        rec = await app_client.post(
            f"/api/collections/{name}/records",
            json={"title": "keep me", "score": 7},
        )
        assert rec.status_code == 200, rec.text
        record_id = rec.json()["id"]

        # 1. A partial list that omits existing fields is rejected.
        partial = await app_client.patch(
            f"/api/collections/{coll_id}",
            headers=headers,
            json={"fields": [{"name": "extra", "type": "text"}]},
        )
        assert partial.status_code == 400, partial.text
        message = partial.json()["message"]
        assert "title" in message and "score" in message
        assert await _field_names(app_client, admin_token, coll_id) == ["title", "score"]
        data = await _record(app_client, name, record_id)
        assert data["title"] == "keep me"
        assert data["score"] == 7

        # 2. Existing fields sent by name only (no id) keep their columns.
        by_name = await app_client.patch(
            f"/api/collections/{coll_id}",
            headers=headers,
            json={
                "fields": [
                    {"name": "title", "type": "text"},
                    {"name": "score", "type": "number"},
                    {"name": "extra", "type": "text"},
                ]
            },
        )
        assert by_name.status_code == 200, by_name.text
        fields = {f["name"]: f for f in by_name.json()["fields"]}
        assert set(fields) == {"title", "score", "extra"}
        data = await _record(app_client, name, record_id)
        assert data["title"] == "keep me"
        assert data["score"] == 7

        # 3. A field is removed only with an explicit marker.
        current = [f for f in by_name.json()["fields"] if f["name"] != "extra"]
        removed = await app_client.patch(
            f"/api/collections/{coll_id}",
            headers=headers,
            json={"fields": [*current, {"name": "extra", "type": "text", "_toDelete": True}]},
        )
        assert removed.status_code == 200, removed.text
        assert await _field_names(app_client, admin_token, coll_id) == ["title", "score"]
        for field in removed.json()["fields"]:
            assert "_toDelete" not in field
        data = await _record(app_client, name, record_id)
        assert "extra" not in data
        assert data["title"] == "keep me"

        # 4. Renaming a field without an id would drop and re-create its
        #    column; it is rejected unless the old field is explicitly removed.
        renamed_fields = [
            {**f, "name": "headline"} if f["name"] == "title" else f
            for f in removed.json()["fields"]
        ]
        renamed = await app_client.patch(
            f"/api/collections/{coll_id}",
            headers=headers,
            json={"fields": renamed_fields},
        )
        assert renamed.status_code == 400, renamed.text
        data = await _record(app_client, name, record_id)
        assert data["title"] == "keep me"
        assert data["score"] == 7

        # 4b. Fields that carry an id can be renamed in place, keeping data.
        with_id = await app_client.patch(
            f"/api/collections/{coll_id}",
            headers=headers,
            json={
                "fields": [
                    *removed.json()["fields"],
                    {"id": "fld_label01", "name": "label", "type": "text"},
                ]
            },
        )
        assert with_id.status_code == 200, with_id.text
        set_label = await app_client.patch(
            f"/api/collections/{name}/records/{record_id}",
            json={"label": "named"},
        )
        assert set_label.status_code == 200, set_label.text
        renamed = await app_client.patch(
            f"/api/collections/{coll_id}",
            headers=headers,
            json={
                "fields": [
                    {**f, "name": "tag"} if f.get("id") == "fld_label01" else f
                    for f in with_id.json()["fields"]
                ]
            },
        )
        assert renamed.status_code == 200, renamed.text
        data = await _record(app_client, name, record_id)
        assert data["tag"] == "named"
        assert data["title"] == "keep me"

        # 5. Non-schema updates are unaffected.
        rules = await app_client.patch(
            f"/api/collections/{coll_id}",
            headers=headers,
            json={"listRule": None},
        )
        assert rules.status_code == 200, rules.text
        assert await _field_names(app_client, admin_token, coll_id) == ["title", "score", "tag"]
    finally:
        await app_client.delete(f"/api/collections/{coll_id}", headers=headers)
