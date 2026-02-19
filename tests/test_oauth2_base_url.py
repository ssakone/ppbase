"""Unit tests for OAuth2 base URL normalization."""

from __future__ import annotations

import asyncio

import pytest

from ppbase.api.record_auth import (
    _dispatch_oauth2_realtime_payload,
    _normalize_oauth_base_url,
    _normalize_oauth_provider_auth_url,
)
from ppbase.services.realtime_service import SubscriptionManager


def test_normalize_oauth_base_url_keeps_regular_host():
    assert (
        _normalize_oauth_base_url("http://localhost:8090")
        == "http://localhost:8090"
    )


def test_normalize_oauth_base_url_rewrites_zero_host():
    assert (
        _normalize_oauth_base_url("http://0.0.0.0:8090")
        == "http://127.0.0.1:8090"
    )


def test_normalize_oauth_provider_auth_url_moves_redirect_uri_to_end():
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        "?client_id=test"
        "&redirect_uri="
        "&response_type=code"
        "&scope=openid+email"
    )
    normalized = _normalize_oauth_provider_auth_url(url)
    assert normalized.endswith("&redirect_uri=")
    assert "response_type=code" in normalized


@pytest.mark.asyncio
async def test_dispatch_oauth2_realtime_payload_pushes_at_oauth2_event():
    manager = SubscriptionManager()
    client_id = manager.register_client()
    await manager.add_subscription(client_id, "@oauth2")

    sent = await _dispatch_oauth2_realtime_payload(
        manager,
        {"state": client_id, "code": "test-code"},
    )
    assert sent is True

    session = manager.get_session(client_id)
    assert session is not None
    event = await asyncio.wait_for(session.response_queue.get(), timeout=1.0)
    assert event["topic"] == "@oauth2"
    assert event["data"]["code"] == "test-code"
