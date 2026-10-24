"""Realtime subscription identity follows PocketBase, not the token type.

PocketBase compares the authenticated model of the previous and the current
subscription request (v0.22.34 apis/realtime.go:187-191 with
extractAuthIdFromGetter :539-551; v0.40.6 apis/realtime.go:218-222 with
isSameAuth :843-853), allows guest -> auth upgrades, stores the auth state on
every accepted request (v0.22.34 :201-203, v0.40.6 :230-231) and unsets it when
the auth record or admin is deleted (v0.22.34 :241-253 and :269-280).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from ppbase.api import realtime as realtime_api
from ppbase.ext.registry import HOOK_REALTIME_SUBSCRIBE_REQUEST, ExtensionRegistry
from ppbase.services.realtime_service import (
    SubscriptionManager,
    broadcast_record_change,
)

USERS_ID = "pbc_users_000001"
AGENTS_ID = "pbc_agents_00001"
SUPERUSERS_ID = "pbc_3142635823"

# Payloads as returned by ppbase.api.deps.get_optional_auth for each token.
AUTH_PAYLOADS: dict[str, dict[str, Any]] = {
    # PocketBase 0.22.34 session token imported with its secret (tokens/record.go:13-27).
    "legacy_user": {
        "id": "user00000000001",
        "type": "authRecord",
        "collectionId": USERS_ID,
        "collectionName": "users",
    },
    # The same session after a PPBase auth-refresh (services/auth_service.py).
    "refreshed_user": {
        "id": "user00000000001",
        "type": "auth",
        "collectionId": USERS_ID,
        "collectionName": "users",
        "refreshable": True,
    },
    "other_user": {
        "id": "user00000000002",
        "type": "auth",
        "collectionId": USERS_ID,
        "collectionName": "users",
    },
    # Same record id in another auth collection: another identity (v0.40.6 isSameAuth).
    "same_id_agent": {
        "id": "user00000000001",
        "type": "auth",
        "collectionId": AGENTS_ID,
        "collectionName": "agent_users",
    },
    # Legacy admin token: get_optional_auth sets the _superusers collectionId.
    "legacy_admin": {
        "id": "admin0000000001",
        "type": "admin",
        "collectionId": SUPERUSERS_ID,
        "collectionName": "_superusers",
    },
    "superuser_record": {
        "id": "admin0000000001",
        "type": "auth",
        "collectionId": SUPERUSERS_ID,
        "collectionName": "_superusers",
    },
}


def _fake_auth(request: Request) -> dict[str, Any] | None:
    key = request.headers.get("Authorization", "")
    payload = AUTH_PAYLOADS.get(key)
    return dict(payload) if payload is not None else None


@pytest.fixture
def realtime_app(monkeypatch):
    app = FastAPI()
    app.include_router(realtime_api.router)
    extensions = ExtensionRegistry()
    manager = SubscriptionManager(extension_registry=extensions)
    app.state.extension_registry = extensions
    app.state.subscription_manager = manager
    app.dependency_overrides[realtime_api.get_optional_auth] = _fake_auth

    async def _fake_resolve_collection(_engine, collection_name):
        return SimpleNamespace(id=f"{collection_name}_id", name=collection_name)

    monkeypatch.setattr(realtime_api, "get_engine", lambda: object())
    monkeypatch.setattr(realtime_api, "resolve_collection", _fake_resolve_collection)
    return app, manager, extensions


async def _post(client: AsyncClient, client_id: str, topics: list[str], token: str | None):
    headers = {"Authorization": token} if token else {}
    return await client.post(
        "/api/realtime",
        json={"clientId": client_id, "subscriptions": topics},
        headers=headers,
    )


def _topics(manager: SubscriptionManager, client_id: str) -> list[str]:
    session = manager.get_session(client_id)
    assert session is not None
    return [subscription.topic for subscription in session.subscriptions]


@pytest.mark.asyncio
async def test_refreshed_legacy_token_keeps_its_realtime_identity(realtime_app) -> None:
    """authRecord token, then the auth token PPBase issued on refresh: same record."""
    app, manager, _extensions = realtime_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client_id = manager.register_client()
        first = await _post(client, client_id, ["buma_supplier/rec1"], "legacy_user")
        assert first.status_code == 204, first.text
        # JS SDK 0.21: a new topic re-sends every topic with the current token.
        new_topic = await _post(
            client, client_id, ["buma_supplier/rec1", "buma_supplier/*"], "refreshed_user"
        )
        assert new_topic.status_code == 204, new_topic.text
        assert _topics(manager, client_id) == ["buma_supplier/rec1", "buma_supplier/*"]
        # The new subscription carries the refreshed auth payload.
        session = manager.get_session(client_id)
        assert {sub.auth_payload["type"] for sub in session.subscriptions} == {"auth"}
        # unsubscribe() of one topic while another remains.
        unsubscribe = await _post(client, client_id, ["buma_supplier/*"], "refreshed_user")
        assert unsubscribe.status_code == 204, unsubscribe.text
        assert _topics(manager, client_id) == ["buma_supplier/*"]
        # And back to the legacy token, still the same record.
        again = await _post(client, client_id, ["buma_supplier/rec1"], "legacy_user")
        assert again.status_code == 204, again.text


@pytest.mark.asyncio
async def test_guest_then_authenticated_is_an_accepted_upgrade(realtime_app) -> None:
    app, manager, _extensions = realtime_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client_id = manager.register_client()
        guest = await _post(client, client_id, ["posts/*"], None)
        assert guest.status_code == 204, guest.text
        assert manager.get_session(client_id).auth_key is None
        guest_again = await _post(client, client_id, ["posts/*", "posts/a"], None)
        assert guest_again.status_code == 204, guest_again.text

        upgraded = await _post(client, client_id, ["posts/*", "notes/*"], "legacy_user")
        assert upgraded.status_code == 204, upgraded.text
        assert manager.get_session(client_id).auth_key == f"{USERS_ID}:user00000000001"
        assert _topics(manager, client_id) == ["posts/*", "notes/*"]

        # Authenticated -> guest stays forbidden, subscriptions untouched.
        downgraded = await _post(client, client_id, ["posts/*"], None)
        assert downgraded.status_code == 403
        assert downgraded.json()["message"] == (
            "The current and the previous request authorization don't match."
        )
        assert _topics(manager, client_id) == ["posts/*", "notes/*"]


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["other_user", "same_id_agent", None])
async def test_another_identity_is_refused(realtime_app, second: str | None) -> None:
    app, manager, _extensions = realtime_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client_id = manager.register_client()
        assert (await _post(client, client_id, ["posts/*"], "refreshed_user")).status_code == 204
        refused = await _post(client, client_id, ["posts/*", "notes/*"], second)
        assert refused.status_code == 403
        assert _topics(manager, client_id) == ["posts/*"]
        assert manager.get_session(client_id).auth_key == f"{USERS_ID}:user00000000001"


@pytest.mark.asyncio
async def test_admin_token_and_superuser_record_token_share_one_identity(realtime_app) -> None:
    app, manager, _extensions = realtime_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client_id = manager.register_client()
        assert (await _post(client, client_id, ["posts/*"], "legacy_admin")).status_code == 204
        both = await _post(client, client_id, ["posts/*", "notes/*"], "superuser_record")
        assert both.status_code == 204, both.text
        assert (await _post(client, client_id, ["posts/*"], "legacy_user")).status_code == 403


@pytest.mark.asyncio
async def test_identity_is_stored_only_when_the_subscription_is_accepted(realtime_app) -> None:
    """A hook that answers without calling next() leaves the auth state as it was."""
    app, manager, extensions = realtime_app

    async def _reject_notes(e):
        if any(topic.startswith("notes/") for topic in e.subscriptions):
            return JSONResponse({"status": 400, "message": "no notes", "data": {}}, 400)
        return await e.next()

    extensions.hooks.get(HOOK_REALTIME_SUBSCRIBE_REQUEST).bind_func(_reject_notes)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client_id = manager.register_client()
        rejected = await _post(client, client_id, ["notes/*"], "legacy_user")
        assert rejected.status_code == 400
        assert manager.get_session(client_id).auth_key is None
        # Still a guest: another account may subscribe on this client.
        accepted = await _post(client, client_id, ["posts/*"], "other_user")
        assert accepted.status_code == 204, accepted.text
        assert manager.get_session(client_id).auth_key == f"{USERS_ID}:user00000000002"


@pytest.mark.asyncio
async def test_deleted_auth_record_turns_its_clients_into_guests(realtime_app) -> None:
    app, manager, _extensions = realtime_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        deleted_client = manager.register_client()
        other_client = manager.register_client()
        assert (await _post(client, deleted_client, ["users/*"], "legacy_user")).status_code == 204
        assert (await _post(client, other_client, ["users/*"], "other_user")).status_code == 204

        users = SimpleNamespace(id=USERS_ID, name="users", type="auth")
        await broadcast_record_change(
            manager, "users", "user00000000001", "delete", None, collection=users
        )

        # The delete event was queued before the auth state was unset.
        event = manager.get_session(deleted_client).response_queue.get_nowait()
        assert event["data"]["action"] == "delete"
        session = manager.get_session(deleted_client)
        assert session.auth_key is None
        assert all(
            sub.auth_payload is None and sub.auth_token is None for sub in session.subscriptions
        )
        assert manager.get_session(other_client).auth_key == f"{USERS_ID}:user00000000002"

        # PocketBase accepts any authorization on the next request of that client.
        guest = await _post(client, deleted_client, ["users/*"], None)
        assert guest.status_code == 204, guest.text


@pytest.mark.asyncio
async def test_deleting_a_base_record_keeps_the_auth_state(realtime_app) -> None:
    app, manager, _extensions = realtime_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client_id = manager.register_client()
        assert (await _post(client, client_id, ["posts/*"], "legacy_user")).status_code == 204

        posts = SimpleNamespace(id=USERS_ID, name="posts", type="base")
        await broadcast_record_change(
            manager, "posts", "user00000000001", "delete", None, collection=posts
        )
        assert manager.get_session(client_id).auth_key == f"{USERS_ID}:user00000000001"
