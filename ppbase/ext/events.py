"""Hook event payloads used by PPBase extension hooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from ppbase.ext.record_repository import RecordRepository

NextHandler = Callable[[], Awaitable[Any]]
ROUTE_REQUEST_STORE_ATTR = "_ppbase_route_store"


@dataclass
class HookEvent:
    """Base hook event with Go-like middleware chaining support."""

    app: Any | None = None
    _next_handler: NextHandler | None = field(default=None, init=False, repr=False)
    _next_called: bool = field(default=False, init=False, repr=False)

    def _set_next_handler(self, handler: NextHandler) -> None:
        self._next_handler = handler
        self._next_called = False

    def _clear_next_handler(self) -> None:
        self._next_handler = None
        self._next_called = False

    async def next(self) -> Any:
        """Continue hook chain. Can be called at most once per handler."""
        if self._next_handler is None:
            raise RuntimeError("next() is not available in the current hook context.")
        if self._next_called:
            raise RuntimeError("next() can be called only once per hook handler.")
        self._next_called = True
        return await self._next_handler()

    @staticmethod
    def _http_error(status: int, message: str, data: dict[str, Any] | None = None) -> HTTPException:
        return HTTPException(
            status_code=status,
            detail={
                "status": status,
                "message": message,
                "data": data or {},
            },
        )

    def current_auth(self) -> dict[str, Any] | None:
        """Return decoded auth payload when available."""
        auth = getattr(self, "auth", None)
        if isinstance(auth, dict):
            return auth
        return None

    def has_auth(self) -> bool:
        return self.current_auth() is not None

    def auth_type(self) -> str | None:
        auth = self.current_auth()
        if not auth:
            return None
        value = str(auth.get("type", "")).strip()
        return value or None

    def auth_id(self) -> str | None:
        auth = self.current_auth()
        if not auth:
            return None
        value = str(auth.get("id", "")).strip()
        return value or None

    def auth_collection_id(self) -> str | None:
        auth = self.current_auth()
        if not auth:
            return None
        value = str(auth.get("collectionId", "")).strip()
        return value or None

    def auth_collection_name(self) -> str | None:
        auth = self.current_auth()
        if not auth:
            return None
        value = str(auth.get("collectionName", "")).strip()
        return value or None

    def has_record_auth(self) -> bool:
        return self.auth_type() == "authRecord"

    def has_superuser_auth(self) -> bool:
        """PocketBase-like superuser check for event auth context."""
        auth_type = self.auth_type()
        if auth_type == "admin":
            return True
        if auth_type == "authRecord" and self.auth_collection_name() == "_superusers":
            return True
        return False

    def is_superuser(self) -> bool:
        return self.has_superuser_auth()

    def require_auth(self) -> dict[str, Any]:
        auth = self.current_auth()
        if auth is None:
            raise self._http_error(
                401,
                "The request requires authentication.",
            )
        return auth

    def require_auth_record(self) -> dict[str, Any]:
        auth = self.require_auth()
        if not self.has_record_auth():
            raise self._http_error(
                403,
                "The authorized auth model is not allowed to perform this action.",
            )
        return auth

    def require_superuser(self) -> dict[str, Any]:
        auth = self.current_auth()
        if auth is not None and self.has_superuser_auth():
            return auth
        raise self._http_error(
            403,
            "Only superusers can perform this action.",
        )

    def is_auth_collection(self, collection_id_or_name: str) -> bool:
        if not self.has_record_auth():
            return False
        target = str(collection_id_or_name or "").strip()
        if not target:
            return False
        return target in {
            self.auth_collection_id() or "",
            self.auth_collection_name() or "",
        }

    def is_same_auth_record(
        self,
        record_id: str,
        collection_id_or_name: str | None = None,
    ) -> bool:
        if not self.has_record_auth():
            return False
        target_record_id = str(record_id or "").strip()
        if not target_record_id:
            return False
        if self.auth_id() != target_record_id:
            return False

        requested_collection = str(
            collection_id_or_name
            or getattr(self, "collection_id_or_name", "")
            or getattr(getattr(self, "collection", None), "name", "")
            or getattr(getattr(self, "collection", None), "id", "")
        ).strip()
        if not requested_collection:
            return True
        return self.is_auth_collection(requested_collection)

    def require_same_auth_record(
        self,
        record_id: str,
        collection_id_or_name: str | None = None,
    ) -> dict[str, Any]:
        auth = self.require_auth_record()
        if not self.is_same_auth_record(record_id, collection_id_or_name):
            raise self._http_error(
                403,
                "The authorized auth model is not allowed to perform this action.",
            )
        return auth

    def is_admin(self) -> bool:
        return self.auth_type() == "admin"

    def is_record_auth(self) -> bool:
        return self.has_record_auth()

    def current_user_id(self) -> str | None:
        if not self.has_record_auth():
            return None
        return self.auth_id()

    def records(
        self,
        collection_id_or_name: str,
        *,
        engine: Any | None = None,
    ) -> RecordRepository:
        """Build a repository-style accessor for a collection."""
        active_engine = engine if engine is not None else getattr(self, "engine", None)
        return RecordRepository(collection_id_or_name, engine=active_engine)

    async def get_current_user(
        self,
        *,
        fields: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch current auth record when auth token type is ``authRecord``."""
        auth = self.current_auth()
        if not auth or auth.get("type") != "authRecord":
            return None
        collection_ref = str(
            auth.get("collectionName") or auth.get("collectionId") or ""
        ).strip()
        record_id = str(auth.get("id") or "").strip()
        if not collection_ref or not record_id:
            return None
        return await self.records(collection_ref).get(record_id, fields=fields)


