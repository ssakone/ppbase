"""Central extension registry for routes and hooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import APIRouter, FastAPI

from ppbase.ext.hooks import Hook

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
HOOK_RECORD_AUTH_REFRESH_REQUEST = "record_auth_refresh_request"
HOOK_RECORD_AUTH_REQUEST = "record_auth_request"
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
    HOOK_RECORD_AUTH_REFRESH_REQUEST,
    HOOK_RECORD_AUTH_REQUEST,
    HOOK_REALTIME_CONNECT_REQUEST,
    HOOK_REALTIME_SUBSCRIBE_REQUEST,
    HOOK_REALTIME_MESSAGE_SEND,
)


@dataclass
class RouteDef:
    path: str
    methods: tuple[str, ...]
    endpoint: Callable[..., Any]
    fastapi_kwargs: dict[str, Any] = field(default_factory=dict)
    order: int = 0


class HookRegistry:
    def __init__(self):
        self._hooks: dict[str, Hook] = {name: Hook(name) for name in HOOK_NAMES}

    def get(self, name: str) -> Hook:
        try:
            return self._hooks[name]
        except KeyError as exc:
            raise ValueError(f"Unknown hook name: {name}") from exc


class ExtensionRegistry:
    """Stores extension routes + hook registrations for one PPBase instance."""

    def __init__(self):
        self.hooks = HookRegistry()
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

    def add_route(
        self,
        path: str,
        methods: list[str] | tuple[str, ...],
        endpoint: Callable[..., Any],
        **fastapi_kwargs: Any,
    ) -> None:
        self.ensure_mutable()
        normalized_methods = tuple(m.upper() for m in methods if str(m).strip())
        if not normalized_methods:
            raise ValueError("Route methods cannot be empty.")
        self._routes.append(
            RouteDef(
                path=path,
                methods=normalized_methods,
                endpoint=endpoint,
                fastapi_kwargs=fastapi_kwargs,
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
            router.add_api_route(
                route.path,
                route.endpoint,
                methods=list(route.methods),
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
