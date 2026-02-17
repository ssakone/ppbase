"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ppbase.config import Settings

logger = logging.getLogger(__name__)


def _handle_db_connection_error(database_url: str, exc: BaseException) -> None:
    """Print a clear message when PostgreSQL is unreachable and exit."""
    import re
    # Mask password in URL for display
    safe_url = re.sub(r"://([^:]*):([^@]*)@", r"://\1:****@", database_url)

    msg = (
        "\n"
        "PostgreSQL is not reachable. PPBase cannot start without a database.\n"
        "\n"
        "  Database URL: %s\n"
        "\n"
        "  Make sure PostgreSQL is running, then try:\n"
        "    python -m ppbase db start   # start PostgreSQL via Docker\n"
        "\n"
        "  Or set PPBASE_DATABASE_URL if using a different database.\n"
    ) % safe_url
    print(msg, file=sys.stderr)
    os._exit(1)  # Exit immediately without raising (avoids traceback)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Manage startup and shutdown of the database engine."""
    import asyncio
    from ppbase.db.engine import init_engine, close_engine
    from ppbase.db.system_tables import create_system_tables
    from ppbase.services.realtime_service import SubscriptionManager, listen_for_db_events

    settings: Settings = app.state.settings

    # Startup: create engine and ensure system tables exist
    try:
        engine = await init_engine(
            settings.database_url,
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            echo=settings.dev,
        )
        await create_system_tables(engine)
    except (ConnectionRefusedError, OSError) as exc:
        _handle_db_connection_error(settings.database_url, exc)

    # Initialize realtime subscription manager
    subscription_manager = SubscriptionManager()
    app.state.subscription_manager = subscription_manager

    # Start PostgreSQL LISTEN task for realtime events
    listen_task = asyncio.create_task(
        listen_for_db_events(engine, subscription_manager)
    )
    logger.info("Started PostgreSQL LISTEN task for realtime events")

    # Bootstrap all system collections and backfill auth options
    from ppbase.db.bootstrap import bootstrap_system_collections
    from ppbase.services.auth_service import generate_default_auth_options
    from sqlalchemy.ext.asyncio import AsyncSession as _AS, async_sessionmaker as _asm

    _boot_factory = _asm(bind=engine, class_=_AS, expire_on_commit=False)
    async with _boot_factory() as _boot_session:
        async with _boot_session.begin():
            await bootstrap_system_collections(_boot_session, engine)

            # Backfill auth options for existing auth collections
            from ppbase.db.system_tables import CollectionRecord
            from sqlalchemy import select

            stmt = select(CollectionRecord).where(CollectionRecord.type == "auth")
            auth_colls = (await _boot_session.execute(stmt)).scalars().all()
            for coll in auth_colls:
                opts = coll.options or {}
                if not opts.get("authToken"):
                    is_su = (coll.name == "_superusers")
                    coll.options = generate_default_auth_options(is_superusers=is_su)
                    await _boot_session.flush()

    # Apply pending migrations if auto_migrate is enabled
    if settings.auto_migrate:
        from ppbase.services.migration_runner import apply_all_pending
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        migrations_dir = settings.migrations_dir
        os.makedirs(migrations_dir, exist_ok=True)

        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with session_factory() as session:
            try:
                async with session.begin():
                    applied = await apply_all_pending(session, engine, migrations_dir)
                if applied:
                    logger.info(
                        "Applied %d pending migration(s) on startup", len(applied)
                    )
                else:
                    logger.debug("No pending migrations to apply")
            except Exception as exc:
                logger.error(
                    "Migration failed during startup: %s", exc
                )
                raise
    else:
        logger.info("Auto-migrate disabled, skipping migration check")

    # Check if any admin exists — if not, print setup URL
    from ppbase.services.admin_service import count_admins
    from sqlalchemy.ext.asyncio import AsyncSession as _CheckAS, async_sessionmaker as _check_asm

    _check_factory = _check_asm(bind=engine, class_=_CheckAS, expire_on_commit=False)
    async with _check_factory() as _check_session:
        admin_count = await count_admins(_check_session)

    if admin_count == 0:
        display_host = settings.host if settings.host != "0.0.0.0" else "127.0.0.1"
        setup_url = f"http://{display_host}:{settings.port}/_/"
        print(
            "\n"
            "No admin account found.\n"
            f"Open {setup_url} to set up your first admin.\n"
        )

    yield

    # Shutdown: cancel LISTEN task and dispose of the connection pool
    logger.info("Shutting down PostgreSQL LISTEN task")
    listen_task.cancel()
    try:
        await listen_task
    except asyncio.CancelledError:
        pass

    await close_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        settings: Optional ``Settings`` instance.  If not provided a
            default ``Settings()`` is created (reads env / .env).

    Returns:
        A fully configured :class:`FastAPI` application.
    """
    if settings is None:
        settings = Settings()

    app = FastAPI(
        title="PPBase",
        version="0.1.0",
        docs_url="/api/docs" if settings.dev else None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # Attach settings to app state so dependencies can access them
    app.state.settings = settings

    # CORS
    from ppbase.middleware.cors import setup_cors

    setup_cors(app, settings.origins)

    # API routes
    from ppbase.api.router import (
        api_router,
        _records_router,
        _record_auth_router,
        _realtime_router,
    )

    app.include_router(api_router, prefix="/api")

    # The records router uses full paths like /api/collections/{...}/records
    # so it must be included at the root level.
    if _records_router is not None:
        app.include_router(_records_router, tags=["records"])

    # The record auth router uses full paths like /api/collections/{...}/auth-*
    # so it must also be included at the root level.
    if _record_auth_router is not None:
        app.include_router(_record_auth_router, tags=["record-auth"])

    # The realtime router uses full paths like /api/realtime
    # so it must also be included at the root level.
    if _realtime_router is not None:
        app.include_router(_realtime_router, tags=["realtime"])

    # Admin UI - serve static files from ppbase/admin/dist/
    import pathlib

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    admin_dist = pathlib.Path(__file__).parent / "admin" / "dist"
    if admin_dist.is_dir():
        # Serve static assets (css/, js/)
        app.mount(
            "/_/assets",
            StaticFiles(directory=str(admin_dist)),
            name="admin-assets",
        )

        # Serve admin SPA at /_/ (all routes fall back to index.html)
        @app.get("/_/{rest_of_path:path}", include_in_schema=False)
        async def _admin_spa(rest_of_path: str = "") -> FileResponse:
            index = admin_dist / "index.html"
            if index.exists():
                return FileResponse(str(index))
            return JSONResponse(
                {"message": "Admin UI not built"}, status_code=404
            )

        # Redirect root /_/ access
        @app.get("/_", include_in_schema=False)
        async def _admin_redirect():
            from fastapi.responses import RedirectResponse

            return RedirectResponse("/_/")

    # Custom exception handler: PocketBase returns flat error objects, not
    # FastAPI's default {"detail": ...} wrapper.
    @app.exception_handler(HTTPException)
    async def _pocketbase_error_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "status" in detail:
            # Already a PocketBase-format error dict
            return JSONResponse(
                content=detail,
                status_code=exc.status_code,
            )
        # Fallback: wrap plain string errors
        return JSONResponse(
            content={
                "status": exc.status_code,
                "message": str(detail) if detail else "",
                "data": {},
            },
            status_code=exc.status_code,
        )

    # Handle Pydantic request validation errors in PocketBase format.
    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        data: dict = {}
        for err in exc.errors():
            loc = err.get("loc", ())
            # Field name is typically the last element after "body"
            field_name = str(loc[-1]) if loc else "unknown"
            data[field_name] = {
                "code": "validation_required",
                "message": err.get("msg", "Invalid value."),
            }
        return JSONResponse(
            content={
                "status": 400,
                "message": "Something went wrong while processing your request.",
                "data": data,
            },
            status_code=400,
        )

    return app