@dataclass
class BootstrapEvent(HookEvent):
    settings: Any | None = None


@dataclass
class ServeEvent(HookEvent):
    settings: Any | None = None


@dataclass
class TerminateEvent(HookEvent):
    settings: Any | None = None


@dataclass
class RouteRequestEvent(HookEvent):
    """HTTP extension middleware event payload."""

    request: Any | None = None
    path: str = ""
    methods: tuple[str, ...] = field(default_factory=tuple)
    auth: dict[str, Any] | None = None
    _auth_loaded: bool = field(default=False, init=False, repr=False)
    _store: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @property
    def method(self) -> str:
        if self.request is None:
            return ""
        return str(getattr(self.request, "method", "")).upper()

    @property
    def headers(self) -> dict[str, str]:
        if self.request is None:
            return {}
        return {str(key): str(value) for key, value in self.request.headers.items()}

    @property
    def query_params(self) -> dict[str, str]:
        if self.request is None:
            return {}
        return {str(key): str(value) for key, value in self.request.query_params.items()}

    @property
    def path_params(self) -> dict[str, Any]:
        if self.request is None:
            return {}
        return dict(getattr(self.request, "path_params", {}) or {})

    def _ensure_store(self) -> dict[str, Any]:
        if self.request is None:
            return self._store

        state = getattr(self.request, "state", None)
        if state is None:
            return self._store

        current = getattr(state, ROUTE_REQUEST_STORE_ATTR, None)
        if isinstance(current, dict):
            return current

        current = {}
        setattr(state, ROUTE_REQUEST_STORE_ATTR, current)
        return current

    def set(self, key: str, value: Any) -> "RouteRequestEvent":
        self._ensure_store()[str(key)] = value
        return self

    def get(self, key: str, default: Any = None) -> Any:
        return self._ensure_store().get(str(key), default)

    def has(self, key: str) -> bool:
        return str(key) in self._ensure_store()

    def remove(self, key: str) -> "RouteRequestEvent":
        self._ensure_store().pop(str(key), None)
        return self

    async def load_auth(self) -> dict[str, Any] | None:
        """Resolve request auth payload once and cache it."""
        if self._auth_loaded:
            return self.auth

        self._auth_loaded = True
        if self.request is None:
            return self.auth

        from ppbase.api.deps import get_optional_auth
        from ppbase.db.engine import get_async_session

        try:
            async for session in get_async_session():
                self.auth = await get_optional_auth(self.request, session=session)
                break
        except RuntimeError as exc:
            if "Session factory not initialised" not in str(exc):
                raise
            self.auth = None

        return self.auth


