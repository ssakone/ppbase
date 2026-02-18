"""Flask-like PPBase facade with decorators and hook registration."""

from __future__ import annotations

import fnmatch
from typing import Any, Callable, Sequence

from fastapi import Depends, Request

from ppbase.api.deps import (
    get_optional_auth,
    require_admin,
    require_auth,
    require_record_auth,
)
from ppbase.config import Settings
from ppbase.ext.hooks import HookBinding
from ppbase.ext.loading import load_hook_target
from ppbase.ext.events import ROUTE_REQUEST_STORE_ATTR
from ppbase.ext.record_repository import RecordRepository
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
    HOOK_RECORD_AUTH_WITH_OTP_REQUEST,
    HOOK_RECORD_CREATE_REQUEST,
    HOOK_RECORD_DELETE_REQUEST,
    HOOK_RECORD_REQUEST_OTP_REQUEST,
    HOOK_RECORDS_LIST_REQUEST,
    HOOK_RECORD_UPDATE_REQUEST,
    HOOK_RECORD_VIEW_REQUEST,
    HOOK_FILE_DOWNLOAD_REQUEST,
    HOOK_FILE_TOKEN_REQUEST,
    HOOK_SERVE,
    HOOK_TERMINATE,
)

RouteHandler = Callable[..., Any]
HookHandler = Callable[..., Any]
MiddlewareHandler = Callable[..., Any]
RouteMiddleware = MiddlewareHandler | HookBinding


def _join_paths(prefix: str, path: str) -> str:
    normalized_prefix = "/" + str(prefix or "").strip("/")
    normalized_path = "/" + str(path or "").strip("/")
    if normalized_prefix == "/":
        return normalized_path
    if normalized_path == "/":
        return normalized_prefix
    return normalized_prefix + normalized_path


def _normalize_methods(methods: Sequence[str] | None) -> set[str]:
    return {str(value).strip().upper() for value in (methods or []) if str(value).strip()}


def _normalize_patterns(
    *,
    path: str | None = None,
    paths: Sequence[str] | None = None,
    prefix: str = "",
) -> tuple[str, ...]:
    raw_values: list[str] = []
    if path is not None:
        raw_values.append(path)
    if paths:
        raw_values.extend(paths)

    normalized: list[str] = []
    for raw in raw_values:
        candidate = str(raw or "").strip()
        if not candidate:
            continue
        normalized.append(_join_paths(prefix, candidate) if prefix else _join_paths("/", candidate))
    return tuple(normalized)


def _path_matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern


