"""Main API router that aggregates all sub-routers."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.health import router as health_router
from ppbase.api.admins import router as admins_router
from ppbase.api.settings import router as settings_router
from ppbase.api.files import router as files_router
from ppbase.api.logs import router as logs_router
from ppbase.api.deps import get_session

api_router = APIRouter()


# ---------------------------------------------------------------------------
# Public init status endpoint
# ---------------------------------------------------------------------------


@api_router.get("/init", tags=["init"])
async def init_status(
    session: AsyncSession = Depends(get_session),
    token: str | None = None,
):
    """Check if the application needs initial setup (no admins exist).

    When no admins exist, returns needsSetup: true only if a valid setup token
    is provided (from the one-time URL printed at startup). Otherwise returns
    needsSetup: false to avoid leaking that setup is required.
    """
    from ppbase.services.setup_service import validate_setup_token

    if await validate_setup_token(session, token):
        return {"needsSetup": True}
    return {"needsSetup": False}


# Always-available routes
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(admins_router, prefix="/admins", tags=["admins"])
api_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_router.include_router(files_router, prefix="/files", tags=["files"])
api_router.include_router(logs_router, tags=["logs"])

# Collections router -- built by another agent; import gracefully.
# NOTE: The collections router already defines prefix="/collections" internally.
try:
    from ppbase.api.collections import router as collections_router

    api_router.include_router(collections_router, tags=["collections"])
except (ImportError, AttributeError):
    _collections_stub = APIRouter(prefix="/collections")

    @_collections_stub.get("")
    async def _collections_list():
        return {"page": 1, "perPage": 30, "totalItems": 0, "totalPages": 1, "items": []}

    api_router.include_router(_collections_stub, tags=["collections"])

# Migrations router -- import gracefully.
# NOTE: The migrations router already defines prefix="/migrations" internally.
try:
    from ppbase.api.migrations import router as migrations_router

    api_router.include_router(migrations_router, tags=["migrations"])
except (ImportError, AttributeError):
    pass

# Records router -- built by another agent; import gracefully.
# NOTE: The records router defines paths like /api/collections/{...}/records/...
# which already include the /api prefix. We store a reference here; the app
# factory will mount it at the root level (not under /api).
try:
    from ppbase.api.records import router as records_router

    _records_router = records_router
except (ImportError, AttributeError):
    _records_router = None

# Record auth router -- auth endpoints for auth collections.
# Paths: /api/collections/{collection}/auth-with-password etc.
try:
    from ppbase.api.record_auth import router as record_auth_router

    _record_auth_router = record_auth_router
except (ImportError, AttributeError):
    _record_auth_router = None

# Realtime router -- SSE endpoints for real-time updates.
# Paths: /api/realtime (GET for connection, POST for subscriptions)
try:
    from ppbase.api.realtime import router as realtime_router

    _realtime_router = realtime_router
except (ImportError, AttributeError):
    _realtime_router = None
