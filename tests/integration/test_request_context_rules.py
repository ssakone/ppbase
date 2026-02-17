"""Integration tests for @request.context rule macro support."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_records_list_rule_allows_request_context_default(
    app_client: AsyncClient,
    admin_token: str,
):
    """Records API should evaluate @request.context as "default"."""
    coll_name = f"ctx_default_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [{"name": "title", "type": "text"}],
            "createRule": "",
            "listRule": '@request.context = "default"',
            "viewRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    create_record = await app_client.post(
        f"/api/collections/{coll_name}/records",
        json={"title": "visible-in-default-context"},
    )
    assert create_record.status_code == 200

    list_records = await app_client.get(f"/api/collections/{coll_name}/records")
    assert list_records.status_code == 200
    payload = list_records.json()
    assert payload["totalItems"] == 1
    assert payload["items"][0]["title"] == "visible-in-default-context"


@pytest.mark.asyncio
async def test_records_list_rule_filters_non_default_request_context(
    app_client: AsyncClient,
    admin_token: str,
):
    """Rules expecting realtime context should not match regular records list."""
    coll_name = f"ctx_rt_only_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [{"name": "title", "type": "text"}],
            "createRule": "",
            "listRule": '@request.context = "realtime"',
            "viewRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    create_record = await app_client.post(
        f"/api/collections/{coll_name}/records",
        json={"title": "hidden-in-default-context"},
    )
    assert create_record.status_code == 200

    list_records = await app_client.get(f"/api/collections/{coll_name}/records")
    assert list_records.status_code == 200
    payload = list_records.json()
    assert payload["totalItems"] == 0
    assert payload["items"] == []
