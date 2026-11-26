"""API rate limiting middleware.

Configuration source:
- ``settings.rateLimiting`` (preferred)
- ``settings.rateLimits`` (legacy compatibility fallback)
- ``settings.rateLimiting.auth`` for the dedicated auth endpoint limit
  (password auth, OTP and password reset requests). It is enabled by default
  for new installs and is applied independently of the global limit.
"""

from __future__ import annotations

import asyncio
import re
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


# Default limit for auth endpoints, per client and per endpoint path.
DEFAULT_AUTH_RATE_LIMIT: dict[str, Any] = {
    "enabled": True,
    "maxRequests": 20,
    "window": 60,
}

_AUTH_PATH_RE = re.compile(
    r"^/api/(?:"
    r"collections/[^/]+/(?:auth-with-password|request-otp|auth-with-otp"
    r"|request-password-reset|confirm-password-reset)"
    r"|admins/auth-with-password"
    r")/?$"
)


def is_auth_rate_limited_path(method: str, path: str) -> bool:
    """Return whether a request targets an auth endpoint with its own limit."""
    return method.upper() == "POST" and bool(_AUTH_PATH_RE.match(path))


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


def _extract_auth_rate_limit_config(settings_value: dict[str, Any]) -> _RateLimitConfig:
    """Return the auth endpoint limit.

    New installs (no stored settings yet, or settings created from the current
    defaults) get :data:`DEFAULT_AUTH_RATE_LIMIT`. Settings saved by earlier
    versions without ``rateLimiting.auth`` keep their previous behaviour until
    the limit is configured.
    """
    defaults = DEFAULT_AUTH_RATE_LIMIT
    if not settings_value:
        raw: Any = defaults
    else:
        direct = settings_value.get("rateLimiting")
        raw = direct.get("auth") if isinstance(direct, dict) else None
        if not isinstance(raw, dict):
            return _RateLimitConfig(enabled=False)
    return _RateLimitConfig(
        enabled=bool(raw.get("enabled", defaults["enabled"])),
        max_requests=_to_positive_int(raw.get("maxRequests"), defaults["maxRequests"]),
        window_seconds=_to_positive_int(raw.get("window"), defaults["window"]),
    )


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


def _too_many_requests(retry_after: int, limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "status": 429,
            "message": "Too many requests.",
            "data": {},
        },
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
        },
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply API request limits (global and auth endpoint specific)."""

    _CACHE_TTL_SECONDS = 2.0
    _SKIP_PREFIXES = ("/api/health", "/api/realtime")

    def __init__(self, app):
        super().__init__(app)
        self._lock = asyncio.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._auth_hits: dict[str, deque[float]] = {}
        self._cache_expires_at = 0.0
        self._cache_version = -1
        self._cached_settings: dict[str, Any] = {}
        self._cached_config = _RateLimitConfig(enabled=False)
        self._cached_auth_config = _RateLimitConfig(enabled=False)

    async def _load_settings_and_config(
        self, request: Request
    ) -> tuple[dict[str, Any], _RateLimitConfig, _RateLimitConfig]:
        now = time.monotonic()
        current_version = int(getattr(request.app.state, "rate_limit_settings_version", 0) or 0)
        if current_version != self._cache_version:
            self._cache_version = current_version
            self._cache_expires_at = 0.0

        if now < self._cache_expires_at:
            return self._cached_settings, self._cached_config, self._cached_auth_config

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
        auth_config = _extract_auth_rate_limit_config(settings_value)
        self._cached_settings = settings_value
        self._cached_config = config
        self._cached_auth_config = auth_config
        self._cache_expires_at = now + self._CACHE_TTL_SECONDS
        return settings_value, config, auth_config

    @staticmethod
    def _hit(
        hits: dict[str, deque[float]],
        key: str,
        config: _RateLimitConfig,
        now: float,
    ) -> tuple[bool, int, int]:
        """Record a request; return ``(allowed, remaining_or_retry, reset)``."""
        bucket = hits.get(key)
        if bucket is None:
            bucket = deque()
            hits[key] = bucket

        window_start = now - config.window_seconds
        while bucket and bucket[0] <= window_start:
            bucket.popleft()

        if len(bucket) >= config.max_requests:
            retry_after = max(1, int((bucket[0] + config.window_seconds) - now))
            return False, retry_after, retry_after

        bucket.append(now)
        remaining = max(0, config.max_requests - len(bucket))
        reset_after = max(0, int((bucket[0] + config.window_seconds) - now))
        return True, remaining, reset_after

    @staticmethod
    def _prune(hits: dict[str, deque[float]], now: float, window: int) -> None:
        """Drop idle buckets so the per-client maps do not grow unbounded."""
        if len(hits) < 10_000:
            return
        cutoff = now - window
        for key in [k for k, b in hits.items() if not b or b[-1] <= cutoff]:
            hits.pop(key, None)

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not path.startswith("/api"):
            return await call_next(request)
        if any(path.startswith(prefix) for prefix in self._SKIP_PREFIXES):
            return await call_next(request)

        settings_value, config, auth_config = await self._load_settings_and_config(request)
        auth_limited = auth_config.enabled and is_auth_rate_limited_path(
            request.method, path
        )
        if not config.enabled and not auth_limited:
            return await call_next(request)

        now = time.monotonic()
        identifier = _resolve_client_identifier(request, settings_value)
        headers: tuple[int, int, int] | None = None

        async with self._lock:
            if auth_limited:
                self._prune(self._auth_hits, now, auth_config.window_seconds)
                allowed, value, reset_after = self._hit(
                    self._auth_hits,
                    f"{identifier}|{path.rstrip('/')}",
                    auth_config,
                    now,
                )
                if not allowed:
                    return _too_many_requests(value, auth_config.max_requests)
                headers = (auth_config.max_requests, value, reset_after)

            if config.enabled:
                self._prune(self._hits, now, config.window_seconds)
                allowed, value, reset_after = self._hit(
                    self._hits, identifier, config, now
                )
                if not allowed:
                    return _too_many_requests(value, config.max_requests)
                if headers is None:
                    headers = (config.max_requests, value, reset_after)

        response = await call_next(request)
        if headers is not None:
            limit, remaining, reset_after = headers
            response.headers.setdefault("X-RateLimit-Limit", str(limit))
            response.headers.setdefault("X-RateLimit-Remaining", str(remaining))
            response.headers.setdefault("X-RateLimit-Reset", str(reset_after))
        return response
