"""SSE (Server-Sent Events) realtime service with PostgreSQL LISTEN/NOTIFY."""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.ext.events import RealtimeMessageSendEvent
from ppbase.ext.registry import HOOK_REALTIME_MESSAGE_SEND

logger = logging.getLogger(__name__)


@dataclass
class RealtimeSubscription:
    """A single subscription to a topic."""

    topic: str
    base_topic: str
    auth_token: str | None = None
    auth_payload: dict[str, Any] | None = None
    options_query: dict[str, Any] = field(default_factory=dict)
    options_headers: dict[str, Any] = field(default_factory=dict)


@dataclass
class RealtimeSession:
    """Client session for SSE realtime."""

    client_id: str
    response_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    subscriptions: list[RealtimeSubscription] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    auth_key: str | None = None
    auth_key_set: bool = False


@dataclass
class ParsedRealtimeTopic:
    """Normalized realtime topic metadata."""

    raw_topic: str
    base_topic: str
    collection_name: str
    resource: str
    options_query: dict[str, Any] = field(default_factory=dict)
    options_headers: dict[str, Any] = field(default_factory=dict)


def parse_realtime_topic(topic: str) -> ParsedRealtimeTopic:
    """Parse topic string and optional ``?options=...`` payload."""
    raw_topic = topic.strip()
    if not raw_topic:
        raise ValueError("Empty topic.")

    base_topic, _, query_string = raw_topic.partition("?")
    parts = base_topic.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid topic format: {raw_topic}")

    collection_name = parts[0]
    resource = parts[1]

    options_query: dict[str, Any] = {}
    options_headers: dict[str, Any] = {}
    if query_string:
        query_map = parse_qs(query_string, keep_blank_values=True)
        raw_options = query_map.get("options", [None])[0]
        if raw_options:
            try:
                parsed = json.loads(raw_options)
            except Exception as exc:
                raise ValueError(f"Invalid topic options JSON: {raw_topic}") from exc

            if not isinstance(parsed, dict):
                raise ValueError(f"Invalid topic options object: {raw_topic}")

            raw_query = parsed.get("query", {})
            raw_headers = parsed.get("headers", {})
            if raw_query is not None and not isinstance(raw_query, dict):
                raise ValueError(f"Invalid topic options query: {raw_topic}")
            if raw_headers is not None and not isinstance(raw_headers, dict):
                raise ValueError(f"Invalid topic options headers: {raw_topic}")

            options_query = raw_query or {}
            options_headers = raw_headers or {}

    return ParsedRealtimeTopic(
        raw_topic=raw_topic,
        base_topic=base_topic,
        collection_name=collection_name,
        resource=resource,
        options_query=options_query,
        options_headers=options_headers,
    )


