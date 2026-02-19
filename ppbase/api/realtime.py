"""FastAPI routes for SSE realtime endpoints."""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from ppbase.ext.events import RealtimeConnectEvent, RealtimeSubscribeEvent
from ppbase.ext.registry import (
    HOOK_REALTIME_CONNECT_REQUEST,
    HOOK_REALTIME_SUBSCRIBE_REQUEST,
    get_extension_registry,
)
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


async def _trigger_realtime_hook(
    request: Request,
    hook_name: str,
    event,
    default_handler,
):
    extensions = get_extension_registry(request.app)
    if extensions is None:
        return await default_handler(event)
    hook = extensions.hooks.get(hook_name)
    return await hook.trigger(event, default_handler)


@router.get("/api/realtime")
async def realtime_connect(
    request: Request,
    subscription_manager: SubscriptionManager = Depends(get_subscription_manager),
    auth: dict[str, Any] | None = Depends(get_optional_auth),
):
    """Establish SSE connection and return clientId."""
    event = RealtimeConnectEvent(
        app=request.app,
        request=request,
        subscription_manager=subscription_manager,
        auth=auth,
    )

    async def _default_connect_handler(e: RealtimeConnectEvent):
        client_id = e.client_id or subscription_manager.register_client()
        e.client_id = client_id

        async def event_generator():
            """Generate SSE events for the client."""
            yield (
                f"id: {client_id}\nevent: PB_CONNECT\ndata: "
                f"{json.dumps({'clientId': client_id})}\n\n"
            )

            session = subscription_manager.get_session(client_id)
            if not session:
                return

            try:
                event_counter = 0
                while True:
                    try:
                        event = await asyncio.wait_for(
                            session.response_queue.get(), timeout=300
                        )
                        topic = event.get("topic", "")
                        data = event.get("data", {})
                        event_counter += 1
                        event_id = f"{client_id}_{event_counter}"
                        yield (
                            f"id: {event_id}\nevent: {topic}\n"
                            f"data: {json.dumps(data)}\n\n"
                        )
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                logger.debug("SSE stream cancelled for client: %s", client_id)
            except Exception as exc:
                logger.error("Error in SSE stream for client %s: %s", client_id, exc)
            finally:
                await subscription_manager.disconnect_client(client_id)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return await _trigger_realtime_hook(
        request,
        HOOK_REALTIME_CONNECT_REQUEST,
        event,
        _default_connect_handler,
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

    event = RealtimeSubscribeEvent(
        app=request.app,
        request=request,
        subscription_manager=subscription_manager,
        client_id=client_id,
        subscriptions=[str(item) for item in subscriptions if isinstance(item, str)],
        authorization=authorization,
        auth=auth,
    )

    async def _default_subscribe_handler(e: RealtimeSubscribeEvent):
        parsed_subscriptions: list[RealtimeSubscription] = []
        for sub in e.subscriptions:
            raw_topic = sub.strip()
            if not raw_topic:
                continue

            try:
                parsed_topic: ParsedRealtimeTopic = parse_realtime_topic(raw_topic)
            except ValueError as exc:
                logger.warning("Invalid realtime topic '%s': %s", raw_topic, exc)
                continue

            try:
                collection = await resolve_collection(engine, parsed_topic.collection_name)
                if collection is None:
                    logger.warning("Collection not found: %s", parsed_topic.collection_name)
                    continue
            except Exception as exc:
                logger.warning(
                    "Failed to resolve collection %s: %s",
                    parsed_topic.collection_name,
                    exc,
                )
                continue

            parsed_subscriptions.append(
                RealtimeSubscription(
                    topic=parsed_topic.raw_topic,
                    base_topic=parsed_topic.base_topic,
                    auth_token=auth_token,
                    auth_payload=e.auth,
                    options_query=parsed_topic.options_query,
                    options_headers=parsed_topic.options_headers,
                )
            )

        e.parsed_subscriptions = parsed_subscriptions
        await subscription_manager.replace_subscriptions(
            e.client_id,
            parsed_subscriptions,
        )
        return Response(status_code=204)

    return await _trigger_realtime_hook(
        request,
        HOOK_REALTIME_SUBSCRIBE_REQUEST,
        event,
        _default_subscribe_handler,
    )
