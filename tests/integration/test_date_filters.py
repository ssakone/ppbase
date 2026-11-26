"""Date field filters: accepted literal forms and error handling."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _create_collection(app_client: AsyncClient, admin_token: str) -> dict:
    name = f"events_{uuid.uuid4().hex[:8]}"
    resp = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": name,
            "type": "base",
            "fields": [
                {"name": "title", "type": "text"},
                {"name": "due", "type": "date"},
                {"name": "views", "type": "number"},
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _ids(app_client: AsyncClient, name: str, filter_expr: str) -> set[str]:
    resp = await app_client.get(
        f"/api/collections/{name}/records",
        params={"filter": filter_expr, "perPage": 100},
    )
    assert resp.status_code == 200, f"{filter_expr!r}: {resp.text}"
    return {item["title"] for item in resp.json()["items"]}


async def test_date_filter_iso_forms(app_client: AsyncClient, admin_token: str) -> None:
    coll = await _create_collection(app_client, admin_token)
    name = coll["name"]
    try:
        for title, due in (
            ("october", "2026-10-15 12:00:00.000Z"),
            ("november-first", "2026-11-01T00:00:00Z"),
            ("november-late", "2026-11-20T08:30:00.250Z"),
            ("undated", ""),
        ):
            resp = await app_client.post(
                f"/api/collections/{name}/records",
                json={"title": title, "due": due, "views": 1},
            )
            assert resp.status_code == 200, resp.text

        from_november = {"november-first", "november-late"}

        # Simple (dot-free) filters on a date field, in each accepted form.
        assert await _ids(app_client, name, 'due >= "2026-11-01T00:00:00Z"') == from_november
        assert await _ids(app_client, name, 'due >= "2026-11-01T00:00:00.000Z"') == from_november
        assert await _ids(app_client, name, 'due >= "2026-11-01 00:00:00.000Z"') == from_november
        assert await _ids(app_client, name, 'due >= "2026-11-01T02:00:00+02:00"') == from_november
        assert await _ids(app_client, name, 'due >= "2026-10-31T19:00:00-0500"') == from_november
        assert await _ids(app_client, name, 'due > "2026-11-01 00:00:00"') == {"november-late"}
        assert await _ids(app_client, name, 'due >= "2026-11-01"') == from_november
        assert await _ids(app_client, name, 'due < "2026-11-01"') == {"october"}
        assert await _ids(app_client, name, 'due = "2026-11-01T00:00:00Z"') == {"november-first"}

        # Empty literal means "no date", as in PocketBase.
        assert await _ids(app_client, name, 'due = ""') == {"undated"}
        assert await _ids(app_client, name, 'due != ""') == {
            "october",
            "november-first",
            "november-late",
        }

        # Combined with other conditions and system date fields.
        assert await _ids(
            app_client,
            name,
            'views = 1 && due >= "2026-11-01T00:00:00Z" && created >= "2000-01-01"',
        ) == from_november

        # Invalid literals are request errors, never server errors.
        for bad in (
            'due >= "not-a-date"',
            'due >= "2026-13-45"',
            'due >= "2026-11-01T25:00:00Z"',
            'created > "yesterday"',
            'views > "abc"',
            "bogus.path:nope = 1",
        ):
            resp = await app_client.get(
                f"/api/collections/{name}/records",
                params={"filter": bad},
            )
            assert resp.status_code == 400, f"{bad!r}: {resp.status_code} {resp.text}"
            body = resp.json()
            assert body["status"] == 400
            assert body["message"]
    finally:
        await app_client.delete(
            f"/api/collections/{coll['id']}",
            headers={"Authorization": admin_token},
        )
