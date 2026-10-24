"""Emit both ``code`` and ``status`` in PPBase JSON error bodies.

PocketBase 0.22.x wraps an API error as ``{"code", "message", "data"}``
(v0.22.34 apis/api_error.go l.12-15); since v0.23.0 the same key is named
``status`` (v0.40.6 tools/router/error.go l.36-42).
PPBase builds the current ``{"status", "message", "data"}`` shape in many
places, so a 0.22 client reading ``response.code`` (``ClientResponseError
.response.code`` in the JS SDK, ``ClientException.response["code"]`` in the
Dart SDK) found nothing.

This pure ASGI middleware mirrors ``status`` into ``code`` for exactly that
shape: an error response (HTTP status >= 400) whose ``application/json``
body is an object with an integer ``status`` equal to the HTTP status, a
``message`` and a ``data`` key, and no ``code`` key.  Every other body
(success responses, files, SSE, already 0.22-shaped legacy bodies such as
``{"code", "message", "data"}``, custom payloads) is passed through
untouched.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    for key, value in headers:
        if key.lower() == b"content-type":
            return value.split(b";", 1)[0].strip().lower() == b"application/json"
    return False


def with_legacy_code(body: bytes, status: int) -> bytes | None:
    """Return ``body`` with a ``code`` key, or ``None`` to keep it as is."""
    try:
        payload: Any = json.loads(body)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or "code" in payload
        or "message" not in payload
        or "data" not in payload
        or type(payload.get("status")) is not int
        or payload["status"] != status
    ):
        return None
    return json.dumps(
        {"code": status, **payload},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


class ErrorEnvelopeCodeMiddleware:
    """Mirror the error ``status`` as PocketBase 0.22's ``code``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start: Message | None = None
        chunks: list[bytes] = []

        async def wrapped_send(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if int(message["status"]) >= 400 and _is_json(headers):
                    start = message
                    return
                await send(message)
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            rewritten = with_legacy_code(body, int(start["status"]))
            headers = list(start.get("headers", []))
            if rewritten is not None:
                body = rewritten
                headers = [
                    (key, value)
                    for key, value in headers
                    if key.lower() != b"content-length"
                ]
                headers.append((b"content-length", str(len(body)).encode("ascii")))
            pending, start = start, None
            chunks.clear()
            await send({**pending, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped_send)
