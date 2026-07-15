"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ppbase import __version__
from ppbase.config import Settings

if TYPE_CHECKING:
    from ppbase.ext.registry import ExtensionRegistry
    from ppbase.services.hook_manager import HookManager

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
    from ppbase.db.system_tables import ParamRecord
    from ppbase.ext.events import BootstrapEvent, ServeEvent, TerminateEvent
    from ppbase.ext.registry import HOOK_BOOTSTRAP, HOOK_SERVE, HOOK_TERMINATE
    from ppbase.services.database_preparation import prepare_database
    from ppbase.services.file_storage import (
        configure_storage_runtime_from_settings_payload,
    )
    from ppbase.services.process_control import schedule_process_restart
    from ppbase.services.realtime_service import SubscriptionManager, listen_for_db_events
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    settings: Settings = app.state.settings
    extensions = getattr(app.state, "extension_registry", None)

    if not settings.jwt_secret and not settings.dev:
        logger.warning(
            "PPBASE_JWT_SECRET is not set; PPBase will use a project-local generated "
            "secret from data_dir/.jwt_secret."
        )

    if extensions is not None:
        await extensions.hooks.get(HOOK_BOOTSTRAP).trigger(
            BootstrapEvent(app=app, settings=settings),
        )

    engine = None
    listen_task = None
    hooks_watcher_task = None
    serve_started = False

    # Startup preparation happens before LISTEN, hooks/jobs, and the lifespan
    # yield that opens the app to HTTP traffic.
    try:
        try:
            engine = await init_engine(
                settings.database_url,
                pool_size=settings.pool_size,
                max_overflow=settings.max_overflow,
                echo=settings.dev,
            )
            # Force the lazy SQLAlchemy engine to establish its initial
            # PostgreSQL connection while the friendly startup error handler
            # is still in scope. Migration code runs outside this handler so
            # its own FileNotFoundError/PermissionError/OSError keeps the real
            # exception type and traceback.
            async with engine.connect():
                pass
        except (ConnectionRefusedError, OSError) as exc:
            _handle_db_connection_error(settings.database_url, exc)

        applied = await prepare_database(
            engine,
            settings.migrations_dir,
            apply_migrations=settings.should_apply_migrations(),
            lock_timeout_seconds=settings.migration_lock_timeout,
        )

        if settings.should_apply_migrations():
            if applied:
                logger.info(
                    "Applied %d pending migration(s) on startup", len(applied)
                )
            else:
                logger.debug("No pending migrations to apply")
        else:
            logger.info("Startup migration application disabled")

        session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Load runtime settings only after migrations have completed.
        async with session_factory() as session:
            settings_stmt = select(ParamRecord.value).where(
                ParamRecord.key == "settings"
            )
            settings_row = (await session.execute(settings_stmt)).scalar_one_or_none()
            configure_storage_runtime_from_settings_payload(
                settings_row if isinstance(settings_row, dict) else None
            )

        # Check if any admin exists — if not, generate the one-time setup URL.
        from ppbase.services.admin_service import count_admins
        from ppbase.services.setup_service import get_or_create_setup_token

        async with session_factory() as session:
            admin_count = await count_admins(session)
            setup_token = None
            if admin_count == 0:
                setup_token = await get_or_create_setup_token(session)
                await session.commit()

        if admin_count == 0 and setup_token:
            display_host = (
                settings.host if settings.host != "0.0.0.0" else "127.0.0.1"
            )
            setup_url = (
                f"http://{display_host}:{settings.port}/_/setup?token={setup_token}"
            )
            print(
                "\n"
                "  No admin account found. Create your first admin:\n"
                "\n"
                f"  {setup_url}\n"
                "\n"
                "  This is a one-time link — open it in your browser.\n"
            )
            try:
                import webbrowser

                if sys.stdin.isatty():
                    webbrowser.open(setup_url)
            except Exception:
                pass

        # Restore hook runtime state after the database is fully migrated.
        hook_manager = getattr(app.state, "hook_manager", None)
        if hook_manager is not None:
            async with session_factory() as session:
                stmt = select(ParamRecord.value).where(
                    ParamRecord.key == "hooks_runtime"
                )
                hooks_runtime = (await session.execute(stmt)).scalar_one_or_none()
                if isinstance(hooks_runtime, dict):
                    disabled = set(hooks_runtime.get("disabled", []))
                    hook_manager.set_auto_restart_on_change(
                        bool(hooks_runtime.get("autoRestartOnChange", False))
                    )
                    for hook_id in disabled:
                        hook_manager.disable_hook(hook_id)
                    if disabled:
                        logger.info(
                            "Restored %d disabled hook(s) from settings",
                            len(disabled),
                        )

        # Realtime starts only after bootstrap and migrations succeed.
        subscription_manager = SubscriptionManager(extension_registry=extensions)
        app.state.subscription_manager = subscription_manager
        listen_task = asyncio.create_task(
            listen_for_db_events(engine, subscription_manager)
        )
        logger.info("Started PostgreSQL LISTEN task for realtime events")

        if extensions is not None:
            await extensions.hooks.get(HOOK_SERVE).trigger(
                ServeEvent(app=app, settings=settings),
            )
        serve_started = True

        # Start hooks directory watcher task (polls every 2 seconds).
        if hook_manager is not None:
            async def _hooks_watcher() -> None:
                while True:
                    await asyncio.sleep(2)
                    try:
                        reloaded = hook_manager.rescan()
                        if reloaded:
                            logger.info(
                                "Hot-reloaded hook file(s): %s", ", ".join(reloaded)
                            )
                            if hook_manager.get_auto_restart_on_change():
                                schedule_process_restart(
                                    "Detected hook file changes: "
                                    f"{', '.join(reloaded)}"
                                )
                    except Exception as exc:
                        logger.error("Hook watcher error: %s", exc)

            hooks_watcher_task = asyncio.create_task(_hooks_watcher())
            logger.info("Started pb_hooks/ watcher task (polling every 2s)")

        yield
    finally:
        if serve_started and extensions is not None:
            try:
                await extensions.hooks.get(HOOK_TERMINATE).trigger(
                    TerminateEvent(app=app, settings=settings),
                )
            except Exception:
                logger.exception("Terminate hook failed")

        if hooks_watcher_task is not None:
            hooks_watcher_task.cancel()
            try:
                await hooks_watcher_task
            except asyncio.CancelledError:
                pass

        if listen_task is not None:
            logger.info("Shutting down PostgreSQL LISTEN task")
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

        if engine is not None:
            await close_engine()


