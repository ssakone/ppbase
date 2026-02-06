"""PPBase -- a Python reimplementation of PocketBase using PostgreSQL.

Usage as library::

    from ppbase import PPBase

    pb = PPBase(database_url="postgresql+asyncpg://localhost/mydb")
    fastapi_app = pb.get_app()
    # Or run directly:
    # pb.start()

Usage as standalone server::

    ppbase serve --http 0.0.0.0:8090 --db postgresql://...
"""

from __future__ import annotations

__version__ = "0.1.0"

from ppbase.config import Settings


class PPBase:
    """Main application facade.

    Owns configuration, the database engine, and the FastAPI application.
    Can be used as a standalone server or embedded inside another FastAPI app.
    """

    def __init__(
        self,
        database_url: str | None = None,
        data_dir: str | None = None,
        dev: bool | None = None,
        origins: list[str] | None = None,
        **overrides: object,
    ) -> None:
        # Only pass explicitly provided values to Settings so that
        # env vars / .env are not overridden by defaults.
        kwargs: dict[str, object] = {**overrides}
        if database_url is not None:
            kwargs["database_url"] = database_url
        if data_dir is not None:
            kwargs["data_dir"] = data_dir
        if dev is not None:
            kwargs["dev"] = dev
        if origins is not None:
            kwargs["origins"] = origins
        self.settings = Settings(**kwargs)  # type: ignore[arg-type]
        self._app = None

    def get_app(self):
        """Build and return the FastAPI application instance.

        The app is created lazily and cached for the lifetime of this object.
        """
        if self._app is None:
            from ppbase.app import create_app

            self._app = create_app(self.settings)
        return self._app

    def start(
        self,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        """Start the server with uvicorn (blocking)."""
        import uvicorn

        app = self.get_app()
        uvicorn.run(
            app,
            host=host or self.settings.host,
            port=port or self.settings.port,
            log_level=self.settings.log_level.lower(),
        )
