"""Integration tests for datetime filter macros in API rules."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


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
