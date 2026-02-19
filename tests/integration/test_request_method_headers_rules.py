"""Integration tests for @request.method and @request.headers.* rule macros."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_records_list_rule_allows_matching_request_method(
    app_client: AsyncClient,
    admin_token: str,
):
    """Records list should pass when @request.method matches."""
    coll_name = f"method_rule_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [{"name": "title", "type": "text"}],
            "createRule": "",
            "listRule": '@request.method = "GET"',
            "viewRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    create_record = await app_client.post(
        f"/api/collections/{coll_name}/records",
        json={"title": "visible-for-get"},
    )
    assert create_record.status_code == 200

    list_records = await app_client.get(f"/api/collections/{coll_name}/records")
    assert list_records.status_code == 200
    payload = list_records.json()
    assert payload["totalItems"] == 1
    assert payload["items"][0]["title"] == "visible-for-get"


@pytest.mark.asyncio
async def test_records_list_rule_uses_request_headers_macro(
    app_client: AsyncClient,
    admin_token: str,
):
    """Rules should resolve @request.headers.* with hyphen/underscore compatibility."""
    coll_name = f"header_rule_{uuid.uuid4().hex[:8]}"

    create_coll = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": coll_name,
            "type": "base",
            "schema": [{"name": "title", "type": "text"}],
            "createRule": "",
            "listRule": '@request.headers.x_test = "pass"',
            "viewRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_coll.status_code == 200

    create_record = await app_client.post(
        f"/api/collections/{coll_name}/records",
        json={"title": "visible-for-header"},
    )
    assert create_record.status_code == 200

    # Should match because request header uses hyphen while macro uses underscore.
    allowed = await app_client.get(
        f"/api/collections/{coll_name}/records",
        headers={"X-Test": "pass"},
    )
    assert allowed.status_code == 200
    allowed_payload = allowed.json()
    assert allowed_payload["totalItems"] == 1

    denied = await app_client.get(f"/api/collections/{coll_name}/records")
    assert denied.status_code == 200
    denied_payload = denied.json()
    assert denied_payload["totalItems"] == 0
    assert denied_payload["items"] == []
