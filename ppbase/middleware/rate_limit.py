"""Global API rate limiting middleware.

Configuration source:
- ``settings.rateLimiting`` (preferred)
- ``settings.rateLimits`` (legacy compatibility fallback)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ppbase.db.engine import get_engine
from ppbase.db.system_tables import ParamRecord


@dataclass(slots=True)
class _RateLimitConfig:
    enabled: bool = False
    max_requests: int = 1000
    window_seconds: int = 60


def _to_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except Exception:
        return default
    if parsed <= 0:
        return default
    return parsed


def _extract_rate_limit_config(settings_value: dict[str, Any]) -> _RateLimitConfig:
    # Preferred shape used by admin-ui settings form.
    direct = settings_value.get("rateLimiting")
    if isinstance(direct, dict):
        return _RateLimitConfig(
            enabled=bool(direct.get("enabled", False)),
            max_requests=_to_positive_int(direct.get("maxRequests"), 1000),
            window_seconds=_to_positive_int(direct.get("window"), 60),
        )

    # Legacy/compat fallback shape.
    legacy = settings_value.get("rateLimits")
    if isinstance(legacy, dict):
        enabled = bool(legacy.get("enabled", False))
        max_requests = 1000
        window_seconds = 60
        rules = legacy.get("rules")
        if isinstance(rules, list) and rules:
            first = rules[0]
            if isinstance(first, dict):
                max_requests = _to_positive_int(first.get("maxRequests"), max_requests)
                window_seconds = _to_positive_int(
                    first.get("window", first.get("duration")),
                    window_seconds,
                )
        return _RateLimitConfig(
            enabled=enabled,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )

    return _RateLimitConfig(enabled=False)


def _resolve_client_identifier(request: Request, settings_value: dict[str, Any]) -> str:
    trusted_proxy = settings_value.get("trustedProxy")
    if isinstance(trusted_proxy, dict):
        raw_headers = trusted_proxy.get("headers")
        use_leftmost = bool(trusted_proxy.get("useLeftmostIP", False))
        if isinstance(raw_headers, list):
            for header_name in raw_headers:
                header = str(header_name or "").strip().lower()
                if not header:
                    continue
                value = request.headers.get(header)
                if not value:
                    continue
                ips = [part.strip() for part in value.split(",") if part.strip()]
                if ips:
                    return ips[0] if use_leftmost else ips[-1]

    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply global request limits for API endpoints."""

    _CACHE_TTL_SECONDS = 2.0
    _SKIP_PREFIXES = ("/api/health", "/api/realtime")

    def __init__(self, app):
        super().__init__(app)
        self._lock = asyncio.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._cache_expires_at = 0.0
        self._cache_version = -1
        self._cached_settings: dict[str, Any] = {}
        self._cached_config = _RateLimitConfig(enabled=False)

    async def _load_settings_and_config(self, request: Request) -> tuple[dict[str, Any], _RateLimitConfig]:
        now = time.monotonic()
        current_version = int(getattr(request.app.state, "rate_limit_settings_version", 0) or 0)
        if current_version != self._cache_version:
            self._cache_version = current_version
            self._cache_expires_at = 0.0

        if now < self._cache_expires_at:
            return self._cached_settings, self._cached_config

        settings_value: dict[str, Any] = {}
        try:
            engine = get_engine()
            factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                stmt = select(ParamRecord.value).where(ParamRecord.key == "settings")
                raw_value = (await session.execute(stmt)).scalars().first()
                if isinstance(raw_value, dict):
                    settings_value = raw_value
        except Exception:
            settings_value = {}

        config = _extract_rate_limit_config(settings_value)
        self._cached_settings = settings_value
        self._cached_config = config
        self._cache_expires_at = now + self._CACHE_TTL_SECONDS
        return settings_value, config

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not path.startswith("/api"):
            return await call_next(request)
        if any(path.startswith(prefix) for prefix in self._SKIP_PREFIXES):
            return await call_next(request)

        settings_value, config = await self._load_settings_and_config(request)
        if not config.enabled:
            return await call_next(request)

        now = time.monotonic()
        identifier = _resolve_client_identifier(request, settings_value)

        async with self._lock:
            bucket = self._hits.get(identifier)
            if bucket is None:
                bucket = deque()
                self._hits[identifier] = bucket

            window_start = now - config.window_seconds
            while bucket and bucket[0] <= window_start:
                bucket.popleft()

            if len(bucket) >= config.max_requests:
                retry_after = max(1, int((bucket[0] + config.window_seconds) - now))
                return JSONResponse(
                    status_code=429,
                    content={
                        "status": 429,
                        "message": "Too many requests.",
                        "data": {},
                    },
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(config.max_requests),
                        "X-RateLimit-Remaining": "0",
                    },
                )

            bucket.append(now)
            remaining = max(0, config.max_requests - len(bucket))
            reset_after = max(0, int((bucket[0] + config.window_seconds) - now))

        response = await call_next(request)
        response.headers.setdefault("X-RateLimit-Limit", str(config.max_requests))
        response.headers.setdefault("X-RateLimit-Remaining", str(remaining))
        response.headers.setdefault("X-RateLimit-Reset", str(reset_after))
        return response
