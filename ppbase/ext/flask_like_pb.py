"""Flask-like PPBase facade with decorators and hook registration."""

from __future__ import annotations

from typing import Any, Callable

from ppbase.config import Settings
from ppbase.ext.loading import load_hook_target
from ppbase.ext.registry import (
    ExtensionRegistry,
    HOOK_BOOTSTRAP,
    HOOK_REALTIME_CONNECT_REQUEST,
    HOOK_REALTIME_MESSAGE_SEND,
    HOOK_REALTIME_SUBSCRIBE_REQUEST,
    HOOK_RECORD_AUTH_REFRESH_REQUEST,
    HOOK_RECORD_AUTH_REQUEST,
    HOOK_RECORD_AUTH_WITH_OAUTH2_REQUEST,
    HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
    HOOK_RECORD_CREATE_REQUEST,
    HOOK_RECORD_DELETE_REQUEST,
    HOOK_RECORDS_LIST_REQUEST,
    HOOK_RECORD_UPDATE_REQUEST,
    HOOK_RECORD_VIEW_REQUEST,
    HOOK_SERVE,
    HOOK_TERMINATE,
)

RouteHandler = Callable[..., Any]
HookHandler = Callable[..., Any]


class FlaskLikePB:
    """Main app facade with Flask-like decorators and hook registration."""

    def __init__(
        self,
        database_url: str | None = None,
        data_dir: str | None = None,
        dev: bool | None = None,
        origins: list[str] | None = None,
        **overrides: object,
    ) -> None:
        kwargs: dict[str, object] = {**overrides}
        if database_url is not None:
            kwargs["database_url"] = database_url
        if data_dir is not None:
            kwargs["data_dir"] = data_dir
        if dev is not None:
            kwargs["dev"] = dev
        if origins is not None:
            kwargs["origins"] = origins

        self._settings_kwargs = kwargs
        self.settings = Settings(**kwargs)  # type: ignore[arg-type]
        self._app = None
        self._extensions = ExtensionRegistry()

    def configure(self, **settings_overrides: object) -> FlaskLikePB:
        """Apply settings overrides before app materialization."""
        if self._app is not None:
            raise RuntimeError("Cannot configure after app materialization.")
        self._settings_kwargs.update(settings_overrides)
        self.settings = Settings(**self._settings_kwargs)  # type: ignore[arg-type]
        return self

    def get_app(self):
        """Build and return the FastAPI app instance."""
        if self._app is None:
            from ppbase.app import create_app

            self._extensions.freeze()
            self._app = create_app(self.settings, extensions=self._extensions)
        return self._app

    def start(self, host: str | None = None, port: int | None = None) -> None:
        """Start the server with uvicorn (blocking)."""
        import uvicorn

        app = self.get_app()
        uvicorn.run(
            app,
            host=host or self.settings.host,
            port=port or self.settings.port,
            log_level=self.settings.log_level.lower(),
        )

    def route(
        self,
        path: str,
        methods: list[str] | tuple[str, ...] | None = None,
        **fastapi_kwargs: Any,
    ) -> Callable[[RouteHandler], RouteHandler]:
        """Register a route handler."""
        route_methods = list(methods) if methods else ["GET"]

        def decorator(func: RouteHandler) -> RouteHandler:
            self._extensions.add_route(path, route_methods, func, **fastapi_kwargs)
            return func

        return decorator

    def get(self, path: str, **fastapi_kwargs: Any):
        return self.route(path, methods=["GET"], **fastapi_kwargs)

    def post(self, path: str, **fastapi_kwargs: Any):
        return self.route(path, methods=["POST"], **fastapi_kwargs)

    def put(self, path: str, **fastapi_kwargs: Any):
        return self.route(path, methods=["PUT"], **fastapi_kwargs)

    def patch(self, path: str, **fastapi_kwargs: Any):
        return self.route(path, methods=["PATCH"], **fastapi_kwargs)

    def delete(self, path: str, **fastapi_kwargs: Any):
        return self.route(path, methods=["DELETE"], **fastapi_kwargs)

    def options(self, path: str, **fastapi_kwargs: Any):
        return self.route(path, methods=["OPTIONS"], **fastapi_kwargs)

    def head(self, path: str, **fastapi_kwargs: Any):
        return self.route(path, methods=["HEAD"], **fastapi_kwargs)

    def _hook_decorator(
        self,
        hook_name: str,
        *,
        collections: tuple[str, ...] = (),
        id: str | None = None,
        priority: int = 0,
    ) -> Callable[[HookHandler], HookHandler]:
        hook = self._extensions.get_hook(hook_name)
        normalized_collections = {value for value in collections if value}

        def _collection_predicate(event: Any) -> bool:
            if not normalized_collections:
                return True
            collection = getattr(event, "collection", None)
            coll_name = str(getattr(collection, "name", "")) if collection else ""
            coll_id = str(getattr(collection, "id", "")) if collection else ""
            requested = str(getattr(event, "collection_id_or_name", ""))
            return (
                coll_name in normalized_collections
                or coll_id in normalized_collections
                or requested in normalized_collections
            )

        def decorator(func: HookHandler) -> HookHandler:
            hook.bind_func(
                func,
                id=id,
                priority=priority,
                predicate=_collection_predicate,
            )
            return func

        return decorator

    def on_bootstrap(self, *, id: str | None = None, priority: int = 0):
        return self._hook_decorator(HOOK_BOOTSTRAP, id=id, priority=priority)

    def on_serve(self, *, id: str | None = None, priority: int = 0):
        return self._hook_decorator(HOOK_SERVE, id=id, priority=priority)

    def on_terminate(self, *, id: str | None = None, priority: int = 0):
        return self._hook_decorator(HOOK_TERMINATE, id=id, priority=priority)

    def on_records_list_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORDS_LIST_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_record_view_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_VIEW_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_record_create_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_CREATE_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_record_update_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_UPDATE_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_record_delete_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_DELETE_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_record_auth_with_password_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_record_auth_with_oauth2_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_AUTH_WITH_OAUTH2_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_record_auth_refresh_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_AUTH_REFRESH_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_record_auth_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_AUTH_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_realtime_connect_request(self, *, id: str | None = None, priority: int = 0):
        return self._hook_decorator(
            HOOK_REALTIME_CONNECT_REQUEST,
            id=id,
            priority=priority,
        )

    def on_realtime_subscribe_request(self, *, id: str | None = None, priority: int = 0):
        return self._hook_decorator(
            HOOK_REALTIME_SUBSCRIBE_REQUEST,
            id=id,
            priority=priority,
        )

    def on_realtime_message_send(self, *, id: str | None = None, priority: int = 0):
        return self._hook_decorator(
            HOOK_REALTIME_MESSAGE_SEND,
            id=id,
            priority=priority,
        )

    def load_hooks(self, target: str) -> FlaskLikePB:
        load_hook_target(target, self)
        return self

    def _reset_for_tests(self) -> None:
        """Reset singleton state between tests."""
        self._app = None
        self._settings_kwargs = {}
        self.settings = Settings()
        self._extensions = ExtensionRegistry()
