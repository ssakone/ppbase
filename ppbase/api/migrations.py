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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ppbase.api.deps import get_session, require_admin
from ppbase.db.engine import get_engine
from ppbase.db.system_tables import CollectionRecord, MigrationRecord

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

    return {
        "items": items,
        "totalItems": len(items),
    }


@router.post("/apply")
async def apply_migrations(
    request: Request,
    session: AsyncSession = Depends(get_session),
    engine: AsyncEngine = Depends(_dep_engine),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Apply all pending migrations."""
    from ppbase.services.migration_runner import apply_all_pending

    migrations_dir = _get_migrations_dir(request)

    try:
        applied = await apply_all_pending(session, engine, migrations_dir)
        await session.commit()
    except Exception as exc:
        await session.rollback()
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
    from ppbase.services.migration_runner import (
        get_applied_migrations,
        revert_migration,
    )

    count = body.count if body else 1
    migrations_dir = _get_migrations_dir(request)

    try:
        # Get applied migrations in reverse order (most recent first)
        applied_records = await get_applied_migrations(session)
        # Sort by applied timestamp descending to revert most recent first
        applied_records.sort(key=lambda r: r.applied, reverse=True)

        to_revert = applied_records[:count]
        reverted = []

        for record in to_revert:
            await revert_migration(session, engine, record.file, migrations_dir)
            reverted.append(record.file)

        await session.commit()
    except Exception as exc:
        await session.rollback()
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
        "total": status["total"],
        "lastApplied": last_applied,
    }


@router.post("/snapshot")
async def generate_snapshot(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Generate snapshot migrations for the current collection state."""
    from ppbase.services.migration_generator import generate_create_migration

    migrations_dir = _get_migrations_dir(request)

    # Ensure migrations directory exists
    migrations_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Fetch all collections
        result = await session.execute(
            select(CollectionRecord).order_by(CollectionRecord.name)
        )
        collections = result.scalars().all()

        generated = []
        for collection in collections:
            filepath = generate_create_migration(collection, migrations_dir)
            filename = Path(filepath).name
            generated.append(filename)
            # Record as applied so it doesn't re-run on startup
            record = MigrationRecord(file=filename)
            session.add(record)

        await session.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": f"Failed to generate snapshot: {exc}",
                "data": {},
            },
        )

    return {
        "generated": generated,
        "count": len(generated),
    }
