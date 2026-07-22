from __future__ import annotations

import asyncio
import threading

import pytest

from ppbase.services.async_utils import to_thread_quiescent


@pytest.mark.asyncio
async def test_cancel_cleanup_runs_off_loop_and_finishes_before_cancellation() -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()
    ticks = 0

    def worker() -> object:
        worker_started.set()
        assert release_worker.wait(timeout=5)
        return object()

    def cleanup(_result: object) -> None:
        cleanup_started.set()
        assert release_cleanup.wait(timeout=5)
        cleanup_finished.set()

    async def ticker() -> None:
        nonlocal ticks
        while not cleanup_finished.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(
        to_thread_quiescent(worker, cancel_cleanup=cleanup)
    )
    while not worker_started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    release_worker.set()
    while not cleanup_started.is_set():
        await asyncio.sleep(0)

    ticker_task = asyncio.create_task(ticker())
    release_timer = threading.Timer(0.2, release_cleanup.set)
    release_timer.start()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        await ticker_task
    finally:
        release_timer.cancel()

    assert cleanup_finished.is_set()
    assert ticks >= 5
