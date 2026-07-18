"""Cancellation-safe adapters for blocking work owned by async callers."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, TypeVar


_T = TypeVar("_T")


async def to_thread_quiescent(
    function: Callable[..., _T],
    /,
    *args: Any,
    cancel_cleanup: Callable[[_T], None] | None = None,
    **kwargs: Any,
) -> _T:
    """Wait for a blocking worker to stop before propagating cancellation.

    This prevents the worker from retaining an object or file descriptor after
    its async owner has started cleanup. If cancellation wins after the worker
    produced a result, ``cancel_cleanup`` releases that otherwise-unobserved
    result before the original cancellation is re-raised.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            completed = worker.result()
        except BaseException:
            pass
        else:
            if cancel_cleanup is not None:
                cleanup_worker = asyncio.create_task(
                    asyncio.to_thread(cancel_cleanup, completed)
                )
                while not cleanup_worker.done():
                    try:
                        await asyncio.shield(cleanup_worker)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                cleanup_worker.result()
        raise cancellation
