"""Central extension registry for routes and hooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from ppbase.ext.events import RouteRequestEvent
from ppbase.ext.hooks import Hook, HookBinding, run_hook_chain

HOOK_BOOTSTRAP = "bootstrap"
HOOK_SERVE = "serve"
HOOK_TERMINATE = "terminate"
HOOK_RECORDS_LIST_REQUEST = "records_list_request"
HOOK_RECORD_VIEW_REQUEST = "record_view_request"
HOOK_RECORD_CREATE_REQUEST = "record_create_request"
HOOK_RECORD_UPDATE_REQUEST = "record_update_request"
HOOK_RECORD_DELETE_REQUEST = "record_delete_request"
HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST = "record_auth_with_password_request"
HOOK_RECORD_AUTH_WITH_OAUTH2_REQUEST = "record_auth_with_oauth2_request"
HOOK_RECORD_REQUEST_OTP_REQUEST = "record_request_otp_request"
HOOK_RECORD_AUTH_WITH_OTP_REQUEST = "record_auth_with_otp_request"
HOOK_RECORD_AUTH_REFRESH_REQUEST = "record_auth_refresh_request"
HOOK_RECORD_AUTH_REQUEST = "record_auth_request"
HOOK_FILE_DOWNLOAD_REQUEST = "file_download_request"
HOOK_FILE_TOKEN_REQUEST = "file_token_request"
HOOK_REALTIME_CONNECT_REQUEST = "realtime_connect_request"
HOOK_REALTIME_SUBSCRIBE_REQUEST = "realtime_subscribe_request"
HOOK_REALTIME_MESSAGE_SEND = "realtime_message_send"

HOOK_NAMES = (
    HOOK_BOOTSTRAP,
    HOOK_SERVE,
    HOOK_TERMINATE,
    HOOK_RECORDS_LIST_REQUEST,
    HOOK_RECORD_VIEW_REQUEST,
    HOOK_RECORD_CREATE_REQUEST,
    HOOK_RECORD_UPDATE_REQUEST,
    HOOK_RECORD_DELETE_REQUEST,
    HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
    HOOK_RECORD_AUTH_WITH_OAUTH2_REQUEST,
    HOOK_RECORD_REQUEST_OTP_REQUEST,
    HOOK_RECORD_AUTH_WITH_OTP_REQUEST,
    HOOK_RECORD_AUTH_REFRESH_REQUEST,
    HOOK_RECORD_AUTH_REQUEST,
    HOOK_FILE_DOWNLOAD_REQUEST,
    HOOK_FILE_TOKEN_REQUEST,
    HOOK_REALTIME_CONNECT_REQUEST,
    HOOK_REALTIME_SUBSCRIBE_REQUEST,
    HOOK_REALTIME_MESSAGE_SEND,
)


RouteMiddlewareHandler = Callable[[RouteRequestEvent], Any]
RouteMiddlewareInput = RouteMiddlewareHandler | HookBinding
RouteMiddlewarePredicate = Callable[[RouteRequestEvent], bool]


@dataclass
class RouteDef:
    path: str
    methods: tuple[str, ...]
    endpoint: Callable[..., Any]
    fastapi_kwargs: dict[str, Any] = field(default_factory=dict)
    middlewares: tuple[HookBinding, ...] = field(default_factory=tuple)
    order: int = 0


class HookRegistry:
    def __init__(self):
        self._hooks: dict[str, Hook] = {name: Hook(name) for name in HOOK_NAMES}

    def get(self, name: str) -> Hook:
        try:
            return self._hooks[name]
        except KeyError as exc:
            raise ValueError(f"Unknown hook name: {name}") from exc


class ExtensionAPIRoute(APIRoute):
    """FastAPI route that executes PPBase extension middlewares."""

    ppbase_middlewares: tuple[HookBinding, ...] = ()
    ppbase_route_path: str = ""
    ppbase_route_methods: tuple[str, ...] = ()

    def get_route_handler(self) -> Callable[[Request], Any]:
        original_handler = super().get_route_handler()
        middlewares = tuple(self.ppbase_middlewares)
        if not middlewares:
            return original_handler

        async def _default_handler(event: RouteRequestEvent) -> Response:
            if event.request is None:
                raise RuntimeError("Route middleware event is missing request.")
            return await original_handler(event.request)

        async def custom_route_handler(request: Request) -> Response:
            event = RouteRequestEvent(
                app=request.app,
                request=request,
                path=self.ppbase_route_path or self.path,
                methods=self.ppbase_route_methods
                or tuple(sorted(self.methods or set())),
            )
            await event.load_auth()
            matched_middlewares = [item for item in middlewares if item.matches(event)]
            result = await run_hook_chain(event, matched_middlewares, _default_handler)

            if isinstance(result, Response):
                return result
            if result is None:
                return Response(status_code=204)
            return JSONResponse(content=jsonable_encoder(result))

        return custom_route_handler


class ExtensionRegistry:
    """Stores extension routes + hook registrations for one PPBase instance."""

    def __init__(self):
        self.hooks = HookRegistry()
        self._global_middlewares: dict[str, HookBinding] = {}
        self._middleware_order = 0
        self._middleware_id = 0
        self._routes: list[RouteDef] = []
        self._route_order = 0
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        self._frozen = True

    def ensure_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError(
                "Cannot register routes/hooks after app materialization. "
                "Register extensions before calling get_app() or start()."
            )

    def _next_middleware_id(self) -> str:
        self._middleware_id += 1
        return f"route_middleware:{self._middleware_id}"

    def _new_middleware_binding(
        self,
        handler: RouteMiddlewareHandler,
        *,
        id: str | None = None,
        priority: int = 0,
        predicate: RouteMiddlewarePredicate | None = None,
    ) -> HookBinding:
        binding = HookBinding(
            id=id or self._next_middleware_id(),
            handler=handler,
            priority=priority,
            predicate=predicate,
        )
        binding.order = self._middleware_order
        self._middleware_order += 1
        return binding

    def register_middleware(
        self,
        handler: RouteMiddlewareHandler,
        *,
        id: str | None = None,
        priority: int = 0,
        predicate: RouteMiddlewarePredicate | None = None,
    ) -> HookBinding:
        self.ensure_mutable()
        return self._new_middleware_binding(
            handler,
            id=id,
            priority=priority,
            predicate=predicate,
        )

    def add_global_middleware(
        self,
        handler: RouteMiddlewareHandler,
        *,
        id: str | None = None,
        priority: int = 0,
        predicate: RouteMiddlewarePredicate | None = None,
    ) -> str:
        self.ensure_mutable()
        binding = self._new_middleware_binding(
            handler,
            id=id,
            priority=priority,
            predicate=predicate,
        )
        self._global_middlewares[binding.id] = binding
        return binding.id

    def remove_global_middleware(self, id: str) -> None:
        self.ensure_mutable()
        self._global_middlewares.pop(id, None)

    @staticmethod
    def _sort_middlewares(bindings: Sequence[HookBinding]) -> tuple[HookBinding, ...]:
        return tuple(sorted(bindings, key=lambda item: (-item.priority, item.order)))

    def _normalize_route_middlewares(
        self,
        middlewares: Sequence[RouteMiddlewareInput] | None,
    ) -> tuple[HookBinding, ...]:
        if not middlewares:
            return ()

        normalized: list[HookBinding] = []
        for item in middlewares:
            if isinstance(item, HookBinding):
                normalized.append(item)
                continue
            if not callable(item):
                raise TypeError("Route middleware must be callable.")
            normalized.append(self._new_middleware_binding(item))
        return tuple(normalized)

    def add_route(
        self,
        path: str,
        methods: list[str] | tuple[str, ...],
        endpoint: Callable[..., Any],
        *,
        middlewares: Sequence[RouteMiddlewareInput] | None = None,
        unbind: Sequence[str] | None = None,
        **fastapi_kwargs: Any,
    ) -> None:
        self.ensure_mutable()
        normalized_methods = tuple(m.upper() for m in methods if str(m).strip())
        if not normalized_methods:
            raise ValueError("Route methods cannot be empty.")

        route_middlewares = self._normalize_route_middlewares(middlewares)
        combined_middlewares = self._sort_middlewares(
            [*self._global_middlewares.values(), *route_middlewares]
        )
        unbound_ids = {str(value).strip() for value in (unbind or []) if str(value).strip()}
        if unbound_ids:
            combined_middlewares = tuple(
                item for item in combined_middlewares if item.id not in unbound_ids
            )

        self._routes.append(
            RouteDef(
                path=path,
                methods=normalized_methods,
                endpoint=endpoint,
                fastapi_kwargs=fastapi_kwargs,
                middlewares=combined_middlewares,
                order=self._route_order,
            )
        )
        self._route_order += 1

    def get_hook(self, name: str) -> Hook:
        self.ensure_mutable()
        return self.hooks.get(name)

    def mount_routes(self, app: FastAPI) -> None:
        if not self._routes:
            return
        collisions = self._detect_route_collisions(app)
        if collisions:
            details = "\n".join(f"- {item}" for item in collisions)
            raise RuntimeError(
                "Extension route collisions detected. Resolve duplicates before startup:\n"
                f"{details}"
            )

        router = APIRouter()
        for route in sorted(self._routes, key=lambda item: item.order):
            route_class = type(
                f"PPBaseExtensionAPIRoute_{route.order}",
                (ExtensionAPIRoute,),
                {
                    "ppbase_middlewares": route.middlewares,
                    "ppbase_route_path": route.path,
                    "ppbase_route_methods": route.methods,
                },
            )
            router.add_api_route(
                route.path,
                route.endpoint,
                methods=list(route.methods),
                route_class_override=route_class,
                **route.fastapi_kwargs,
            )
        app.include_router(router)

    def _detect_route_collisions(self, app: FastAPI) -> list[str]:
        existing: dict[tuple[str, str], str] = {}
        for route in app.router.routes:
            methods = getattr(route, "methods", None)
            path = getattr(route, "path", None) or getattr(route, "path_format", None)
            if not methods or not path:
                continue
            route_name = getattr(route, "name", "<route>")
            for method in methods:
                existing[(method.upper(), path)] = route_name

        collisions: list[str] = []
        extension_seen: dict[tuple[str, str], str] = {}
        for route in self._routes:
            endpoint_name = getattr(route.endpoint, "__name__", "<handler>")
            for method in route.methods:
                key = (method, route.path)
                if key in existing:
                    collisions.append(
                        f"{method} {route.path} conflicts with existing route '{existing[key]}'"
                    )
                if key in extension_seen:
                    collisions.append(
                        f"{method} {route.path} conflicts with extension route "
                        f"'{extension_seen[key]}'"
                    )
                extension_seen[key] = endpoint_name
        return collisions


def get_extension_registry(app: FastAPI) -> ExtensionRegistry | None:
    return getattr(app.state, "extension_registry", None)
