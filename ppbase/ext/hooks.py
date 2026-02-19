"""Hook chain engine."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ppbase.ext.events import HookEvent

HookCallable = Callable[[HookEvent], Any]
HookPredicate = Callable[[HookEvent], bool]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class HookBinding:
    id: str
    handler: HookCallable
    priority: int = 0
    order: int = 0
    predicate: HookPredicate | None = None

    def matches(self, event: HookEvent) -> bool:
        if self.predicate is None:
            return True
        return self.predicate(event)


async def run_hook_chain(
    event: HookEvent,
    bindings: Sequence[HookBinding],
    default_handler: HookCallable | None = None,
) -> Any:
    """Execute a hook/middleware chain with ``await event.next()`` semantics."""

    ordered_bindings = list(bindings)

    async def dispatch(index: int) -> Any:
        if index >= len(ordered_bindings):
            if default_handler is None:
                return None
            return await _maybe_await(default_handler(event))

        binding = ordered_bindings[index]

        async def _next() -> Any:
            return await dispatch(index + 1)

        event._set_next_handler(_next)
        try:
            return await _maybe_await(binding.handler(event))
        finally:
            event._clear_next_handler()

    return await dispatch(0)


class Hook:
    """Priority-aware middleware-like hook registry."""

    def __init__(self, name: str):
        self.name = name
        self._bindings: dict[str, HookBinding] = {}
        self._next_order = 0
        self._next_id = 0

    def _generate_id(self) -> str:
        self._next_id += 1
        return f"{self.name}:{self._next_id}"

    def bind(
        self,
        handler: HookCallable | HookBinding,
        id: str | None = None,
        priority: int = 0,
        *,
        predicate: HookPredicate | None = None,
    ) -> str:
        """Register a hook handler and return its handler ID."""
        if isinstance(handler, HookBinding):
            binding = handler
            if id is not None:
                binding.id = id
            binding.priority = priority if priority != 0 else binding.priority
            if predicate is not None:
                binding.predicate = predicate
        else:
            binding = HookBinding(
                id=id or self._generate_id(),
                handler=handler,
                priority=priority,
                predicate=predicate,
            )

        binding.order = self._next_order
        self._next_order += 1
        self._bindings[binding.id] = binding
        return binding.id

    def bind_func(
        self,
        func: HookCallable,
        id: str | None = None,
        priority: int = 0,
        *,
        predicate: HookPredicate | None = None,
    ) -> str:
        """Register a function as a hook handler."""
        return self.bind(func, id=id, priority=priority, predicate=predicate)

    def unbind(self, id: str) -> None:
        self._bindings.pop(id, None)

    def unbind_all(self) -> None:
        self._bindings.clear()

    def _ordered_bindings(self, event: HookEvent) -> list[HookBinding]:
        filtered = [binding for binding in self._bindings.values() if binding.matches(event)]
        return sorted(filtered, key=lambda b: (-b.priority, b.order))

    async def trigger(
        self,
        event: HookEvent,
        default_handler: HookCallable | None = None,
    ) -> Any:
        """Trigger the hook chain.

        Handlers can call ``await event.next()`` exactly once to continue.
        """
        bindings = self._ordered_bindings(event)
        return await run_hook_chain(event, bindings, default_handler)
