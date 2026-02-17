"""Integration tests for SSE Realtime functionality.

Tests the realtime service, subscription manager, and PostgreSQL NOTIFY integration.
"""
import asyncio
import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.db.system_tables import CollectionRecord
from ppbase.services.realtime_service import (
    SubscriptionManager,
    broadcast_record_change,
)


@pytest.mark.asyncio
async def test_subscription_manager_register_client():
    """Test client registration."""
    manager = SubscriptionManager()

    client_id = manager.register_client()

    assert client_id is not None
    assert isinstance(client_id, str)
    assert len(client_id) > 0
    assert client_id in manager.sessions


@pytest.mark.asyncio
async def test_subscription_manager_add_subscription():
    """Test adding subscriptions."""
    manager = SubscriptionManager()
    client_id = manager.register_client()

    # Add subscription
    await manager.add_subscription(client_id, "posts/*")

    session = manager.get_session(client_id)
    assert session is not None
    assert len(session.subscriptions) == 1
    assert session.subscriptions[0].topic == "posts/*"


@pytest.mark.asyncio
async def test_subscription_manager_remove_subscription():
    """Test removing subscriptions."""
    manager = SubscriptionManager()
    client_id = manager.register_client()

    # Add then remove
    await manager.add_subscription(client_id, "posts/*")
    await manager.remove_subscription(client_id, "posts/*")

    session = manager.get_session(client_id)
    assert len(session.subscriptions) == 0


@pytest.mark.asyncio
async def test_subscription_manager_clear_subscriptions():
    """Test clearing all subscriptions."""
    manager = SubscriptionManager()
    client_id = manager.register_client()

    # Add multiple subscriptions
    await manager.add_subscription(client_id, "posts/*")
    await manager.add_subscription(client_id, "comments/*")
    await manager.clear_subscriptions(client_id)

    session = manager.get_session(client_id)
    assert len(session.subscriptions) == 0


@pytest.mark.asyncio
async def test_subscription_manager_get_clients_for_topic():
    """Test getting clients for a topic."""
    manager = SubscriptionManager()
    client1 = manager.register_client()
    client2 = manager.register_client()
    client3 = manager.register_client()

    # Subscribe clients to different topics
    await manager.add_subscription(client1, "posts/*")
    await manager.add_subscription(client2, "posts/*")
    await manager.add_subscription(client3, "comments/*")

    # Get clients for posts
    posts_clients = manager.get_clients_for_topic("posts/*")
    assert len(posts_clients) == 2
    assert client1 in posts_clients
    assert client2 in posts_clients
    assert client3 not in posts_clients

    # Get clients for comments
    comments_clients = manager.get_clients_for_topic("comments/*")
    assert len(comments_clients) == 1
    assert client3 in comments_clients


@pytest.mark.asyncio
async def test_subscription_manager_disconnect_client():
    """Test disconnecting a client."""
    manager = SubscriptionManager()
    client_id = manager.register_client()

    await manager.disconnect_client(client_id)

    assert client_id not in manager.sessions
    assert manager.get_session(client_id) is None


@pytest.mark.asyncio
async def test_broadcast_record_change():
    """Test broadcasting record changes to subscribed clients."""
    manager = SubscriptionManager()
    client_id = manager.register_client()

    # Subscribe to posts/*
    await manager.add_subscription(client_id, "posts/*")

    # Broadcast a create event
    await broadcast_record_change(
        manager,
        "posts",
        "abc123",
        "create",
        {"id": "abc123", "title": "Test Post"},
    )

    # Check if event was queued
    session = manager.get_session(client_id)
    assert not session.response_queue.empty()

    # Get event from queue
    event = await asyncio.wait_for(session.response_queue.get(), timeout=1.0)
    # Topic is now the subscription topic (not the specific record path)
    # This matches PocketBase SDK protocol where event: field = subscription topic
    assert event["topic"] == "posts/*"
    assert event["data"]["action"] == "create"
    assert event["data"]["record"]["id"] == "abc123"
    assert event["data"]["record"]["title"] == "Test Post"


@pytest.mark.asyncio
async def test_broadcast_to_single_record_subscription():
    """Test broadcasting to single-record subscriptions."""
    manager = SubscriptionManager()
    client_id = manager.register_client()

    # Subscribe to specific record
    await manager.add_subscription(client_id, "posts/abc123")

    # Broadcast update for that record
    await broadcast_record_change(
        manager, "posts", "abc123", "update", {"id": "abc123", "title": "Updated"}
    )

    # Should receive event
    session = manager.get_session(client_id)
    assert not session.response_queue.empty()

    # Broadcast update for different record
    await broadcast_record_change(
        manager, "posts", "xyz789", "update", {"id": "xyz789", "title": "Other"}
    )

    # Should NOT receive event for different record (queue has only 1 event)
    assert session.response_queue.qsize() == 1


@pytest.mark.asyncio
async def test_broadcast_delete_event():
    """Test broadcasting delete events."""
    manager = SubscriptionManager()
    client_id = manager.register_client()

    await manager.add_subscription(client_id, "posts/*")

    # Broadcast delete (no record data)
    await broadcast_record_change(manager, "posts", "abc123", "delete", None)

    session = manager.get_session(client_id)
    event = await asyncio.wait_for(session.response_queue.get(), timeout=1.0)

    assert event["data"]["action"] == "delete"
    assert event["data"]["record"] == {"id": "abc123"}


@pytest.mark.asyncio
async def test_multiple_clients_receive_broadcast():
    """Test that multiple clients receive the same broadcast."""
    manager = SubscriptionManager()
    client1 = manager.register_client()
    client2 = manager.register_client()

    await manager.add_subscription(client1, "posts/*")
    await manager.add_subscription(client2, "posts/*")

    # Broadcast event
    await broadcast_record_change(
        manager, "posts", "abc123", "create", {"id": "abc123", "title": "Shared"}
    )

    # Both clients should receive
    session1 = manager.get_session(client1)
    session2 = manager.get_session(client2)

    assert not session1.response_queue.empty()
    assert not session2.response_queue.empty()

    event1 = await asyncio.wait_for(session1.response_queue.get(), timeout=1.0)
    event2 = await asyncio.wait_for(session2.response_queue.get(), timeout=1.0)

    assert event1["data"]["record"]["title"] == "Shared"
    assert event2["data"]["record"]["title"] == "Shared"


@pytest.mark.asyncio
async def test_subscription_with_auth_token():
    """Test subscriptions with auth tokens."""
    manager = SubscriptionManager()
    client_id = manager.register_client()

    # Add subscription with auth token
    await manager.add_subscription(client_id, "private_posts/*", "test_token_123")

    session = manager.get_session(client_id)
    assert session.subscriptions[0].auth_token == "test_token_123"


@pytest.mark.asyncio
async def test_update_existing_subscription():
    """Test updating an existing subscription (e.g., change auth token)."""
    manager = SubscriptionManager()
    client_id = manager.register_client()

    # Add subscription
    await manager.add_subscription(client_id, "posts/*", "token1")

    # Update same subscription with new token
    await manager.add_subscription(client_id, "posts/*", "token2")

    session = manager.get_session(client_id)
    # Should still have only 1 subscription
    assert len(session.subscriptions) == 1
    # Token should be updated
    assert session.subscriptions[0].auth_token == "token2"
