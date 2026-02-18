from __future__ import annotations

import pytest

from ppbase.ext.events import HookEvent
from ppbase.ext.hooks import Hook


@pytest.mark.asyncio
async def test_hook_priority_and_stable_order() -> None:
    hook = Hook("chain")
    calls: list[str] = []

    async def low(e: HookEvent):
        calls.append("low")
        return await e.next()

    def high_first(e: HookEvent):
        calls.append("high_first")
        return e.next()

    async def high_second(e: HookEvent):
        calls.append("high_second")
        return await e.next()

    hook.bind_func(low, priority=0)
    hook.bind_func(high_first, priority=10)
    hook.bind_func(high_second, priority=10)

    async def default(_: HookEvent):
        calls.append("default")
        return "ok"

    result = await hook.trigger(HookEvent(), default)
    assert result == "ok"
    assert calls == ["high_first", "high_second", "low", "default"]


@pytest.mark.asyncio
async def test_hook_short_circuit_when_next_not_called() -> None:
    hook = Hook("chain")
    calls: list[str] = []

    async def stop(_: HookEvent):
        calls.append("stop")
        return "blocked"

    async def never(_: HookEvent):
        calls.append("never")
        return "never"

    hook.bind_func(stop, priority=10)
    hook.bind_func(never, priority=0)

    async def default(_: HookEvent):
        calls.append("default")
        return "default"

    result = await hook.trigger(HookEvent(), default)
    assert result == "blocked"
    assert calls == ["stop"]


@pytest.mark.asyncio
async def test_hook_next_cannot_be_called_twice() -> None:
    hook = Hook("chain")

    async def invalid(e: HookEvent):
        await e.next()
        await e.next()

    hook.bind_func(invalid)

    async def default(_: HookEvent):
        return "ok"

    with pytest.raises(RuntimeError, match="only once"):
        await hook.trigger(HookEvent(), default)


@pytest.mark.asyncio
async def test_hook_exception_is_fail_closed() -> None:
    hook = Hook("chain")

    async def explode(_: HookEvent):
        raise RuntimeError("boom")

    hook.bind_func(explode)

    with pytest.raises(RuntimeError, match="boom"):
        await hook.trigger(HookEvent(), lambda _: "ok")