class RouteGroup:
    """Grouped route registration with shared middlewares and path prefix."""

    def __init__(
        self,
        app_pb: "FlaskLikePB",
        prefix: str,
        *,
        middlewares: Sequence[RouteMiddleware] | None = None,
        unbound_middleware_ids: Sequence[str] | None = None,
    ) -> None:
        self._pb = app_pb
        self._prefix = "/" + str(prefix or "").strip("/")
        self._middlewares: list[RouteMiddleware] = list(middlewares or [])
        self._unbound_middleware_ids: set[str] = {
            str(value).strip()
            for value in (unbound_middleware_ids or [])
            if str(value).strip()
        }

    @property
    def prefix(self) -> str:
        return self._prefix

    def middleware(
        self,
        *,
        id: str | None = None,
        priority: int = 0,
        path: str | None = None,
        paths: Sequence[str] | None = None,
        methods: Sequence[str] | None = None,
        predicate: Callable[[Any], bool] | None = None,
    ):
        def decorator(func: MiddlewareHandler) -> MiddlewareHandler:
            merged_predicate = self._pb._build_route_middleware_predicate(
                path=path,
                paths=paths,
                methods=methods,
                predicate=predicate,
                prefix=self._prefix,
            )
            binding = self._pb._extensions.register_middleware(
                func,
                id=id,
                priority=priority,
                predicate=merged_predicate,
            )
            self._middlewares.append(binding)
            return func

        return decorator

    def use(
        self,
        func: MiddlewareHandler | None = None,
        *,
        id: str | None = None,
        priority: int = 0,
        path: str | None = None,
        paths: Sequence[str] | None = None,
        methods: Sequence[str] | None = None,
        predicate: Callable[[Any], bool] | None = None,
    ):
        if func is None:
            return self.middleware(
                id=id,
                priority=priority,
                path=path,
                paths=paths,
                methods=methods,
                predicate=predicate,
            )
        merged_predicate = self._pb._build_route_middleware_predicate(
            path=path,
            paths=paths,
            methods=methods,
            predicate=predicate,
            prefix=self._prefix,
        )
        binding = self._pb._extensions.register_middleware(
            func,
            id=id,
            priority=priority,
            predicate=merged_predicate,
        )
        self._middlewares.append(binding)
        return func

    def route(
        self,
        path: str,
        methods: list[str] | tuple[str, ...] | None = None,
        *,
        middlewares: Sequence[RouteMiddleware] | None = None,
        unbind: Sequence[str] | None = None,
        **fastapi_kwargs: Any,
    ):
        merged_middlewares = [*self._middlewares, *(middlewares or [])]
        merged_unbind = [
            *self._unbound_middleware_ids,
            *(str(value).strip() for value in (unbind or []) if str(value).strip()),
        ]
        return self._pb.route(
            _join_paths(self._prefix, path),
            methods=methods,
            middlewares=merged_middlewares,
            unbind=merged_unbind,
            **fastapi_kwargs,
        )

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

    def group(self, prefix: str) -> "RouteGroup":
        return RouteGroup(
            self._pb,
            _join_paths(self._prefix, prefix),
            middlewares=list(self._middlewares),
            unbound_middleware_ids=list(self._unbound_middleware_ids),
        )

    def unbind(self, *ids: str) -> "RouteGroup":
        for value in ids:
            normalized = str(value or "").strip()
            if normalized:
                self._unbound_middleware_ids.add(normalized)
        return self


