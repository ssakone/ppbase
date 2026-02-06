"""Main API router that aggregates all sub-routers."""

from __future__ import annotations

from fastapi import APIRouter

from ppbase.api.health import router as health_router
from ppbase.api.admins import router as admins_router
from ppbase.api.settings import router as settings_router
from ppbase.api.files import router as files_router

api_router = APIRouter()

# Always-available routes
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(admins_router, prefix="/admins", tags=["admins"])
api_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_router.include_router(files_router, prefix="/files", tags=["files"])

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

# Records router -- built by another agent; import gracefully.
# NOTE: The records router defines paths like /api/collections/{...}/records/...
# which already include the /api prefix. We store a reference here; the app
# factory will mount it at the root level (not under /api).
try:
    from ppbase.api.records import router as records_router

    _records_router = records_router
except (ImportError, AttributeError):
    _records_router = None
