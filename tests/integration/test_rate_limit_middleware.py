from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _restore_rate_limit_settings(
    app_client: AsyncClient,
    admin_token: str,
    payload: dict,
) -> None:
    # The limiter can temporarily block restore calls, so retry a few times.
    for attempt in range(6):
        response = await app_client.patch(
            "/api/settings",
            headers={"Authorization": admin_token},
            json=payload,
        )
        if response.status_code == 200:
            return
        if response.status_code != 429:
            assert response.status_code == 200, response.text
        await asyncio.sleep(0.25)
    raise AssertionError("Failed to restore rate limit settings after retries.")


async def test_global_rate_limit_blocks_after_threshold(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    current = await app_client.get(
        "/api/settings",
        headers={"Authorization": admin_token},
    )
    assert current.status_code == 200, current.text
    previous = current.json()
    restore_payload = {
        "rateLimiting": previous.get("rateLimiting", {"enabled": False, "maxRequests": 1000, "window": 60}),
        "trustedProxy": previous.get("trustedProxy", {"headers": [], "useLeftmostIP": False}),
    }

    enable = await app_client.patch(
        "/api/settings",
        headers={"Authorization": admin_token},
        json={
            "rateLimiting": {"enabled": True, "maxRequests": 2, "window": 1},
            "trustedProxy": {"headers": [], "useLeftmostIP": False},
        },
    )
    assert enable.status_code == 200, enable.text

    try:
        first = await app_client.get(
            "/api/collections?page=1&perPage=1",
            headers={"Authorization": admin_token},
        )
        second = await app_client.get(
            "/api/collections?page=1&perPage=1",
            headers={"Authorization": admin_token},
        )
        blocked = await app_client.get(
            "/api/collections?page=1&perPage=1",
            headers={"Authorization": admin_token},
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

        assert blocked.status_code == 429, blocked.text
        payload = blocked.json()
        assert payload == {
            "status": 429,
            "message": "Too many requests.",
            "data": {},
        }
        assert blocked.headers.get("retry-after")
        assert blocked.headers.get("x-ratelimit-limit") == "2"
        assert blocked.headers.get("x-ratelimit-remaining") == "0"
    finally:
        await asyncio.sleep(1.1)
        await _restore_rate_limit_settings(app_client, admin_token, restore_payload)


async def test_rate_limit_uses_trusted_proxy_header_when_configured(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    current = await app_client.get(
        "/api/settings",
        headers={"Authorization": admin_token},
    )
    assert current.status_code == 200, current.text
    previous = current.json()
    restore_payload = {
        "rateLimiting": previous.get("rateLimiting", {"enabled": False, "maxRequests": 1000, "window": 60}),
        "trustedProxy": previous.get("trustedProxy", {"headers": [], "useLeftmostIP": False}),
    }

    enable = await app_client.patch(
        "/api/settings",
        headers={"Authorization": admin_token},
        json={
            "rateLimiting": {"enabled": True, "maxRequests": 1, "window": 1},
            "trustedProxy": {"headers": ["x-forwarded-for"], "useLeftmostIP": True},
        },
    )
    assert enable.status_code == 200, enable.text

    try:
        first = await app_client.get(
            "/api/collections?page=1&perPage=1",
            headers={
                "Authorization": admin_token,
                "X-Forwarded-For": "10.0.0.1",
            },
        )
        blocked = await app_client.get(
            "/api/collections?page=1&perPage=1",
            headers={
                "Authorization": admin_token,
                "X-Forwarded-For": "10.0.0.1",
            },
        )
        second_client = await app_client.get(
            "/api/collections?page=1&perPage=1",
            headers={
                "Authorization": admin_token,
                "X-Forwarded-For": "10.0.0.2",
            },
        )

        assert first.status_code == 200, first.text
        assert blocked.status_code == 429, blocked.text
        assert second_client.status_code == 200, second_client.text
    finally:
        await asyncio.sleep(1.1)
        await _restore_rate_limit_settings(app_client, admin_token, restore_payload)