@dataclass
class RecordRequestEvent(HookEvent):
    request: Any | None = None
    collection: Any | None = None
    collection_id_or_name: str = ""
    record_id: str | None = None
    auth: dict[str, Any] | None = None
    data: dict[str, Any] = field(default_factory=dict)
    files: dict[str, list[tuple[str, bytes]]] = field(default_factory=dict)
    page: int | None = None
    per_page: int | None = None
    sort: str | None = None
    filter: str | None = None
    expand: str | None = None
    fields: str | None = None
    skip_total: bool | None = None
    engine: Any | None = None

    def records(
        self,
        collection_id_or_name: str | None = None,
        *,
        engine: Any | None = None,
    ) -> RecordRepository:
        target = str(
            collection_id_or_name
            or self.collection_id_or_name
            or getattr(self.collection, "name", "")
            or getattr(self.collection, "id", "")
        ).strip()
        if not target:
            raise ValueError(
                "Cannot infer collection for RecordRequestEvent. "
                "Pass collection_id_or_name explicitly."
            )
        active_engine = engine if engine is not None else self.engine
        return RecordRepository(target, engine=active_engine)

    async def get(
        self,
        record_id: str | None = None,
        *,
        collection: str | None = None,
        fields: str | None = None,
    ) -> dict[str, Any] | None:
        target_record_id = str(record_id or self.record_id or "").strip()
        if not target_record_id:
            raise ValueError("record_id is required.")
        selected_fields = fields if fields is not None else self.fields
        return await self.records(collection).get(target_record_id, fields=selected_fields)

    async def list(
        self,
        *,
        collection: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
        sort: str | None = None,
        filter: str | None = None,
        fields: str | None = None,
        skip_total: bool | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.records(collection).list(
            page=page if page is not None else (self.page or 1),
            per_page=per_page if per_page is not None else (self.per_page or 30),
            sort=sort if sort is not None else self.sort,
            filter=filter if filter is not None else self.filter,
            fields=fields if fields is not None else self.fields,
            skip_total=skip_total if skip_total is not None else bool(self.skip_total),
            request_context=request_context,
        )

    async def create(
        self,
        data: dict[str, Any],
        *,
        collection: str | None = None,
        files: dict[str, list[tuple[str, bytes]]] | None = None,
    ) -> dict[str, Any]:
        return await self.records(collection).create(data, files=files)

    async def update(
        self,
        data: dict[str, Any],
        *,
        record_id: str | None = None,
        collection: str | None = None,
        files: dict[str, list[tuple[str, bytes]]] | None = None,
    ) -> dict[str, Any] | None:
        target_record_id = str(record_id or self.record_id or "").strip()
        if not target_record_id:
            raise ValueError("record_id is required.")
        return await self.records(collection).update(
            target_record_id,
            data,
            files=files,
        )

    async def delete(
        self,
        *,
        record_id: str | None = None,
        collection: str | None = None,
    ) -> bool:
        target_record_id = str(record_id or self.record_id or "").strip()
        if not target_record_id:
            raise ValueError("record_id is required.")
        return await self.records(collection).delete(target_record_id)


@dataclass
class RecordAuthRequestEvent(HookEvent):
    request: Any | None = None
    collection: Any | None = None
    collection_id_or_name: str = ""
    auth: dict[str, Any] | None = None
    method: str = "default"
    auth_method: str | None = None
    body: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] | None = None
    record: dict[str, Any] | None = None
    token: str | None = None
    meta: dict[str, Any] | None = None
    identity: str | None = None
    identity_field: str | None = None
    password: str | None = None
    email: str | None = None
    otp_id: str | None = None
    otp: str | None = None


@dataclass
class FileTokenRequestEvent(HookEvent):
    request: Any | None = None
    collection: Any | None = None
    record: dict[str, Any] | None = None
    auth: dict[str, Any] | None = None
    token: str | None = None


@dataclass
class FileDownloadRequestEvent(HookEvent):
    request: Any | None = None
    collection: Any | None = None
    record: dict[str, Any] | None = None
    file_field: dict[str, Any] | None = None
    filename: str = ""
    served_path: str = ""
    served_name: str = ""
    force_download: bool = False


@dataclass
class RealtimeConnectEvent(HookEvent):
    request: Any | None = None
    subscription_manager: Any | None = None
    client_id: str | None = None
    auth: dict[str, Any] | None = None


@dataclass
class RealtimeSubscribeEvent(HookEvent):
    request: Any | None = None
    subscription_manager: Any | None = None
    client_id: str = ""
    subscriptions: list[str] = field(default_factory=list)
    parsed_subscriptions: list[Any] = field(default_factory=list)
    authorization: str | None = None
    auth: dict[str, Any] | None = None


@dataclass
class RealtimeMessageSendEvent(HookEvent):
    subscription_manager: Any | None = None
    session: Any | None = None
    client_id: str = ""
    subscription: Any | None = None
    topic: str = ""
    data: dict[str, Any] = field(default_factory=dict)
