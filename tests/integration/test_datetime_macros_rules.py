"""Integration tests for datetime filter macros in API rules."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_records_list_accepts_pocketbase_datetime_string_filter(
    app_client: AsyncClient,
    admin_token: str,
):
    """PocketBase clients send datetime filters as strings."""
    coll_name = f"dt_string_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [{"name": "title", "type": "text"}],
            "createRule": "",
            "listRule": "",
            "viewRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    create_record = await app_client.post(
        f"/api/collections/{coll_name}/records",
        json={"title": "datetime-string-filter"},
    )
    assert create_record.status_code == 200

    list_records = await app_client.get(
        f"/api/collections/{coll_name}/records",
        params={
            "filter": (
                'created >= "1970-01-01 00:00:00" '
                '&& created <= "2999-12-31 23:59:59"'
            ),
        },
    )
    assert list_records.status_code == 200
    payload = list_records.json()
    assert payload["totalItems"] == 1
    assert payload["items"][0]["title"] == "datetime-string-filter"


@pytest.mark.asyncio
async def test_records_list_accepts_schema_datetime_string_filter(
    app_client: AsyncClient,
    admin_token: str,
):
    """Custom date fields should type string literals from collection metadata."""
    coll_name = f"dt_schema_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [
                {"name": "title", "type": "text"},
                {"name": "expires_at", "type": "date"},
            ],
            "createRule": "",
            "listRule": "",
            "viewRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    for title, expires_at in (
        ("expired", "2026-05-17T23:59:59Z"),
        ("active", "2026-05-19T00:00:00Z"),
    ):
        create_record = await app_client.post(
            f"/api/collections/{coll_name}/records",
            json={"title": title, "expires_at": expires_at},
        )
        assert create_record.status_code == 200

    list_records = await app_client.get(
        f"/api/collections/{coll_name}/records",
        params={"filter": 'expires_at >= "2026-05-18 00:00:00"'},
    )
    assert list_records.status_code == 200
    payload = list_records.json()
    assert payload["totalItems"] == 1
    assert payload["items"][0]["title"] == "active"


@pytest.mark.asyncio
async def test_record_view_rule_accepts_schema_datetime_string_filter(
    app_client: AsyncClient,
    admin_token: str,
):
    """Record rules should type custom date literals from collection metadata."""
    coll_name = f"dt_rule_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [
                {"name": "title", "type": "text"},
                {"name": "expires_at", "type": "date"},
            ],
            "createRule": "",
            "listRule": "",
            "viewRule": 'expires_at >= "2026-05-18 00:00:00"',
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    create_record = await app_client.post(
        f"/api/collections/{coll_name}/records",
        json={"title": "visible", "expires_at": "2026-05-19T00:00:00Z"},
    )
    assert create_record.status_code == 200
    record_id = create_record.json()["id"]

    view_record = await app_client.get(
        f"/api/collections/{coll_name}/records/{record_id}",
    )
    assert view_record.status_code == 200
    assert view_record.json()["title"] == "visible"


@pytest.mark.asyncio
async def test_records_list_rule_supports_datetime_boundary_macros(
    app_client: AsyncClient,
    admin_token: str,
):
    """Boundary datetime macros should be usable in listRule expressions."""
    coll_name = f"dt_bounds_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [{"name": "title", "type": "text"}],
            "createRule": "",
            "listRule": (
                "created >= @todayStart && created <= @todayEnd "
                "&& created >= @monthStart && created <= @monthEnd "
                "&& created >= @yearStart && created <= @yearEnd"
            ),
            "viewRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    create_record = await app_client.post(
        f"/api/collections/{coll_name}/records",
        json={"title": "datetime-bounds"},
    )
    assert create_record.status_code == 200

    list_records = await app_client.get(f"/api/collections/{coll_name}/records")
    assert list_records.status_code == 200
    payload = list_records.json()
    assert payload["totalItems"] == 1


@pytest.mark.asyncio
async def test_records_list_rule_supports_relative_datetime_macros(
    app_client: AsyncClient,
    admin_token: str,
):
    """Relative datetime macros should evaluate without parse/runtime errors."""
    coll_name = f"dt_relative_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [{"name": "title", "type": "text"}],
            "createRule": "",
            "listRule": "@yesterday < @now && @tomorrow > @now",
            "viewRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    create_record = await app_client.post(
        f"/api/collections/{coll_name}/records",
        json={"title": "datetime-relative"},
    )
    assert create_record.status_code == 200

    list_records = await app_client.get(f"/api/collections/{coll_name}/records")
    assert list_records.status_code == 200
    payload = list_records.json()
    assert payload["totalItems"] == 1


@pytest.mark.asyncio
async def test_records_list_rule_supports_datetime_component_macros(
    app_client: AsyncClient,
    admin_token: str,
):
    """Numeric datetime component macros should resolve in rules."""
    coll_name = f"dt_parts_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [{"name": "title", "type": "text"}],
            "createRule": "",
            "listRule": (
                "@second >= 0 && @second <= 59 "
                "&& @minute >= 0 && @minute <= 59 "
                "&& @hour >= 0 && @hour <= 23 "
                "&& @weekday >= 0 && @weekday <= 6 "
                "&& @day >= 1 && @day <= 31 "
                "&& @month >= 1 && @month <= 12 "
                "&& @year >= 2000"
            ),
            "viewRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    create_record = await app_client.post(
        f"/api/collections/{coll_name}/records",
        json={"title": "datetime-components"},
    )
    assert create_record.status_code == 200

    list_records = await app_client.get(f"/api/collections/{coll_name}/records")
    assert list_records.status_code == 200
    payload = list_records.json()
    assert payload["totalItems"] == 1
