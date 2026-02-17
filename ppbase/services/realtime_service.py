"""SSE (Server-Sent Events) realtime service with PostgreSQL LISTEN/NOTIFY."""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


@dataclass
class RealtimeSubscription:
    """A single subscription to a topic."""

    topic: str
    auth_token: str | None = None


@dataclass
class RealtimeSession:
    """Client session for SSE realtime."""

    client_id: str
    response_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    subscriptions: list[RealtimeSubscription] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SubscriptionManager:
    """Manage SSE realtime client sessions and subscriptions."""

    def __init__(self):
        self.sessions: dict[str, RealtimeSession] = {}
        self._lock = asyncio.Lock()

    def register_client(self) -> str:
        """Register a new client and return client ID."""
        client_id = secrets.token_urlsafe(32)
        session = RealtimeSession(client_id=client_id)
        self.sessions[client_id] = session
        logger.info(f"Registered SSE client: {client_id}")
        return client_id

    async def add_subscription(
        self, client_id: str, topic: str, auth_token: str | None = None
    ) -> None:
        """Add a subscription to a client session."""
        async with self._lock:
            session = self.sessions.get(client_id)
            if not session:
                raise ValueError(f"Client not found: {client_id}")

            # Check if already subscribed
            for sub in session.subscriptions:
                if sub.topic == topic:
                    # Update auth token
                    sub.auth_token = auth_token
                    logger.debug(f"Updated subscription: {client_id} -> {topic}")
                    return

            # Add new subscription
            session.subscriptions.append(
                RealtimeSubscription(topic=topic, auth_token=auth_token)
            )
            logger.debug(f"Added subscription: {client_id} -> {topic}")

    async def remove_subscription(self, client_id: str, topic: str) -> None:
        """Remove a subscription from a client session."""
        async with self._lock:
            session = self.sessions.get(client_id)
            if not session:
                return

            session.subscriptions = [sub for sub in session.subscriptions if sub.topic != topic]
            logger.debug(f"Removed subscription: {client_id} -> {topic}")

    async def clear_subscriptions(self, client_id: str) -> None:
        """Clear all subscriptions for a client."""
        async with self._lock:
            session = self.sessions.get(client_id)
            if not session:
                return

            session.subscriptions.clear()
            logger.debug(f"Cleared subscriptions for client: {client_id}")

    def get_clients_for_topic(self, topic: str) -> list[str]:
        """Get all client IDs subscribed to a topic."""
        client_ids = []
        for client_id, session in self.sessions.items():
            for sub in session.subscriptions:
                if sub.topic == topic:
                    client_ids.append(client_id)
                    break
        return client_ids

    async def disconnect_client(self, client_id: str) -> None:
        """Disconnect a client and cleanup session."""
        async with self._lock:
            if client_id in self.sessions:
                del self.sessions[client_id]
                logger.info(f"Disconnected SSE client: {client_id}")

    def get_session(self, client_id: str) -> RealtimeSession | None:
        """Get a client session by ID."""
        return self.sessions.get(client_id)


async def broadcast_record_change(
    subscription_manager: SubscriptionManager,
    collection_name: str,
    record_id: str,
    action: str,
    record_data: dict[str, Any] | None = None,
) -> None:
    """Broadcast a record change event to subscribed clients.

    Args:
        subscription_manager: Subscription manager instance
        collection_name: Collection name
        record_id: Record ID
        action: Action type ("create", "update", "delete")
        record_data: Record data (None for delete)
    """
    # Build event data
    event_data = {
        "action": action,
        "record": record_data if record_data else {"id": record_id},
    }

    # Determine topics to broadcast to
    topics = [
        f"{collection_name}/*",  # Collection-wide subscription
        f"{collection_name}/{record_id}",  # Single-record subscription
    ]

    # Broadcast to all subscribed clients
    # The SSE event name MUST match the subscription topic the client used.
    # PocketBase SDK listens for events named exactly like the subscription topic.
    # e.g. if subscribed to "posts/*", the SSE event: field must be "posts/*"
    for topic in topics:
        client_ids = subscription_manager.get_clients_for_topic(topic)
        for client_id in client_ids:
            session = subscription_manager.get_session(client_id)
            if session:
                try:
                    # Use the subscription topic as the SSE event name
                    await session.response_queue.put(
                        {
                            "topic": topic,  # subscription topic = SSE event name
                            "data": event_data,
                        }
                    )
                except Exception as e:
                    logger.error(f"Failed to queue event for client {client_id}: {e}")


async def listen_for_db_events(
    engine: AsyncEngine, subscription_manager: SubscriptionManager
) -> None:
    """Listen for PostgreSQL NOTIFY events and broadcast to SSE clients.

    Uses a direct asyncpg connection (not SQLAlchemy) to ensure proper
    async notification callbacks work correctly.

    This runs as a background task for the lifetime of the application.
    """
    import asyncpg

    logger.info("Starting PostgreSQL LISTEN task for realtime events")

    # Extract connection parameters from the SQLAlchemy engine URL
    # Use engine.url properties to avoid URL encoding issues
    url = engine.url
    conn_params = {
        "host": url.host or "localhost",
        "port": url.port or 5432,
        "user": url.username,
        "password": url.password,
        "database": url.database,
    }
    logger.debug(f"LISTEN connection params: host={conn_params['host']}, port={conn_params['port']}, db={conn_params['database']}")

    conn = None

    async def on_notification(connection, pid, channel, payload):
        """Handle PostgreSQL NOTIFY events."""
        try:
            event = json.loads(payload)
            collection_name = event.get("collection")
            record_id = event.get("record_id")
            action = event.get("action")

            if not collection_name or not record_id or not action:
                logger.warning(f"Invalid notification payload: {payload}")
                return

            logger.debug(
                f"Realtime notification: {action} on {collection_name}/{record_id}"
            )

            # Get record data (if not delete)
            record_data = None
            if action != "delete":
                from ppbase.services.record_service import get_record, resolve_collection

                try:
                    collection = await resolve_collection(engine, collection_name)
                    if collection:
                        record_data = await get_record(engine, collection, record_id)
                except Exception as e:
                    logger.error(f"Failed to fetch record {record_id}: {e}")
                    # Still broadcast with minimal data
                    record_data = {"id": record_id}

            # Broadcast to subscribed clients
            await broadcast_record_change(
                subscription_manager,
                collection_name,
                record_id,
                action,
                record_data,
            )

        except Exception as e:
            logger.error(f"Error processing notification: {e}", exc_info=True)

    try:
        # Create a dedicated direct asyncpg connection for LISTEN
        # This bypasses SQLAlchemy's sync wrapper which blocks async callbacks
        conn = await asyncpg.connect(**conn_params)

        # Register the notification callback
        await conn.add_listener("record_changes", on_notification)

        logger.info("PostgreSQL LISTEN active on channel: record_changes")

        # Keep the connection alive indefinitely
        while True:
            await asyncio.sleep(30)
            # Heartbeat query to prevent connection timeout
            try:
                await conn.fetchval("SELECT 1")
            except asyncpg.PostgresConnectionStatusError:
                logger.error("LISTEN connection lost, re-raising to restart task")
                raise

    except asyncio.CancelledError:
        logger.info("PostgreSQL LISTEN task cancelled")
        raise
    except Exception as e:
        logger.error(f"PostgreSQL LISTEN task failed: {e}", exc_info=True)
        raise
    finally:
        if conn and not conn.is_closed():
            try:
                await conn.remove_listener("record_changes", on_notification)
                await conn.close()
                logger.info("LISTEN connection closed")
            except Exception as e:
                logger.error(f"Error closing LISTEN connection: {e}")