class _BuiltinRouteMiddlewares:
    """PocketBase-like route helpers (middlewares + response helpers)."""

    def __init__(self, app_pb: "FlaskLikePB") -> None:
        self._pb = app_pb

    @staticmethod
    def _normalize_collections(collections: Sequence[str]) -> set[str]:
        return {str(value).strip() for value in collections if str(value).strip()}

    def require_guest_only(self) -> MiddlewareHandler:
        async def _middleware(event: Any):
            if hasattr(event, "load_auth"):
                await event.load_auth()
            if getattr(event, "has_auth")():
                raise event._http_error(403, "The request is allowed only for guest clients.")
            return await event.next()

        return _middleware

    def require_auth(self, *collections: str) -> MiddlewareHandler:
        allowed_collections = self._normalize_collections(collections)

        async def _middleware(event: Any):
            if hasattr(event, "load_auth"):
                await event.load_auth()
            auth = getattr(event, "require_auth")()
            if not allowed_collections:
                return await event.next()

            auth_collections = {
                str(auth.get("collectionName", "")).strip(),
                str(auth.get("collectionId", "")).strip(),
            }
            if str(auth.get("type", "")).strip() == "admin":
                auth_collections.add("_superusers")
            auth_collections.discard("")

            if auth_collections.isdisjoint(allowed_collections):
                raise event._http_error(
                    403,
                    "The authorized auth model is not allowed to perform this action.",
                )
            return await event.next()

        return _middleware

    def require_superuser_auth(self) -> MiddlewareHandler:
        async def _middleware(event: Any):
            if hasattr(event, "load_auth"):
                await event.load_auth()
            if not getattr(event, "has_auth")():
                raise event._http_error(401, "The request requires authentication.")
            if not getattr(event, "is_superuser")():
                raise event._http_error(403, "Only superusers can perform this action.")
            return await event.next()

        return _middleware

    def require_superuser_or_owner_auth(
        self,
        owner_id_param: str = "id",
    ) -> MiddlewareHandler:
        owner_key = str(owner_id_param or "id").strip() or "id"

        async def _middleware(event: Any):
            if hasattr(event, "load_auth"):
                await event.load_auth()
            if getattr(event, "is_superuser")():
                return await event.next()

            auth = getattr(event, "require_auth_record")()
            auth_id = str(auth.get("id", "")).strip()
            target_id = str(getattr(event, "path_params", {}).get(owner_key, "")).strip()
            if not target_id or auth_id != target_id:
                raise event._http_error(
                    403,
                    "The authorized auth model is not allowed to perform this action.",
                )
            return await event.next()

        return _middleware

    @staticmethod
    def _apply_fields_filter(record: dict[str, Any], fields_param: str | None) -> dict[str, Any]:
        value = str(fields_param or "").strip()
        if not value:
            return record
        allowed = {field.strip() for field in value.split(",") if field.strip()}
        if not allowed or "*" in allowed:
            return record
        return {key: val for key, val in record.items() if key in allowed}

    @staticmethod
    async def _resolve_collection_ref(
        engine: Any,
        collection: Any | None,
        fallback_record: dict[str, Any] | None = None,
    ) -> Any:
        if collection is not None and hasattr(collection, "id") and hasattr(collection, "name"):
            return collection

        candidate = str(collection or "").strip()
        if not candidate and isinstance(fallback_record, dict):
            candidate = str(
                fallback_record.get("collectionName")
                or fallback_record.get("collectionId")
                or ""
            ).strip()
        if not candidate:
            raise ValueError("Collection context is required.")

        from ppbase.services.record_service import resolve_collection

        resolved = await resolve_collection(engine, candidate)
        if resolved is None:
            raise ValueError(f"Missing collection with id or name '{candidate}'.")
        return resolved

    @staticmethod
    def _build_request_context(
        request: Request,
        auth_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        headers_info: dict[str, str] = {}
        for key, value in request.headers.items():
            lower = key.lower()
            headers_info[lower] = value
            headers_info[lower.replace("-", "_")] = value

        auth_info: dict[str, Any] = {}
        if auth_payload:
            auth_info = {
                "id": auth_payload.get("id", ""),
                "email": auth_payload.get("email", ""),
                "type": auth_payload.get("type", ""),
                "collectionId": auth_payload.get("collectionId", ""),
                "collectionName": auth_payload.get("collectionName", ""),
            }

        return {
            "context": "default",
            "method": request.method.upper(),
            "headers": headers_info,
            "auth": auth_info,
            "data": {},
            "query": dict(request.query_params),
        }

    @staticmethod
    async def _resolve_request_auth(request: Request) -> dict[str, Any] | None:
        from ppbase.ext.events import RouteRequestEvent

        event = RouteRequestEvent(app=request.app, request=request)
        return await event.load_auth()

    async def enrich_records(
        self,
        request: Request,
        records: list[dict[str, Any]],
        *,
        collection: Any | None = None,
        default_expand: str | None = None,
    ) -> list[dict[str, Any]]:
        if not records:
            return []

        from ppbase.db.engine import get_engine
        from ppbase.services.expand_service import expand_records
        from ppbase.services.record_service import get_all_collections

        engine = get_engine()
        collection_ref = await self._resolve_collection_ref(engine, collection, records[0])

        result_records = [dict(item) for item in records]
        auth_payload = await self._resolve_request_auth(request)
        request_context = self._build_request_context(request, auth_payload)

        expand_value = str(request.query_params.get("expand", "") or "").strip()
        if not expand_value:
            expand_value = str(default_expand or "").strip()
        if expand_value:
            all_collections = await get_all_collections(engine)
            await expand_records(
                engine,
                collection_ref,
                result_records,
                expand_value,
                all_collections,
                request_context=request_context,
            )

        fields_value = str(request.query_params.get("fields", "") or "").strip()
        if fields_value:
            result_records = [
                self._apply_fields_filter(item, fields_value)
                for item in result_records
            ]

        return result_records

    async def enrich_record(
        self,
        request: Request,
        record: dict[str, Any] | None,
        *,
        collection: Any | None = None,
        default_expand: str | None = None,
    ) -> dict[str, Any] | None:
        if record is None:
            return None
        records = await self.enrich_records(
            request,
            [record],
            collection=collection,
            default_expand=default_expand,
        )
        return records[0] if records else None

    async def record_auth_response(
        self,
        request: Request,
        record: dict[str, Any],
        *,
        collection: Any | None = None,
        auth_method: str = "default",
        meta: dict[str, Any] | None = None,
        token: str | None = None,
        default_expand: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise ValueError("record must be a dict.")
        record_id = str(record.get("id", "") or "").strip()
        if not record_id:
            raise ValueError("record.id is required.")

        from ppbase.api.record_auth import _trigger_record_auth_success_hook
        from ppbase.db.engine import get_engine
        from ppbase.ext.events import RecordAuthRequestEvent
        from ppbase.services.record_auth_service import generate_record_auth_token

        engine = get_engine()
        collection_ref = await self._resolve_collection_ref(engine, collection, record)

        enriched_record = await self.enrich_record(
            request,
            dict(record),
            collection=collection_ref,
            default_expand=default_expand,
        )
        if enriched_record is None:
            raise ValueError("record is required.")

        final_token = str(token or "").strip()
        if not final_token:
            settings = request.app.state.settings
            final_token = await generate_record_auth_token(
                engine,
                collection_ref,
                record_id,
                settings,
            )

        event = RecordAuthRequestEvent(
            app=request.app,
            request=request,
            collection=collection_ref,
            collection_id_or_name=str(
                getattr(collection_ref, "name", "")
                or getattr(collection_ref, "id", "")
                or ""
            ),
            method=str(auth_method or "default"),
            auth_method=str(auth_method or "default"),
            auth=await self._resolve_request_auth(request),
            record=enriched_record,
            token=final_token,
            meta=dict(meta) if isinstance(meta, dict) else None,
        )

        response_data: dict[str, Any] = {
            "token": final_token,
            "record": enriched_record,
        }
        if meta is not None:
            response_data["meta"] = meta

        return await _trigger_record_auth_success_hook(request, event, response_data)


class FlaskLikePB:
    """Main app facade with Flask-like decorators and hook registration."""

    def __init__(
        self,
        database_url: str | None = None,
        data_dir: str | None = None,
        public_dir: str | None = None,
        dev: bool | None = None,
        origins: list[str] | None = None,
        **overrides: object,
    ) -> None:
        kwargs: dict[str, object] = {**overrides}
        if database_url is not None:
            kwargs["database_url"] = database_url
        if data_dir is not None:
            kwargs["data_dir"] = data_dir
        if public_dir is not None:
            kwargs["public_dir"] = public_dir
        if dev is not None:
            kwargs["dev"] = dev
        if origins is not None:
            kwargs["origins"] = origins

        self._settings_kwargs = kwargs
        self.settings = Settings(**kwargs)  # type: ignore[arg-type]
        self._app = None
        self._extensions = ExtensionRegistry()
        self.apis = _BuiltinRouteMiddlewares(self)

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
        *,
        middlewares: Sequence[RouteMiddleware] | None = None,
        unbind: Sequence[str] | None = None,
        **fastapi_kwargs: Any,
    ) -> Callable[[RouteHandler], RouteHandler]:
        """Register a route handler."""
        route_methods = list(methods) if methods else ["GET"]

        def decorator(func: RouteHandler) -> RouteHandler:
            self._extensions.add_route(
                path,
                route_methods,
                func,
                middlewares=middlewares,
                unbind=unbind,
                **fastapi_kwargs,
            )
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

    @staticmethod
    def _build_route_middleware_predicate(
        *,
        path: str | None = None,
        paths: Sequence[str] | None = None,
        methods: Sequence[str] | None = None,
        predicate: Callable[[Any], bool] | None = None,
        prefix: str = "",
    ) -> Callable[[Any], bool] | None:
        method_filters = _normalize_methods(methods)
        path_patterns = _normalize_patterns(path=path, paths=paths, prefix=prefix)
        if not method_filters and not path_patterns and predicate is None:
            return None

        def _predicate(event: Any) -> bool:
            if method_filters:
                event_method = str(getattr(event, "method", "") or "").upper()
                if event_method not in method_filters:
                    return False

            if path_patterns:
                event_path = str(getattr(event, "path", "") or "").strip()
                if not event_path:
                    request_obj = getattr(event, "request", None)
                    event_path = str(getattr(getattr(request_obj, "url", None), "path", "") or "")
                if not any(_path_matches(event_path, pattern) for pattern in path_patterns):
                    return False

            if predicate is not None and not predicate(event):
                return False

            return True

        return _predicate

    def middleware(
        self,
        *,
        id: str | None = None,
        priority: int = 0,
        path: str | None = None,
        paths: Sequence[str] | None = None,
        methods: Sequence[str] | None = None,
        predicate: Callable[[Any], bool] | None = None,
    ):
        """Register a global extension-route middleware."""
        merged_predicate = self._build_route_middleware_predicate(
            path=path,
            paths=paths,
            methods=methods,
            predicate=predicate,
        )

        def decorator(func: MiddlewareHandler) -> MiddlewareHandler:
            self._extensions.add_global_middleware(
                func,
                id=id,
                priority=priority,
                predicate=merged_predicate,
            )
            return func

        return decorator

    def use(
        self,
        func: MiddlewareHandler | None = None,
        *,
        id: str | None = None,
        priority: int = 0,
        path: str | None = None,
        paths: Sequence[str] | None = None,
        methods: Sequence[str] | None = None,
        predicate: Callable[[Any], bool] | None = None,
    ):
        """Programmatic alias for ``@pb.middleware(...)`` registration."""
        if func is None:
            return self.middleware(
                id=id,
                priority=priority,
                path=path,
                paths=paths,
                methods=methods,
                predicate=predicate,
            )
        merged_predicate = self._build_route_middleware_predicate(
            path=path,
            paths=paths,
            methods=methods,
            predicate=predicate,
        )
        self._extensions.add_global_middleware(
            func,
            id=id,
            priority=priority,
            predicate=merged_predicate,
        )
        return func

    def unbind_middleware(self, id: str) -> FlaskLikePB:
        """Remove a previously registered global middleware by ID."""
        self._extensions.remove_global_middleware(id)
        return self

    def group(self, prefix: str) -> RouteGroup:
        """Create a route group with shared prefix and middlewares."""
        return RouteGroup(self, prefix)

    def records(
        self,
        collection_id_or_name: str,
        *,
        engine: Any | None = None,
    ) -> RecordRepository:
        """Return a repository-like accessor for record CRUD operations."""
        return RecordRepository(collection_id_or_name, engine=engine)

    def optional_auth(self):
        """FastAPI dependency returning auth payload or ``None``."""
        return Depends(get_optional_auth)

    def require_admin(self):
        """FastAPI dependency requiring an admin token."""
        return Depends(require_admin)

    def require_auth(self):
        """FastAPI dependency requiring any valid auth token."""
        return Depends(require_auth)

    def require_record_auth(self):
        """FastAPI dependency requiring an auth-record token."""
        return Depends(require_record_auth)

    @staticmethod
    def _resolve_request_store(request: Request) -> dict[str, Any]:
        store = getattr(request.state, ROUTE_REQUEST_STORE_ATTR, None)
        if isinstance(store, dict):
            return store
        created: dict[str, Any] = {}
        setattr(request.state, ROUTE_REQUEST_STORE_ATTR, created)
        return created

    def request_store(self):
        """FastAPI dependency exposing per-request middleware store."""

        async def _dependency(request: Request) -> dict[str, Any]:
            return self._resolve_request_store(request)

        return Depends(_dependency)

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

    def on_record_request_otp_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_REQUEST_OTP_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_record_auth_with_otp_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_RECORD_AUTH_WITH_OTP_REQUEST,
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

    def on_file_download_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_FILE_DOWNLOAD_REQUEST,
            collections=collections,
            id=id,
            priority=priority,
        )

    def on_file_token_request(
        self, *collections: str, id: str | None = None, priority: int = 0
    ):
        return self._hook_decorator(
            HOOK_FILE_TOKEN_REQUEST,
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
