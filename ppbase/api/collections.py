"""FastAPI routes for the Collections API.

All endpoints require superuser (admin) authentication.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
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


def _get_migration_kwargs(request: Request) -> dict[str, Any]:
    """Extract auto_migrate and migrations_dir from app settings."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return {"auto_migrate": False, "migrations_dir": None}
    return {
        "auto_migrate": getattr(settings, "auto_migrate", False),
        "migrations_dir": getattr(settings, "migrations_dir", None),
    }


def _parse_fields(fields_param: str | None) -> set[str] | None:
    """Parse ``fields`` query param into a normalized set."""
    if not fields_param:
        return None
    fields = {name.strip() for name in fields_param.split(",") if name.strip()}
    return fields or None


def _filter_item_fields(
    items: list[dict[str, Any]],
    fields_param: str | None,
) -> list[dict[str, Any]]:
    """Filter list item keys based on PocketBase ``fields`` semantics."""
    fields = _parse_fields(fields_param)
    if not fields or "*" in fields:
        return items
    return [{k: v for k, v in item.items() if k in fields} for item in items]


def _collection_scaffolds() -> dict[str, Any]:
    """Return PocketBase-like collection scaffolds for Dashboard usage."""
    auth = {
        "id": "",
        "name": "",
        "type": "auth",
        "system": False,
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            {
                "id": "text_id",
                "name": "id",
                "type": "text",
                "required": True,
                "system": True,
                "hidden": False,
                "presentable": False,
                "primaryKey": True,
                "autogeneratePattern": "[a-z0-9]{15}",
                "min": 15,
                "max": 15,
                "pattern": "^[a-z0-9]+$",
            },
            {
                "id": "password_field",
                "name": "password",
                "type": "password",
                "required": True,
                "system": True,
                "hidden": True,
                "presentable": False,
                "min": 8,
                "max": 0,
                "cost": 0,
                "pattern": "",
            },
            {
                "id": "token_key",
                "name": "tokenKey",
                "type": "text",
                "required": True,
                "system": True,
                "hidden": True,
                "presentable": False,
                "primaryKey": False,
                "autogeneratePattern": "[a-zA-Z0-9]{50}",
                "min": 30,
                "max": 60,
                "pattern": "",
            },
            {
                "id": "email_field",
                "name": "email",
                "type": "email",
                "required": True,
                "system": True,
                "hidden": False,
                "presentable": False,
                "onlyDomains": None,
                "exceptDomains": None,
            },
            {
                "id": "email_visibility",
                "name": "emailVisibility",
                "type": "bool",
                "required": False,
                "system": True,
                "hidden": False,
                "presentable": False,
            },
            {
                "id": "verified",
                "name": "verified",
                "type": "bool",
                "required": False,
                "system": True,
                "hidden": False,
                "presentable": False,
            },
        ],
        "indexes": [],
        "created": "",
        "updated": "",
        "authRule": "",
        "manageRule": None,
        "authAlert": {
            "enabled": True,
            "emailTemplate": {
                "subject": "Login from a new location",
                "body": "...",
            },
        },
        "oauth2": {
            "enabled": False,
            "providers": [],
            "mappedFields": {
                "id": "",
                "name": "",
                "username": "",
                "avatarURL": "",
            },
        },
        "passwordAuth": {
            "enabled": True,
            "identityFields": ["email"],
        },
        "mfa": {
            "enabled": False,
            "duration": 1800,
            "rule": "",
        },
        "otp": {
            "enabled": False,
            "duration": 180,
            "length": 8,
            "emailTemplate": {
                "subject": "OTP for {APP_NAME}",
                "body": "...",
            },
        },
        "authToken": {"duration": 604800},
        "passwordResetToken": {"duration": 1800},
        "emailChangeToken": {"duration": 1800},
        "verificationToken": {"duration": 259200},
        "fileToken": {"duration": 180},
        "verificationTemplate": {
            "subject": "Verify your {APP_NAME} email",
            "body": "...",
        },
        "resetPasswordTemplate": {
            "subject": "Reset your {APP_NAME} password",
            "body": "...",
        },
        "confirmEmailChangeTemplate": {
            "subject": "Confirm your {APP_NAME} new email address",
            "body": "...",
        },
    }

    base = {
        "id": "",
        "name": "",
        "type": "base",
        "system": False,
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            {
                "id": "text_id",
                "name": "id",
                "type": "text",
                "required": True,
                "system": True,
                "hidden": False,
                "presentable": False,
                "primaryKey": True,
                "autogeneratePattern": "[a-z0-9]{15}",
                "min": 15,
                "max": 15,
                "pattern": "^[a-z0-9]+$",
            }
        ],
        "indexes": [],
        "created": "",
        "updated": "",
    }

    view = {
        "id": "",
        "name": "",
        "type": "view",
        "system": False,
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [],
        "indexes": [],
        "created": "",
        "updated": "",
        "viewQuery": "",
    }

    return {
        "auth": auth,
        "base": base,
        "view": view,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_collections(
    page: int = Query(default=1, ge=1),
    perPage: int = Query(default=30, ge=1, le=500, alias="perPage"),
    sort: str = Query(default=""),
    filter: str = Query(default="", alias="filter"),
    fields: str | None = Query(default=None),
    skipTotal: bool = Query(default=False),
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
        skip_total=skipTotal,
    )
    payload = result.model_dump()
    payload["items"] = _filter_item_fields(payload.get("items", []), fields)
    return payload


@router.post("", status_code=200)
async def create_collection(
    request: Request,
    data: CollectionCreate,
    session: AsyncSession = Depends(_get_session),
    engine: AsyncEngine = Depends(_get_engine),
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Create a new collection."""
    try:
        result = await collection_service.create_collection(
            session, engine, data, **_get_migration_kwargs(request)
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


@router.get("/meta/scaffolds")
async def get_collection_scaffolds(
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Return collection type scaffolds used by PocketBase dashboard clients."""
    return _collection_scaffolds()


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
    request: Request,
    idOrName: str,
    data: CollectionUpdate,
    session: AsyncSession = Depends(_get_session),
    engine: AsyncEngine = Depends(_get_engine),
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Update an existing collection."""
    try:
        result = await collection_service.update_collection(
            session, engine, idOrName, data, **_get_migration_kwargs(request)
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
    request: Request,
    idOrName: str,
    session: AsyncSession = Depends(_get_session),
    engine: AsyncEngine = Depends(_get_engine),
    _admin: str = Depends(require_admin),
) -> None:
    """Delete a collection."""
    try:
        await collection_service.delete_collection(
            session, engine, idOrName, **_get_migration_kwargs(request)
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
    request: Request,
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
            **_get_migration_kwargs(request),
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


@router.post("/{idOrName}/truncate", status_code=204)
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
