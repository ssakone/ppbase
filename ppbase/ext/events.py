"""Hook event payloads used by PPBase extension hooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

NextHandler = Callable[[], Awaitable[Any]]


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


@dataclass
class RecordAuthRequestEvent(HookEvent):
    request: Any | None = None
    collection: Any | None = None
    collection_id_or_name: str = ""
    auth: dict[str, Any] | None = None
    method: str = "default"
    body: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] | None = None


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
