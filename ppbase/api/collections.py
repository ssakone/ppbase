"""FastAPI routes for the Collections API.

All endpoints require superuser (admin) authentication.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ppbase.db.engine import get_async_session, get_engine
from ppbase.models.collection import (
    CollectionCreate,
    CollectionImportPayload,
    CollectionResponse,
    CollectionUpdate,
)
from ppbase.db.schema_manager import get_database_tables
from ppbase.services import collection_service

router = APIRouter(prefix="/collections", tags=["collections"])


# ---------------------------------------------------------------------------
# Admin auth dependency
# ---------------------------------------------------------------------------


async def require_admin(
    authorization: str | None = Header(default=None),
) -> str:
    """Simple admin auth dependency.

    For now, checks that an Authorization header is present and non-empty.
    Full JWT validation will be wired up when the auth module is ready.

    Returns the raw token string.
    """
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={
                "status": 401,
                "message": "The request requires admin authorization token to be set.",
                "data": {},
            },
        )
    # TODO: validate JWT and ensure admin role
    return authorization


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


async def _get_session() -> Any:
    """Yield an async session from the engine module."""
    async for session in get_async_session():
        yield session


def _get_engine() -> AsyncEngine:
    return get_engine()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_collections(
    page: int = Query(default=1, ge=1),
    perPage: int = Query(default=30, ge=1, le=500, alias="perPage"),
    sort: str = Query(default=""),
    filter: str = Query(default="", alias="filter"),
    session: AsyncSession = Depends(_get_session),
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """List all collections with pagination."""
    result = await collection_service.list_collections(
        session,
        page=page,
        per_page=perPage,
        sort=sort,
        filter_str=filter,
    )
    return result.model_dump()


@router.post("", status_code=200)
async def create_collection(
    data: CollectionCreate,
    session: AsyncSession = Depends(_get_session),
    engine: AsyncEngine = Depends(_get_engine),
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Create a new collection."""
    try:
        result = await collection_service.create_collection(session, engine, data)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": str(exc),
                "data": {},
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": str(exc),
                "data": {},
            },
        )
    return result.model_dump()


@router.get("/meta/tables")
async def list_database_tables(
    engine: AsyncEngine = Depends(_get_engine),
    _admin: str = Depends(require_admin),
) -> list[dict]:
    """Return all database tables and their columns for SQL editor autocomplete."""
    return await get_database_tables(engine)


@router.get("/{idOrName}")
async def view_collection(
    idOrName: str,
    session: AsyncSession = Depends(_get_session),
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """View a single collection by ID or name."""
    try:
        record = await collection_service.get_collection(session, idOrName)
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={
                "status": 404,
                "message": f"Collection '{idOrName}' not found.",
                "data": {},
            },
        )
    return CollectionResponse.from_record(record).model_dump()


@router.patch("/{idOrName}")
async def update_collection(
    idOrName: str,
    data: CollectionUpdate,
    session: AsyncSession = Depends(_get_session),
    engine: AsyncEngine = Depends(_get_engine),
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Update an existing collection."""
    try:
        result = await collection_service.update_collection(
            session, engine, idOrName, data
        )
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={
                "status": 404,
                "message": f"Collection '{idOrName}' not found.",
                "data": {},
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": str(exc),
                "data": {},
            },
        )
    return result.model_dump()


@router.delete("/{idOrName}", status_code=204)
async def delete_collection(
    idOrName: str,
    session: AsyncSession = Depends(_get_session),
    engine: AsyncEngine = Depends(_get_engine),
    _admin: str = Depends(require_admin),
) -> None:
    """Delete a collection."""
    try:
        await collection_service.delete_collection(session, engine, idOrName)
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={
                "status": 404,
                "message": f"Collection '{idOrName}' not found.",
                "data": {},
            },
        )
    except (ValueError, Exception) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": str(exc),
                "data": {},
            },
        )


@router.put("/import", status_code=204)
async def import_collections(
    payload: CollectionImportPayload,
    session: AsyncSession = Depends(_get_session),
    engine: AsyncEngine = Depends(_get_engine),
    _admin: str = Depends(require_admin),
) -> None:
    """Bulk import collections."""
    try:
        await collection_service.import_collections(
            session,
            engine,
            payload.collections,
            delete_missing=payload.deleteMissing,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": str(exc),
                "data": {},
            },
        )


@router.delete("/{idOrName}/truncate", status_code=204)
async def truncate_collection(
    idOrName: str,
    session: AsyncSession = Depends(_get_session),
    engine: AsyncEngine = Depends(_get_engine),
    _admin: str = Depends(require_admin),
) -> None:
    """Truncate all records in a collection."""
    try:
        await collection_service.truncate_collection(session, engine, idOrName)
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={
                "status": 404,
                "message": f"Collection '{idOrName}' not found.",
                "data": {},
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": str(exc),
                "data": {},
            },
        )
