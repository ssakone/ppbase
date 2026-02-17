"""FastAPI routes for SSE realtime endpoints."""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from ppbase.services.realtime_service import (
    ParsedRealtimeTopic,
    RealtimeSubscription,
    SubscriptionManager,
    parse_realtime_topic,
)
from ppbase.services.record_service import resolve_collection

logger = logging.getLogger(__name__)
router = APIRouter()


def get_subscription_manager(request: Request) -> SubscriptionManager:
    """Dependency to get subscription manager from app state."""
    return request.app.state.subscription_manager


def _error_response(status: int, message: str, data: Any = None) -> JSONResponse:
    body: dict[str, Any] = {
        "status": status,
        "message": message,
        "data": data or {},
    }
    return JSONResponse(content=body, status_code=status)


def _build_realtime_auth_key(auth: dict[str, Any] | None) -> str | None:
    """Build a stable auth identity key for realtime subscription consistency."""
    if not auth:
        return None
    token_type = auth.get("type", "")
    auth_id = auth.get("id", "")
    collection_id = auth.get("collectionId", "")
    return f"{token_type}:{collection_id}:{auth_id}"


@router.get("/api/realtime")
async def realtime_connect(
    request: Request,
    subscription_manager: SubscriptionManager = Depends(get_subscription_manager),
):
    """Establish SSE connection and return clientId."""
    client_id = subscription_manager.register_client()

    async def event_generator():
        """Generate SSE events for the client."""
        # Send initial PB_CONNECT event with clientId (with id field for SDK compatibility)
        yield f"id: {client_id}\nevent: PB_CONNECT\ndata: {json.dumps({'clientId': client_id})}\n\n"

        # Stream events from client's queue
        session = subscription_manager.get_session(client_id)
        if not session:
            return

        try:
            event_counter = 0
            while True:
                # Wait for event with 5-minute timeout (idle timeout)
                try:
                    event = await asyncio.wait_for(
                        session.response_queue.get(), timeout=300
                    )
                    topic = event.get("topic", "")
                    data = event.get("data", {})
                    event_counter += 1
                    event_id = f"{client_id}_{event_counter}"
                    yield f"id: {event_id}\nevent: {topic}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    # 5-minute idle timeout - send keepalive
                    yield f": keepalive\n\n"
        except asyncio.CancelledError:
            logger.debug(f"SSE stream cancelled for client: {client_id}")
        except Exception as e:
            logger.error(f"Error in SSE stream for client {client_id}: {e}")
        finally:
            # Cleanup on disconnect
            await subscription_manager.disconnect_client(client_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.post("/api/realtime")
async def realtime_subscribe(
    request: Request,
    authorization: str | None = Header(None),
    subscription_manager: SubscriptionManager = Depends(get_subscription_manager),
    auth: dict[str, Any] | None = Depends(get_optional_auth),
):
    """Subscribe/unsubscribe to topics."""
    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    client_id = body.get("clientId", "").strip()
    subscriptions = body.get("subscriptions", [])

    if not client_id:
        return _error_response(
            400,
            "Missing clientId.",
            {"clientId": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    # Verify client exists
    session = subscription_manager.get_session(client_id)
    if not session:
        return _error_response(404, "Missing or invalid client id.")

    current_auth_key = _build_realtime_auth_key(auth)
    if session.auth_key_set and session.auth_key != current_auth_key:
        return _error_response(
            403,
            "The current and the previous request authorization don't match.",
        )
    if not session.auth_key_set:
        session.auth_key = current_auth_key
        session.auth_key_set = True

    engine = get_engine()

    # Parse auth token from Authorization header
    auth_token = None
    if authorization:
        auth_token = authorization
        if auth_token.lower().startswith("bearer "):
            auth_token = auth_token[7:].strip()

    parsed_subscriptions: list[RealtimeSubscription] = []

    # Process each subscription (new set replaces old subscriptions)
    for sub in subscriptions:
        # Parse topic: "collectionName/*" or "collectionName/recordId"
        raw_topic = sub.strip()
        if not raw_topic:
            continue

        try:
            parsed_topic: ParsedRealtimeTopic = parse_realtime_topic(raw_topic)
        except ValueError as e:
            logger.warning("Invalid realtime topic '%s': %s", raw_topic, e)
            continue

        # Resolve collection
        try:
            collection = await resolve_collection(engine, parsed_topic.collection_name)
            if collection is None:
                logger.warning(f"Collection not found: {parsed_topic.collection_name}")
                continue
        except Exception as e:
            logger.warning(
                "Failed to resolve collection %s: %s",
                parsed_topic.collection_name,
                e,
            )
            continue

        parsed_subscriptions.append(
            RealtimeSubscription(
                topic=parsed_topic.raw_topic,
                base_topic=parsed_topic.base_topic,
                auth_token=auth_token,
                auth_payload=auth,
                options_query=parsed_topic.options_query,
                options_headers=parsed_topic.options_headers,
            )
        )

    await subscription_manager.replace_subscriptions(
        client_id,
        parsed_subscriptions,
    )

    return Response(status_code=204)
