"""FastAPI routes for SSE realtime endpoints."""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from ppbase.services.realtime_service import SubscriptionManager
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
        return _error_response(404, f"Client not found: {client_id}")

    engine = get_engine()

    # If subscriptions is empty, clear all subscriptions
    if not subscriptions:
        await subscription_manager.clear_subscriptions(client_id)
        return Response(status_code=204)

    # Parse auth token from Authorization header
    auth_token = None
    if authorization:
        auth_token = authorization
        if auth_token.lower().startswith("bearer "):
            auth_token = auth_token[7:].strip()

    # Process each subscription
    for sub in subscriptions:
        # Parse topic: "collectionName/*" or "collectionName/recordId"
        topic = sub.strip()
        if not topic:
            continue

        parts = topic.split("/")
        if len(parts) != 2:
            logger.warning(f"Invalid topic format: {topic}")
            continue

        collection_name = parts[0]
        resource = parts[1]

        # Resolve collection
        try:
            collection = await resolve_collection(engine, collection_name)
            if collection is None:
                logger.warning(f"Collection not found: {collection_name}")
                continue
        except Exception as e:
            logger.warning(f"Failed to resolve collection {collection_name}: {e}")
            continue

        # Check authorization
        # For collection-wide subscriptions (*), check listRule
        # For single-record subscriptions, check viewRule
        # TODO: Actually evaluate rules with auth context
        # For now, we'll allow all subscriptions and rely on
        # the rules being enforced when records are fetched
        # This matches PocketBase behavior where subscriptions
        # are allowed but events are filtered by rules

        # Add subscription
        await subscription_manager.add_subscription(client_id, topic, auth_token)

    return Response(status_code=204)
