from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


async def _create_collection(
    app_client: AsyncClient,
    admin_token: str,
    *,
    name: str,
    schema: list[dict],
) -> dict:
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": name,
            "type": "base",
            "schema": schema,
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_records_filter_supports_nested_relation_traversal(
    app_client: AsyncClient,
    admin_token: str,
):
    suffix = uuid.uuid4().hex[:8]
    region_name = f"regions_{suffix}"
    country_name = f"countries_{suffix}"
    company_name = f"companies_{suffix}"
    employee_name = f"employees_{suffix}"

    regions = await _create_collection(
        app_client,
        admin_token,
        name=region_name,
        schema=[{"name": "name", "type": "text", "required": True}],
    )
    countries = await _create_collection(
        app_client,
        admin_token,
        name=country_name,
        schema=[
            {"name": "name", "type": "text", "required": True},
            {
                "name": "region",
                "type": "relation",
                "required": False,
                "options": {"collectionId": regions["id"], "maxSelect": 1},
            },
        ],
    )
    companies = await _create_collection(
        app_client,
        admin_token,
        name=company_name,
        schema=[
            {"name": "name", "type": "text", "required": True},
            {
                "name": "country",
                "type": "relation",
                "required": False,
                "options": {"collectionId": countries["id"], "maxSelect": 1},
            },
        ],
    )
    employees = await _create_collection(
        app_client,
        admin_token,
        name=employee_name,
        schema=[
            {"name": "name", "type": "text", "required": True},
            {
                "name": "company",
                "type": "relation",
                "required": False,
                "options": {"collectionId": companies["id"], "maxSelect": 1},
            },
        ],
    )

    west_region = await app_client.post(
        f"/api/collections/{region_name}/records",
        json={"name": "West Africa"},
    )
    assert west_region.status_code == 200, west_region.text
    east_region = await app_client.post(
        f"/api/collections/{region_name}/records",
        json={"name": "East Africa"},
    )
    assert east_region.status_code == 200, east_region.text

    mali = await app_client.post(
        f"/api/collections/{country_name}/records",
        json={"name": "Mali", "region": west_region.json()["id"]},
    )
    assert mali.status_code == 200, mali.text
    kenya = await app_client.post(
        f"/api/collections/{country_name}/records",
        json={"name": "Kenya", "region": east_region.json()["id"]},
    )
    assert kenya.status_code == 200, kenya.text

    ppbase = await app_client.post(
        f"/api/collections/{company_name}/records",
        json={"name": "PPBase", "country": mali.json()["id"]},
    )
    assert ppbase.status_code == 200, ppbase.text
    other = await app_client.post(
        f"/api/collections/{company_name}/records",
        json={"name": "OtherCo", "country": kenya.json()["id"]},
    )
    assert other.status_code == 200, other.text

    employee_match = await app_client.post(
        f"/api/collections/{employee_name}/records",
        json={"name": "Alice", "company": ppbase.json()["id"]},
    )
    assert employee_match.status_code == 200, employee_match.text
    employee_other = await app_client.post(
        f"/api/collections/{employee_name}/records",
        json={"name": "Bob", "company": other.json()["id"]},
    )
    assert employee_other.status_code == 200, employee_other.text

    response = await app_client.get(
        f"/api/collections/{employee_name}/records",
        params={"filter": "company.country.region.name ~ 'West Africa'"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["totalItems"] == 1
    assert [item["name"] for item in payload["items"]] == ["Alice"]