def _normalize_request_headers(headers: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize request header keys for @request.headers.* lookups."""
    if not headers:
        return {}
    normalized: dict[str, Any] = {}
    for raw_key, value in headers.items():
        key = str(raw_key).lower()
        normalized[key] = value
        normalized[key.replace("-", "_")] = value
    return normalized


class SubscriptionManager:
    """Manage SSE realtime client sessions and subscriptions."""

    def __init__(self, extension_registry: Any | None = None):
        self.sessions: dict[str, RealtimeSession] = {}
        self._lock = asyncio.Lock()
        self.extension_registry = extension_registry

    def register_client(self) -> str:
        """Register a new client and return client ID."""
        client_id = secrets.token_urlsafe(32)
        session = RealtimeSession(client_id=client_id)
        self.sessions[client_id] = session
        logger.info(f"Registered SSE client: {client_id}")
        return client_id

    async def add_subscription(
        self,
        client_id: str,
        topic: str,
        auth_token: str | None = None,
        auth_payload: dict[str, Any] | None = None,
        *,
        base_topic: str | None = None,
        options_query: dict[str, Any] | None = None,
        options_headers: dict[str, Any] | None = None,
    ) -> None:
        """Add a subscription to a client session."""
        async with self._lock:
            session = self.sessions.get(client_id)
            if not session:
                raise ValueError(f"Client not found: {client_id}")

            parsed = parse_realtime_topic(topic)
            normalized_base_topic = base_topic or parsed.base_topic

            # Check if already subscribed
            for sub in session.subscriptions:
                if sub.topic == topic:
                    # Update auth token
                    sub.base_topic = normalized_base_topic
                    sub.auth_token = auth_token
                    sub.auth_payload = auth_payload
                    sub.options_query = options_query or parsed.options_query
                    sub.options_headers = options_headers or parsed.options_headers
                    logger.debug(f"Updated subscription: {client_id} -> {topic}")
                    return

            # Add new subscription
            session.subscriptions.append(
                RealtimeSubscription(
                    topic=topic,
                    base_topic=normalized_base_topic,
                    auth_token=auth_token,
                    auth_payload=auth_payload,
                    options_query=options_query or parsed.options_query,
                    options_headers=options_headers or parsed.options_headers,
                )
            )
            logger.debug(f"Added subscription: {client_id} -> {topic}")

    async def replace_subscriptions(
        self,
        client_id: str,
        subscriptions: list[RealtimeSubscription],
    ) -> None:
        """Replace all subscriptions for a client."""
        async with self._lock:
            session = self.sessions.get(client_id)
            if not session:
                raise ValueError(f"Client not found: {client_id}")
            session.subscriptions = list(subscriptions)
            logger.debug(
                "Replaced subscriptions: %s -> %d topic(s)",
                client_id,
                len(subscriptions),
            )

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
    *,
    engine: AsyncEngine | None = None,
    collection: Any | None = None,
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

    # Resolve collection when rule-based auth filtering is enabled.
    if engine is not None and collection is None:
        from ppbase.services.record_service import resolve_collection

        collection = await resolve_collection(engine, collection_name)
        if collection is None:
            logger.warning(
                "Skipping realtime broadcast for unknown collection: %s",
                collection_name,
            )
            return

    # Broadcast to all subscribed clients
    # The SSE event name MUST match the subscription topic the client used.
    # PocketBase SDK listens for events named exactly like the subscription topic.
    # e.g. if subscribed to "posts/*", the SSE event: field must be "posts/*"
    sessions_snapshot = list(subscription_manager.sessions.items())
    for client_id, session in sessions_snapshot:
        subscriptions_snapshot = list(session.subscriptions)
        for sub in subscriptions_snapshot:
            if sub.base_topic not in topics:
                continue

            if engine is not None and collection is not None:
                allowed = await _is_subscription_allowed_for_event(
                    engine=engine,
                    collection=collection,
                    subscription=sub,
                    record_id=record_id,
                    action=action,
                )
                if not allowed:
                    continue

            sub_event_data = event_data
            if (
                engine is not None
                and collection is not None
                and action != "delete"
                and sub.options_query
            ):
                maybe_event_data = await _build_subscription_event_data(
                    engine=engine,
                    collection=collection,
                    record_id=record_id,
                    action=action,
                    base_record=record_data,
                    subscription=sub,
                )
                if maybe_event_data is None:
                    continue
                sub_event_data = maybe_event_data

            try:
                await _send_realtime_message(
                    subscription_manager=subscription_manager,
                    session=session,
                    client_id=client_id,
                    subscription=sub,
                    topic=sub.topic,
                    data=sub_event_data,
                )
            except Exception as exc:
                logger.error("Failed to queue event for client %s: %s", client_id, exc)


async def _send_realtime_message(
    *,
    subscription_manager: SubscriptionManager,
    session: RealtimeSession,
    client_id: str,
    subscription: RealtimeSubscription,
    topic: str,
    data: dict[str, Any],
) -> None:
    extension_registry = getattr(subscription_manager, "extension_registry", None)
    if extension_registry is None:
        await session.response_queue.put({"topic": topic, "data": data})
        return

    event = RealtimeMessageSendEvent(
        subscription_manager=subscription_manager,
        session=session,
        client_id=client_id,
        subscription=subscription,
        topic=topic,
        data=data,
    )
    hook = extension_registry.hooks.get(HOOK_REALTIME_MESSAGE_SEND)

    async def _default_send_handler(e: RealtimeMessageSendEvent) -> None:
        await session.response_queue.put({"topic": e.topic, "data": e.data})

    await hook.trigger(event, _default_send_handler)


def _build_rule_context(
    token_payload: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build rule-engine and filter-parser contexts from token payload."""
    if token_payload is None:
        auth_ctx: dict[str, Any] | None = None
        auth_info: dict[str, Any] = {}
    elif token_payload.get("type") == "admin":
        auth_ctx = {
            "is_admin": True,
            "@request.auth.id": token_payload.get("id", ""),
            "@request.auth.email": token_payload.get("email", ""),
        }
        auth_info = {
            "id": token_payload.get("id", ""),
            "email": token_payload.get("email", ""),
            "type": token_payload.get("type", ""),
            "collectionId": token_payload.get("collectionId", ""),
            "collectionName": token_payload.get("collectionName", ""),
        }
    else:
        auth_ctx = {
            "is_admin": False,
            "@request.auth.id": token_payload.get("id", ""),
            "@request.auth.collectionId": token_payload.get("collectionId", ""),
            "@request.auth.collectionName": token_payload.get("collectionName", ""),
            "@request.auth.type": token_payload.get("type", ""),
        }
        auth_info = {
            "id": token_payload.get("id", ""),
            "email": token_payload.get("email", ""),
            "type": token_payload.get("type", ""),
            "collectionId": token_payload.get("collectionId", ""),
            "collectionName": token_payload.get("collectionName", ""),
        }

    request_context = {
        "context": "realtime",
        "method": "GET",
        "headers": {},
        "auth": auth_info,
        "data": {},
        "query": {},
    }
    return auth_ctx, request_context


async def _is_subscription_allowed_for_event(
    *,
    engine: AsyncEngine,
    collection: Any,
    subscription: RealtimeSubscription,
    record_id: str,
    action: str,
) -> bool:
    """Check if a subscription is allowed to receive a specific event."""
    from ppbase.services.record_service import check_record_rule
    from ppbase.services.rule_engine import check_rule

    if subscription.base_topic.endswith("/*"):
        rule = collection.list_rule
    else:
        rule = collection.view_rule

    auth_ctx, request_context = _build_rule_context(subscription.auth_payload)
    if subscription.options_query:
        request_context["query"] = dict(subscription.options_query)
    if subscription.options_headers:
        request_context["headers"] = _normalize_request_headers(
            subscription.options_headers
        )
    rule_result = check_rule(rule, auth_ctx)

    # Optional per-subscription filter
    filter_value = subscription.options_query.get("filter")
    filter_expr = filter_value.strip() if isinstance(filter_value, str) else ""
    if filter_expr:
        if rule_result is True:
            rule_result = filter_expr
        elif isinstance(rule_result, str):
            rule_result = f"({rule_result}) && ({filter_expr})"

    if rule_result is False:
        return False
    if rule_result is True:
        return True

    # For expression rules we need the row to exist in DB.
    # Delete events are dropped in this case to avoid leaking protected activity.
    if action == "delete":
        return False

    try:
        return await check_record_rule(
            engine,
            collection,
            record_id,
            rule_result,
            request_context,
        )
    except Exception as exc:
        logger.warning(
            "Realtime rule check failed for %s/%s: %s",
            collection.name,
            record_id,
            exc,
        )
        return False


def _parse_fields(fields_param: str | None) -> set[str] | None:
    """Parse ``fields`` query into a normalized set."""
    if not fields_param:
        return None
    fields = {name.strip() for name in fields_param.split(",") if name.strip()}
    return fields or None


def _apply_fields_filter(record: dict[str, Any], fields_param: str | None) -> dict[str, Any]:
    """Apply PocketBase-like fields filtering to a single record."""
    fields = _parse_fields(fields_param)
    if not fields or "*" in fields:
        return record
    return {k: v for k, v in record.items() if k in fields}


async def _build_subscription_event_data(
    *,
    engine: AsyncEngine,
    collection: Any,
    record_id: str,
    action: str,
    base_record: dict[str, Any] | None,
    subscription: RealtimeSubscription,
) -> dict[str, Any] | None:
    """Build per-subscription event payload with options query applied."""
    from ppbase.services.expand_service import expand_records
    from ppbase.services.record_service import get_all_collections, get_record

    _auth_ctx, request_context = _build_rule_context(subscription.auth_payload)
    if subscription.options_query:
        request_context["query"] = dict(subscription.options_query)
    if subscription.options_headers:
        request_context["headers"] = _normalize_request_headers(
            subscription.options_headers
        )

    record = dict(base_record) if isinstance(base_record, dict) else None
    if record is None:
        record = await get_record(
            engine,
            collection,
            record_id,
            request_context=request_context,
        )
        if record is None:
            return None

    expand_value = subscription.options_query.get("expand")
    expand = expand_value.strip() if isinstance(expand_value, str) else ""
    if expand:
        all_colls = await get_all_collections(engine)
        items = [record]
        try:
            await expand_records(
                engine,
                collection,
                items,
                expand,
                all_colls,
                request_context=request_context,
            )
            record = items[0]
        except Exception as exc:
            logger.warning(
                "Realtime expand failed for %s/%s on topic %s: %s",
                collection.name,
                record_id,
                subscription.topic,
                exc,
            )
            return None

    fields_value = subscription.options_query.get("fields")
    fields = fields_value if isinstance(fields_value, str) else None
    record = _apply_fields_filter(record, fields)

    return {
        "action": action,
        "record": record,
    }


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

            from ppbase.services.record_service import get_record, resolve_collection

            collection = await resolve_collection(engine, collection_name)
            if collection is None:
                logger.warning(f"Collection not found for realtime event: {collection_name}")
                return

            # Get record data (if not delete)
            record_data = None
            if action != "delete":
                try:
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
                engine=engine,
                collection=collection,
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
