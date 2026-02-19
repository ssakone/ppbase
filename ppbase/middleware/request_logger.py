"""Request logging middleware.

Intercepts every HTTP request, measures execution time, and writes a row to
the ``_requests`` system table.  DB writes are fire-and-forget (background
task) so they never block the response.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Paths to skip logging (admin UI assets, health check, realtime SSE)
_SKIP_PREFIXES = ("/_/", "/api/health", "/api/realtime")


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

        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)

        entry = {
            "url": str(request.url),
            "method": request.method,
            "status": response.status_code,
            "exec_time": elapsed_ms,
            "remote_ip": request.client.host if request.client else None,
            "referer": request.headers.get("referer"),
            "user_agent": request.headers.get("user-agent"),
            "meta": None,
        }

        asyncio.create_task(_write_log(request.app.state, entry))

        return response