def create_app(
    settings: Settings | None = None,
    *,
    extensions: "ExtensionRegistry | None" = None,
    hook_manager: "HookManager | None" = None,
) -> FastAPI:
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
        version=__version__,
        docs_url="/api/docs" if settings.dev else None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # Attach settings to app state so dependencies can access them
    app.state.settings = settings
    app.state.extension_registry = extensions
    app.state.hook_manager = hook_manager
    app.state.rate_limit_settings_version = 0

    if extensions is not None:
        http_middlewares = tuple(extensions.get_http_middlewares())
    else:
        http_middlewares = ()

    if http_middlewares:
        import inspect

        async def _invoke_handler(
            handler: Any,
            request: Request,
            call_next: Any,
        ) -> Response:
            result = handler(request, call_next)
            if inspect.isawaitable(result):
                return await result
            return result

        @app.middleware("http")
        async def _ppbase_http_middleware(request: Request, call_next):
            async def _run_http(index: int) -> Response:
                if index >= len(http_middlewares):
                    return await call_next(request)

                binding = http_middlewares[index]
                if not binding.matches(request):
                    return await _run_http(index + 1)

                async def _next() -> Response:
                    return await _run_http(index + 1)

                return await _invoke_handler(binding.handler, request, _next)

            return await _run_http(0)

    from ppbase.services.file_storage import set_storage_settings

    set_storage_settings(settings)

    # CORS
    from ppbase.middleware.cors import setup_cors

    setup_cors(app, settings.origins)

    # Request logger
    from ppbase.middleware.request_logger import RequestLoggerMiddleware
    from ppbase.middleware.rate_limit import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestLoggerMiddleware)

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

    if extensions is not None:
        extensions.mount_routes(app)

    # Admin UI - serve static files from ppbase/admin/dist/
    import pathlib

    from fastapi.responses import FileResponse, Response
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

    # Public directory (PocketBase-like): files are served at /
    # without directory listing; "/" serves index.html when present.
    public_dir = str(settings.public_dir or "").strip()
    if public_dir:
        public_root = pathlib.Path(public_dir).expanduser().resolve()

        @app.get("/", include_in_schema=False)
        async def _public_index() -> Response:
            index = public_root / "index.html"
            if index.is_file():
                return FileResponse(str(index))
            return Response(status_code=404)

        @app.get("/{public_path:path}", include_in_schema=False)
        async def _public_file(public_path: str) -> Response:
            normalized = public_path.strip("/")
            if not normalized:
                return Response(status_code=404)
            if (
                normalized in {"api", "_"}
                or normalized.startswith("api/")
                or normalized.startswith("_/")
            ):
                raise HTTPException(status_code=404, detail="Not Found")

            candidate = (public_root / normalized).resolve()
            try:
                candidate.relative_to(public_root)
            except ValueError:
                return Response(status_code=404)

            if candidate.is_file():
                return FileResponse(str(candidate))

            # Resolve directory requests like /dashboard/ to /dashboard/index.html
            if candidate.is_dir():
                index = candidate / "index.html"
                if index.is_file():
                    return FileResponse(str(index))

            return Response(status_code=404)

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
