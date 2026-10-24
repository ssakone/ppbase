"""Only superusers may use ``@collection.``/``@request.`` in list filters.

PocketBase 0.22.34 ``checkForAdminOnlyRuleFields`` (apis/record_helpers.go
l.319-345), called first by the records list handler (apis/record_crud.go
l.50-53): a guest or an auth record gets 403 "Only admins can filter by
@collection." (or "@request.") when the ``filter`` or ``sort`` query
parameter contains it.  Verified with the probe ``buma_rules`` group
(``pdv_filter_rule``) against the official binary.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_PASSWORD = "ws-a3-User-Password-1"


@pytest_asyncio.fixture
async def env(
    app_client: AsyncClient, admin_token: str
) -> AsyncIterator[tuple[str, dict[str, str], dict[str, str]]]:
    admin = {"Authorization": admin_token}
    suffix = uuid.uuid4().hex[:6]
    created: list[str] = []
    people = f"ao_people_{suffix}"
    docs = f"ao_docs_{suffix}"
    try:
        for body in (
            {
                "name": people,
                "type": "auth",
                "schema": [{"name": "name", "type": "text"}],
                "listRule": "",
                "viewRule": "",
                "createRule": "",
            },
            {
                "name": docs,
                "type": "base",
                "schema": [{"name": "title", "type": "text"}],
                "listRule": "",
            },
        ):
            response = await app_client.post(
                "/api/collections", headers=admin, json=body
            )
            assert response.status_code == 200, response.text
            created.append(response.json()["id"])
        email = f"ao-{suffix}@example.invalid"
        response = await app_client.post(
            f"/api/collections/{people}/records",
            json={"email": email, "password": _PASSWORD, "passwordConfirm": _PASSWORD},
        )
        assert response.status_code == 200, response.text
        response = await app_client.post(
            f"/api/collections/{people}/auth-with-password",
            json={"identity": email, "password": _PASSWORD},
        )
        assert response.status_code == 200, response.text
        user = {"Authorization": response.json()["token"]}
        response = await app_client.post(
            f"/api/collections/{docs}/records", json={"title": "d1"}, headers=admin
        )
        assert response.status_code == 200, response.text
        yield docs, admin, user
    finally:
        for collection_id in reversed(created):
            await app_client.delete(f"/api/collections/{collection_id}", headers=admin)


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"filter": '@collection.users.id != ""'},
            "Only admins can filter by @collection.",
        ),
        ({"filter": '@request.auth.id != ""'}, "Only admins can filter by @request."),
        ({"sort": "@request.auth.id"}, "Only admins can filter by @request."),
        (
            {"filter": '@request.auth.id != "" && @collection.users.id != ""'},
            "Only admins can filter by @collection.",
        ),
    ],
)
async def test_guests_and_records_cannot_use_admin_only_fields(
    app_client: AsyncClient,
    env: tuple[str, dict[str, str], dict[str, str]],
    params: dict[str, str],
    message: str,
) -> None:
    docs, _admin, user = env
    for headers in ({}, user):
        response = await app_client.get(
            f"/api/collections/{docs}/records", params=params, headers=headers
        )
        assert response.status_code == 403, response.text
        assert response.json()["message"] == message
        assert response.json()["data"] == {}


async def test_superusers_and_plain_filters_are_allowed(
    app_client: AsyncClient,
    env: tuple[str, dict[str, str], dict[str, str]],
) -> None:
    docs, admin, user = env
    response = await app_client.get(
        f"/api/collections/{docs}/records",
        params={"filter": '@request.auth.id != ""'},
        headers=admin,
    )
    assert response.status_code == 200, response.text
    response = await app_client.get(
        f"/api/collections/{docs}/records",
        params={"filter": 'title = "d1"', "sort": "-created"},
        headers=user,
    )
    assert response.status_code == 200, response.text
    assert [item["title"] for item in response.json()["items"]] == ["d1"]
