"""FastAPI routes for the Migrations API.

Endpoints:
    GET    /api/migrations          -- list all migrations (applied + pending)
    POST   /api/migrations/apply    -- apply all pending migrations
    POST   /api/migrations/revert   -- revert last migration(s)
    GET    /api/migrations/status   -- migration status summary
    POST   /api/migrations/snapshot -- generate snapshot migrations

All endpoints require admin authentication.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ppbase.api.deps import get_session, require_admin
from ppbase.db.engine import get_engine
from ppbase.db.system_tables import MigrationRecord

router = APIRouter(prefix="/migrations", tags=["migrations"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class RevertBody(BaseModel):
    count: int = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_migrations_dir(request: Request) -> Path:
    """Resolve the migrations directory from app settings."""
    settings = request.app.state.settings
    migrations_dir = getattr(settings, "migrations_dir", "./pb_migrations")
    return Path(migrations_dir).resolve()


def _dep_engine() -> AsyncEngine:
    return get_engine()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_migrations(
    request: Request,
    page: int = Query(default=1, ge=1),
    perPage: int = Query(default=30, ge=1, le=200, alias="perPage"),
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """List all migrations with their applied status."""
    from ppbase.services.migration_runner import (
        _list_migration_files,
        get_applied_migrations,
    )

    migrations_dir = _get_migrations_dir(request)
    migration_files = _list_migration_files(migrations_dir)

    # Get applied migrations from DB
    applied_records = await get_applied_migrations(session)
    applied_map = {r.file: r.applied for r in applied_records}

    items = []
    seen = set()
    for filename in migration_files:
        applied_at = applied_map.get(filename)
        items.append({
            "file": filename,
            "applied": applied_at.isoformat() if applied_at else None,
            "status": "applied" if applied_at else "pending",
        })
        seen.add(filename)

    # Include applied records that no longer have files on disk (orphaned)
    for filename, applied_at in applied_map.items():
        if filename not in seen:
            items.append({
                "file": filename,
                "applied": applied_at.isoformat() if applied_at else None,
                "status": "applied",
            })

    items.sort(key=lambda x: x["file"])

    total_items = len(items)
    total_pages = max(1, (total_items + perPage - 1) // perPage)
    start = (page - 1) * perPage
    end = start + perPage

    return {
        "page": page,
        "perPage": perPage,
        "totalItems": total_items,
        "totalPages": total_pages,
        "items": items[start:end],
    }


@router.post("/apply")
async def apply_migrations(
    request: Request,
    session: AsyncSession = Depends(get_session),
    engine: AsyncEngine = Depends(_dep_engine),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Apply all pending migrations."""
    from ppbase.services.database_preparation import prepare_database

    migrations_dir = _get_migrations_dir(request)
    settings = request.app.state.settings

    try:
        # Admin authentication used this request session for read-only work.
        # End that transaction before reserving the migration connection so a
        # pool configured with a single connection cannot deadlock itself.
        await session.rollback()
        applied = await prepare_database(
            engine,
            migrations_dir,
            apply_migrations=True,
            lock_timeout_seconds=getattr(settings, "migration_lock_timeout", 30.0),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": f"Failed to apply migrations: {exc}",
                "data": {},
            },
        )

    return {
        "applied": applied,
        "count": len(applied),
    }


@router.post("/revert")
async def revert_migrations(
    request: Request,
    body: RevertBody | None = None,
    session: AsyncSession = Depends(get_session),
    engine: AsyncEngine = Depends(_dep_engine),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Revert the last N applied migration(s)."""
    from ppbase.services.database_preparation import prepare_database_and_revert

    count = body.count if body else 1
    migrations_dir = _get_migrations_dir(request)
    settings = request.app.state.settings

    try:
        await session.rollback()
        reverted = await prepare_database_and_revert(
            engine,
            migrations_dir,
            count=count,
            lock_timeout_seconds=getattr(settings, "migration_lock_timeout", 30.0),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": f"Failed to revert migrations: {exc}",
                "data": {},
            },
        )

    return {
        "reverted": reverted,
        "count": len(reverted),
    }


@router.get("/status")
async def migration_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Return a summary of migration status."""
    from ppbase.services.migration_runner import get_migration_status

    migrations_dir = _get_migrations_dir(request)

    status = await get_migration_status(session, migrations_dir)

    # Find the last applied migration timestamp
    last_applied = None
    if status["applied"]:
        # applied is a list of filenames; query DB for timestamp of last one
        result = await session.execute(
            select(MigrationRecord)
            .order_by(MigrationRecord.applied.desc())
            .limit(1)
        )
        last_record = result.scalars().first()
        if last_record:
            last_applied = last_record.applied.isoformat()

    return {
        "applied": status["applied_count"],
        "pending": status["pending_count"],
        "orphaned": status["orphaned_count"],
        "total": status["total"],
        "initialized": status["initialized"],
        "lastApplied": last_applied,
    }


@router.post("/snapshot")
async def generate_snapshot(
    request: Request,
    session: AsyncSession = Depends(get_session),
    engine: AsyncEngine = Depends(_dep_engine),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Generate one sanitized snapshot for the current collection state."""
    from ppbase.services.migration_snapshot import create_migration_snapshot

    migrations_dir = _get_migrations_dir(request)
    settings = request.app.state.settings

    try:
        await session.rollback()
        filepath = await create_migration_snapshot(
            engine,
            migrations_dir,
            lock_timeout_seconds=getattr(settings, "migration_lock_timeout", 30.0),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": f"Failed to generate snapshot: {exc}",
                "data": {},
            },
        )

    return {"generated": [Path(filepath).name], "count": 1}
