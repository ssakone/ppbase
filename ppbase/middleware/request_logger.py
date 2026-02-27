"""Request logging middleware.

Intercepts every HTTP request, measures execution time, and writes a row to
the ``_requests`` system table.  DB writes are fire-and-forget (background
task) so they never block the response.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Paths to skip logging (admin UI assets, health check, realtime SSE)
_SKIP_PREFIXES = ("/_/", "/api/health", "/api/realtime")

# Capture only small, text-like payloads to avoid large/binary blobs in logs.
_MAX_CAPTURE_BYTES = 32 * 1024
_CAPTURE_CONTENT_TYPES = (
    "application/json",
    "application/x-www-form-urlencoded",
    "text/",
)
_SENSITIVE_KEYS = (
    "password",
    "passwordconfirm",
    "token",
    "tokensecret",
    "secret",
    "authorization",
    "cookie",
    "apikey",
    "api_key",
    "clientsecret",
    "client_secret",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "codeverifier",
    "code_verifier",
)


def _normalize_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum() or ch == "_")


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return any(s in normalized for s in _SENSITIVE_KEYS)


def _redact_sensitive(data: Any) -> Any:
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for key, value in data.items():
            if _is_sensitive_key(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_sensitive(value)
        return redacted
    if isinstance(data, list):
        return [_redact_sensitive(item) for item in data]
    return data


def _parse_content_length(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _normalized_content_type(content_type: str | None) -> str:
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _is_capture_content_type(content_type: str | None) -> bool:
    normalized_type = _normalized_content_type(content_type)
    if not normalized_type:
        return False

    return any(
        normalized_type == allowed or normalized_type.startswith(allowed)
        for allowed in _CAPTURE_CONTENT_TYPES
    )


def _should_capture_body(content_type: str | None, content_length: int | None) -> bool:
    if content_length is None:
        return False
    if content_length > _MAX_CAPTURE_BYTES:
        return False
    return _is_capture_content_type(content_type)


def _decode_body(body: bytes, content_type: str | None) -> Any:
    if not body:
        return None

    if len(body) > _MAX_CAPTURE_BYTES:
        preview = body[:_MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
        return {
            "_truncated": True,
            "size": len(body),
            "preview": preview,
        }

    normalized_type = _normalized_content_type(content_type)
    text = body.decode("utf-8", errors="replace")

    if normalized_type == "application/json":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    return text


async def _write_log(app_state, entry: dict) -> None:
    """Write a single request log entry to the DB (best-effort)."""
    try:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        from ppbase.db.system_tables import RequestLogRecord
        from ppbase.core.id_generator import generate_id
        from ppbase.db.engine import get_engine

        try:
            engine = get_engine()
        except RuntimeError:
            return

        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                row = RequestLogRecord(
                    id=generate_id(),
                    url=entry["url"],
                    method=entry["method"],
                    status=entry["status"],
                    exec_time=entry["exec_time"],
                    remote_ip=entry.get("remote_ip"),
                    referer=entry.get("referer"),
                    user_agent=entry.get("user_agent"),
                    request_body=entry.get("request_body"),
                    response_body=entry.get("response_body"),
                    meta=entry.get("meta"),
                )
                session.add(row)
    except Exception as exc:
        logger.debug("Request log write failed: %s", exc)


class RequestLoggerMiddleware(BaseHTTPMiddleware):
    """Middleware that logs every API request to the _requests table."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Skip non-API paths to avoid noise
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        content_length = _parse_content_length(request.headers.get("content-length"))
        request_content_type = request.headers.get("content-type")
        should_capture_request = _should_capture_body(request_content_type, content_length)

        request_body_bytes = await request.body() if should_capture_request else b""
        if should_capture_request:
            request_body_sent = False

            async def receive() -> dict:
                nonlocal request_body_sent
                if request_body_sent:
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }

                request_body_sent = True
                return {
                    "type": "http.request",
                    "body": request_body_bytes,
                    "more_body": False,
                }

            request = Request(request.scope, receive)

        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)

        response_content_length = _parse_content_length(response.headers.get("content-length"))
        response_content_type = response.headers.get("content-type")
        response_is_capturable_type = _is_capture_content_type(response_content_type)
        should_capture_response = _should_capture_body(
            response_content_type,
            response_content_length,
        )

        request_payload = (
            _redact_sensitive(_decode_body(request_body_bytes, request_content_type))
            if should_capture_request
            else None
        )

        response_payload = None
        if should_capture_response:
            response_chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                response_chunks.append(chunk)

            response_body = b"".join(response_chunks)
            response_payload = _redact_sensitive(_decode_body(response_body, response_content_type))

            response = Response(
                content=response_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
                background=response.background,
            )
        elif response_is_capturable_type and response_content_length is not None and response_content_length > _MAX_CAPTURE_BYTES:
            response_payload = {
                "_truncated": True,
                "size": response_content_length,
                "contentType": _normalized_content_type(response_content_type),
            }

        entry = {
            "url": str(request.url),
            "method": request.method,
            "status": response.status_code,
            "exec_time": elapsed_ms,
            "remote_ip": request.client.host if request.client else None,
            "referer": request.headers.get("referer"),
            "user_agent": request.headers.get("user-agent"),
            "request_body": request_payload,
            "response_body": response_payload,
            "meta": None,
        }

        asyncio.create_task(_write_log(request.app.state, entry))

        return response
