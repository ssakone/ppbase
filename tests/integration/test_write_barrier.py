"""Testcontainers coverage for the PostgreSQL database/file write barrier."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from ppbase.api import settings as settings_api
from ppbase.config import Settings
from ppbase.db.system_tables import ParamRecord
from ppbase.services import (
    file_storage,
    migration_runner as migration_runner_module,
    write_barrier as write_barrier_module,
)
from ppbase.services.migration_runner import migration_lock
from ppbase.services.write_barrier import (
    WriteBarrierConnectionLostError,
    WriteBarrierError,
    WriteBarrierTimeoutError,
    acquire_retained_backup_write_barrier,
    assert_write_barrier_held,
    backup_write_barrier,
    mutation_write_barrier,
    mutation_write_barrier_on_connection,
    storage_runtime_switch_barrier,
)


pytestmark = pytest.mark.asyncio


class _SettingsPatchRequest:
    def __init__(self, payload: dict[str, Any], runtime_settings: Settings) -> None:
        self._payload = payload
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                settings=runtime_settings,
                rate_limit_settings_version=0,
            )
        )

    async def json(self) -> dict[str, Any]:
        return self._payload


class _SettingsDependencySession:
    async def rollback(self) -> None:
        return None


@pytest.fixture(scope="module")
def write_barrier_url() -> Iterator[str]:
    """Use a dedicated disposable PostgreSQL cluster, never a configured DB."""
    with PostgresContainer(
        image="postgres:16-alpine",
        username="ppbarrier",
        password="ppbarrier",
        dbname="ppbarrier",
    ) as postgres:
        host = postgres.get_container_host_ip()
        port = postgres.get_exposed_port(5432)
        yield (
            "postgresql+asyncpg://ppbarrier:ppbarrier@"
            f"{host}:{port}/ppbarrier"
        )


@pytest_asyncio.fixture
async def barrier_engine(write_barrier_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        write_barrier_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _wait_for_advisory_lock_count(
    engine: AsyncEngine,
    minimum: int,
    *,
    timeout: float = 2.0,
) -> None:
    async def locks_are_visible() -> bool:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND granted"
                )
            )
            return int(result.scalar_one()) >= minimum

    deadline = asyncio.get_running_loop().time() + timeout
    while not await locks_are_visible():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Expected at least {minimum} granted advisory locks."
            )
        await asyncio.sleep(0.02)


async def _wait_for_waiting_exclusive_advisory_lock(
    engine: AsyncEngine,
    key: int,
    *,
    timeout: float = 2.0,
) -> None:
    unsigned_key = key & ((1 << 64) - 1)
    class_id = (unsigned_key >> 32) & 0xFFFFFFFF
    object_id = unsigned_key & 0xFFFFFFFF

    async def exclusive_waiter_is_visible() -> bool:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_locks "
                    "WHERE locktype = 'advisory' "
                    "AND mode = 'ExclusiveLock' AND NOT granted "
                    "AND objsubid = 1 AND classid::bigint = :class_id "
                    "AND objid::bigint = :object_id"
                    ")"
                ),
                {
                    "class_id": class_id,
                    "object_id": object_id,
                },
            )
            return bool(result.scalar_one())

    deadline = asyncio.get_running_loop().time() + timeout
    while not await exclusive_waiter_is_visible():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "Expected a waiting exclusive PostgreSQL advisory lock."
            )
        await asyncio.sleep(0.02)


async def _seed_durable_settings(
    engine: AsyncEngine,
    settings_value: dict[str, Any],
) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: ParamRecord.__table__.create(
                sync_connection,
                checkfirst=True,
            )
        )
        await connection.execute(
            delete(ParamRecord).where(ParamRecord.key == "settings")
        )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            ParamRecord(
                id="settings_test",
                key="settings",
                value=settings_value,
            )
        )
        await session.commit()


async def _read_durable_settings(engine: AsyncEngine) -> dict[str, Any]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        value = await session.scalar(
            select(ParamRecord.value).where(ParamRecord.key == "settings")
        )
    assert isinstance(value, dict)
    return value


def _prepare_local_settings_runtime(tmp_path: Path) -> Settings:
    runtime_settings = Settings(
        data_dir=str(tmp_path),
        storage_backend="local",
    )
    file_storage._set_storage_settings_unchecked(runtime_settings)
    file_storage._clear_runtime_storage_overrides_unchecked()
    return runtime_settings


def _assert_runtime_reconciled_to_s3(
    request: _SettingsPatchRequest,
) -> None:
    assert file_storage.get_storage_backend() == "s3"
    assert request.app.state.settings.storage_backend == "s3"
    assert request.app.state.settings.s3_bucket == "durable-bucket"
    assert request.app.state.settings.s3_access_key == "durable-access"
    assert request.app.state.settings.s3_secret_key == "durable-secret"
    assert request.app.state.rate_limit_settings_version == 1


async def test_shared_mutations_overlap_and_exclusive_backup_waits(
    write_barrier_url: str,
) -> None:
    mutation_engines = [
        create_async_engine(write_barrier_url, poolclass=NullPool)
        for _ in range(2)
    ]
    backup_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    both_shared = asyncio.Event()
    release_shared = asyncio.Event()
    shared_count = 0
    count_lock = asyncio.Lock()
    backup_entered = asyncio.Event()

    async def mutate(engine: AsyncEngine) -> None:
        nonlocal shared_count
        async with mutation_write_barrier(engine, timeout_seconds=2):
            async with count_lock:
                shared_count += 1
                if shared_count == 2:
                    both_shared.set()
            await release_shared.wait()

    async def backup() -> None:
        async with backup_write_barrier(backup_engine, timeout_seconds=2):
            backup_entered.set()

    mutation_tasks = [
        asyncio.create_task(mutate(engine)) for engine in mutation_engines
    ]
    backup_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(both_shared.wait(), timeout=1)
        backup_task = asyncio.create_task(backup())
        await asyncio.sleep(0.1)
        assert not backup_entered.is_set()

        release_shared.set()
        await asyncio.wait_for(backup_entered.wait(), timeout=1)
        await asyncio.gather(*mutation_tasks, backup_task)
    finally:
        release_shared.set()
        for task in mutation_tasks:
            if not task.done():
                task.cancel()
        if backup_task is not None and not backup_task.done():
            backup_task.cancel()
        await asyncio.gather(
            *mutation_tasks,
            *([backup_task] if backup_task is not None else []),
            return_exceptions=True,
        )
        for engine in [*mutation_engines, backup_engine]:
            await engine.dispose()


async def test_write_barrier_ignores_search_path_function_spoofing(
    write_barrier_url: str,
) -> None:
    setup_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    spoofed_engine: AsyncEngine | None = None
    try:
        async with setup_engine.begin() as connection:
            await connection.execute(
                text("DROP SCHEMA IF EXISTS barrier_spoof CASCADE")
            )
            await connection.execute(text("CREATE SCHEMA barrier_spoof"))
            await connection.execute(
                text(
                    "CREATE FUNCTION barrier_spoof.pg_backend_pid() "
                    "RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT -1'"
                )
            )
            await connection.execute(
                text(
                    "CREATE FUNCTION barrier_spoof.pg_try_advisory_lock(bigint) "
                    "RETURNS boolean LANGUAGE sql AS 'SELECT true'"
                )
            )
            await connection.execute(
                text(
                    "CREATE FUNCTION barrier_spoof.pg_advisory_unlock(bigint) "
                    "RETURNS boolean LANGUAGE sql AS 'SELECT true'"
                )
            )
            await connection.execute(
                text(
                    "CREATE FUNCTION barrier_spoof.concat_name(name, text) "
                    "RETURNS text LANGUAGE sql IMMUTABLE AS "
                    "'SELECT pg_catalog.concat($1, $2, ''-spoofed'')'"
                )
            )
            await connection.execute(
                text(
                    "CREATE OPERATOR barrier_spoof.|| ("
                    "FUNCTION = barrier_spoof.concat_name, "
                    "LEFTARG = name, RIGHTARG = text)"
                )
            )
            for argument_type in ("integer", "bigint", "text"):
                await connection.execute(
                    text(
                        f"CREATE FUNCTION barrier_spoof.always_true_{argument_type}("
                        f"{argument_type}, {argument_type}) RETURNS boolean "
                        "LANGUAGE sql IMMUTABLE AS 'SELECT true'"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE OPERATOR barrier_spoof.= ("
                        f"FUNCTION = barrier_spoof.always_true_{argument_type}, "
                        f"LEFTARG = {argument_type}, RIGHTARG = {argument_type})"
                    )
                )

        spoofed_engine = create_async_engine(
            write_barrier_url,
            poolclass=NullPool,
            connect_args={
                "server_settings": {
                    "search_path": "barrier_spoof, pg_catalog",
                }
            },
        )
        async with backup_write_barrier(
            spoofed_engine,
            timeout_seconds=1,
        ) as lease:
            assert lease.backend_pid > 0
            async with setup_engine.connect() as observer:
                canonical_barrier_key = int(
                    await observer.scalar(
                        text(
                            "SELECT pg_catalog.hashtextextended("
                            "pg_catalog.concat(pg_catalog.current_database(), "
                            "':ppbase:backup-write-barrier'), :seed)"
                        ),
                        {"seed": write_barrier_module._WRITE_BARRIER_SEED},
                    )
                )
                canonical_migration_key = (
                    await migration_runner_module.migration_lock_key(observer)
                )
                actual_lock_count = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_locks "
                        "WHERE locktype = 'advisory' AND granted AND pid = :pid"
                    ),
                    {"pid": lease.backend_pid},
                )
                lock_rows = (
                    await observer.execute(
                        text(
                            "SELECT classid::bigint AS classid, "
                            "objid::bigint AS objid "
                            "FROM pg_catalog.pg_locks "
                            "WHERE locktype = 'advisory' AND granted "
                            "AND pid = :pid AND objsubid = 1"
                        ),
                        {"pid": lease.backend_pid},
                    )
                ).mappings().all()
            assert int(actual_lock_count) == 2
            assert lease.barrier_key == canonical_barrier_key
            observed_keys: set[int] = set()
            for row in lock_rows:
                unsigned = (int(row["classid"]) << 32) | int(row["objid"])
                observed_keys.add(
                    unsigned - (1 << 64)
                    if unsigned >= (1 << 63)
                    else unsigned
                )
            assert observed_keys == {
                lease.barrier_key,
                canonical_migration_key,
            }

        with pytest.raises(WriteBarrierConnectionLostError):
            async with mutation_write_barrier(
                spoofed_engine,
                timeout_seconds=1,
            ) as lease:
                unlocked = await lease.connection.scalar(
                    text("SELECT pg_catalog.pg_advisory_unlock_shared(:key)"),
                    {"key": lease.barrier_key},
                )
                await lease.connection.commit()
                assert unlocked is True
                await assert_write_barrier_held(lease)
    finally:
        if spoofed_engine is not None:
            await spoofed_engine.dispose()
        async with setup_engine.begin() as connection:
            await connection.execute(
                text("DROP SCHEMA IF EXISTS barrier_spoof CASCADE")
            )
        await setup_engine.dispose()


async def test_waiting_exclusive_backup_prevents_new_shared_lock_barging(
    write_barrier_url: str,
) -> None:
    first_mutation_engine = create_async_engine(
        write_barrier_url,
        poolclass=NullPool,
    )
    backup_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    second_mutation_engine = create_async_engine(
        write_barrier_url,
        poolclass=NullPool,
    )
    observer_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    first_mutation_entered = asyncio.Event()
    release_first_mutation = asyncio.Event()
    backup_entered = asyncio.Event()
    release_backup = asyncio.Event()
    second_mutation_entered = asyncio.Event()
    acquisition_order: list[str] = []
    barrier_key: int | None = None

    async def hold_first_mutation() -> None:
        nonlocal barrier_key
        async with mutation_write_barrier(
            first_mutation_engine,
            timeout_seconds=3,
        ) as lease:
            barrier_key = lease.barrier_key
            first_mutation_entered.set()
            await release_first_mutation.wait()

    async def run_backup() -> None:
        async with backup_write_barrier(
            backup_engine,
            timeout_seconds=3,
        ):
            acquisition_order.append("backup")
            backup_entered.set()
            await release_backup.wait()

    async def run_second_mutation() -> None:
        async with mutation_write_barrier(
            second_mutation_engine,
            timeout_seconds=3,
        ):
            acquisition_order.append("second_mutation")
            second_mutation_entered.set()

    first_mutation_task = asyncio.create_task(hold_first_mutation())
    backup_task: asyncio.Task[None] | None = None
    second_mutation_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_mutation_entered.wait(), timeout=1)
        assert barrier_key is not None
        backup_task = asyncio.create_task(run_backup())
        await _wait_for_waiting_exclusive_advisory_lock(
            observer_engine,
            barrier_key,
        )

        second_mutation_task = asyncio.create_task(run_second_mutation())
        await asyncio.sleep(0.1)
        assert not second_mutation_entered.is_set()

        release_first_mutation.set()
        await asyncio.wait_for(backup_entered.wait(), timeout=1)
        assert not second_mutation_entered.is_set()
        assert acquisition_order == ["backup"]

        release_backup.set()
        await asyncio.wait_for(second_mutation_entered.wait(), timeout=1)
        await asyncio.gather(
            first_mutation_task,
            backup_task,
            second_mutation_task,
        )
        assert acquisition_order == ["backup", "second_mutation"]
    finally:
        release_first_mutation.set()
        release_backup.set()
        tasks = [first_mutation_task]
        if backup_task is not None:
            tasks.append(backup_task)
        if second_mutation_task is not None:
            tasks.append(second_mutation_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for engine in (
            first_mutation_engine,
            backup_engine,
            second_mutation_engine,
            observer_engine,
        ):
            await engine.dispose()


async def test_backup_lock_order_avoids_deadlock_with_pool_size_one(
    write_barrier_url: str,
) -> None:
    migration_engine = create_async_engine(
        write_barrier_url,
        pool_size=1,
        max_overflow=0,
    )
    backup_engine = create_async_engine(
        write_barrier_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    mutation_engine = create_async_engine(
        write_barrier_url,
        pool_size=1,
        max_overflow=0,
    )
    observer_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    backup_entered = asyncio.Event()
    release_backup = asyncio.Event()
    mutation_entered = asyncio.Event()

    async def backup() -> None:
        async with backup_write_barrier(
            backup_engine,
            timeout_seconds=3,
        ) as lease:
            result = await lease.connection.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
            await lease.connection.rollback()
            backup_entered.set()
            await release_backup.wait()

    async def mutate() -> None:
        async with mutation_write_barrier(
            mutation_engine,
            timeout_seconds=3,
        ):
            mutation_entered.set()

    backup_task: asyncio.Task[None] | None = None
    try:
        # An existing migration may finish without joining the new barrier.
        # The backup takes the barrier first and then waits for this lock.
        async with migration_lock(migration_engine, timeout_seconds=1):
            backup_task = asyncio.create_task(backup())
            await _wait_for_advisory_lock_count(observer_engine, 2)
            assert not backup_entered.is_set()

            mutation_task = asyncio.create_task(mutate())
            await asyncio.sleep(0.1)
            assert not mutation_entered.is_set()

        await asyncio.wait_for(backup_entered.wait(), timeout=1)
        assert not mutation_entered.is_set()
        release_backup.set()
        await asyncio.wait_for(mutation_entered.wait(), timeout=1)
        await asyncio.gather(backup_task, mutation_task)
    finally:
        release_backup.set()
        for task in (backup_task, mutation_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (backup_task, mutation_task)
                if task is not None
            ),
            return_exceptions=True,
        )
        for engine in (
            migration_engine,
            backup_engine,
            mutation_engine,
            observer_engine,
        ):
            await engine.dispose()


async def test_retained_cutover_barrier_blocks_mutations_until_explicit_release(
    write_barrier_url: str,
) -> None:
    cutover_engine = create_async_engine(
        write_barrier_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    mutation_engine = create_async_engine(
        write_barrier_url,
        pool_size=1,
        max_overflow=0,
    )
    mutation_entered = asyncio.Event()
    guard = None
    mutation_task: asyncio.Task[None] | None = None

    async def mutate() -> None:
        async with mutation_write_barrier(
            mutation_engine,
            timeout_seconds=3,
        ):
            mutation_entered.set()

    try:
        guard = await acquire_retained_backup_write_barrier(
            cutover_engine,
            timeout_seconds=3,
        )
        assert guard.active is True
        await guard.verify_held()

        with pytest.raises(WriteBarrierError, match="mutations are fenced"):
            await mutate()
        assert mutation_entered.is_set() is False

        # Exec failure callbacks arrive from the restart thread and marshal
        # release onto a fresh task on this loop.
        await asyncio.create_task(guard.close())
        assert guard.active is False
        await mutate()
        assert mutation_entered.is_set() is True
    finally:
        if guard is not None and guard.active:
            await guard.close()
        await cutover_engine.dispose()
        await mutation_engine.dispose()


async def test_queued_mutation_rechecks_process_fence_after_cutover_session_loss(
    write_barrier_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cutover_engine = create_async_engine(
        write_barrier_url,
        pool_size=1,
        max_overflow=0,
    )
    mutation_engine = create_async_engine(
        write_barrier_url,
        pool_size=1,
        max_overflow=0,
    )
    observer_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    mutation_open_started = asyncio.Event()
    allow_mutation_open = asyncio.Event()
    mutation_entered = asyncio.Event()
    original_open_lease = write_barrier_module._open_lease

    async def delayed_mutation_open(engine, **kwargs):
        if engine is mutation_engine:
            mutation_open_started.set()
            await allow_mutation_open.wait()
        return await original_open_lease(engine, **kwargs)

    monkeypatch.setattr(
        write_barrier_module,
        "_open_lease",
        delayed_mutation_open,
    )

    async def mutate() -> None:
        async with mutation_write_barrier(
            mutation_engine,
            timeout_seconds=3,
        ):
            mutation_entered.set()

    guard = None
    mutation_task = asyncio.create_task(mutate())
    try:
        await asyncio.wait_for(mutation_open_started.wait(), timeout=2)
        guard = await acquire_retained_backup_write_barrier(
            cutover_engine,
            timeout_seconds=3,
        )
        allow_mutation_open.set()

        async with observer_engine.begin() as observer:
            terminated = bool(
                (
                    await observer.execute(
                        text("SELECT pg_catalog.pg_terminate_backend(:pid)"),
                        {"pid": guard.lease.backend_pid},
                    )
                ).scalar_one()
            )
        assert terminated is True

        with pytest.raises(WriteBarrierError, match="mutations are fenced"):
            await asyncio.wait_for(mutation_task, timeout=3)
        assert mutation_entered.is_set() is False
    finally:
        allow_mutation_open.set()
        if not mutation_task.done():
            mutation_task.cancel()
            await asyncio.gather(mutation_task, return_exceptions=True)
        if guard is not None:
            try:
                await guard.close()
            except WriteBarrierError:
                pass
        await observer_engine.dispose()
        await mutation_engine.dispose()
        await cutover_engine.dispose()


async def test_exclusive_timeout_releases_partial_lease(
    write_barrier_url: str,
) -> None:
    mutation_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    backup_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    observer_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    mutation_entered = asyncio.Event()
    release_mutation = asyncio.Event()

    async def hold_mutation() -> None:
        async with mutation_write_barrier(
            mutation_engine,
            timeout_seconds=1,
        ):
            mutation_entered.set()
            await release_mutation.wait()

    mutation_task = asyncio.create_task(hold_mutation())

    try:
        await asyncio.wait_for(mutation_entered.wait(), timeout=1)
        with pytest.raises(WriteBarrierTimeoutError, match="exclusive"):
            async with backup_write_barrier(
                backup_engine,
                timeout_seconds=0.1,
            ):
                pytest.fail("Exclusive barrier must not be entered")

        release_mutation.set()
        await mutation_task

        async with observer_engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND granted"
                )
            )
            assert int(result.scalar_one()) == 0
    finally:
        release_mutation.set()
        if not mutation_task.done():
            mutation_task.cancel()
        await asyncio.gather(mutation_task, return_exceptions=True)
        for engine in (mutation_engine, backup_engine, observer_engine):
            await engine.dispose()


async def test_nested_shared_barrier_reuses_one_pool_connection(
    barrier_engine: AsyncEngine,
) -> None:
    async with mutation_write_barrier(
        barrier_engine,
        timeout_seconds=1,
    ) as outer:
        async with mutation_write_barrier(
            barrier_engine,
            timeout_seconds=0.05,
        ) as inner:
            assert inner is outer
            result = await inner.connection.execute(text("SELECT pg_backend_pid()"))
            assert int(result.scalar_one()) == outer.backend_pid
            await inner.connection.rollback()


async def test_connection_loss_aborts_before_sealing(
    barrier_engine: AsyncEngine,
    write_barrier_url: str,
    tmp_path: Path,
) -> None:
    killer_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    partial_resource = tmp_path / "database.dump.partial"
    seal = tmp_path / "SEALED"

    async def build_then_seal() -> None:
        async with backup_write_barrier(
            barrier_engine,
            timeout_seconds=1,
        ) as lease:
            partial_resource.write_bytes(b"unsealed backup resource")
            async with killer_engine.connect() as killer:
                result = await killer.execute(
                    text("SELECT pg_terminate_backend(:pid)"),
                    {"pid": lease.backend_pid},
                )
                assert bool(result.scalar_one())
        # Publication is deliberately after the fail-closed lease boundary.
        seal.write_text("sealed", encoding="utf-8")

    try:
        with pytest.raises(WriteBarrierConnectionLostError, match="session"):
            await build_then_seal()
        assert partial_resource.exists()
        assert not seal.exists()
    finally:
        await killer_engine.dispose()


async def test_borrowed_ambiguous_acquisition_invalidates_uncancellably(
    barrier_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_acquired = asyncio.Event()
    invalidation_started = asyncio.Event()
    finish_invalidation = asyncio.Event()
    wait_forever = asyncio.Event()
    backend_pid = 0

    async def acquire_then_lose_result(
        connection,
        *,
        key: int,
        mode,
        deadline: float,
        label: str,
        finish_transaction: bool,
    ) -> None:
        _ = (mode, deadline, label)
        assert finish_transaction is False
        result = await connection.execute(
            text("SELECT pg_advisory_lock_shared(:key)"),
            {"key": key},
        )
        assert result.scalar_one() is None
        lock_acquired.set()
        await wait_forever.wait()

    original_invalidate = write_barrier_module.AsyncConnection.invalidate

    async def delayed_invalidate(connection, *args, **kwargs) -> None:
        invalidation_started.set()
        await finish_invalidation.wait()
        await original_invalidate(connection, *args, **kwargs)

    monkeypatch.setattr(
        write_barrier_module,
        "_acquire_lock",
        acquire_then_lose_result,
    )
    monkeypatch.setattr(
        write_barrier_module.AsyncConnection,
        "invalidate",
        delayed_invalidate,
    )

    async with barrier_engine.connect() as borrowed_connection:
        backend_pid = int(
            await borrowed_connection.scalar(text("SELECT pg_backend_pid()"))
        )

        async def acquire_borrowed_barrier() -> None:
            async with mutation_write_barrier_on_connection(
                borrowed_connection,
                timeout_seconds=2,
            ):
                pytest.fail("An ambiguously acquired lease must not be entered")

        acquisition = asyncio.create_task(acquire_borrowed_barrier())
        await asyncio.wait_for(lock_acquired.wait(), timeout=1)
        acquisition.cancel()
        await asyncio.wait_for(invalidation_started.wait(), timeout=1)
        # A second cancellation while invalidate() is blocked must not return
        # the session-level advisory lock to the SQLAlchemy pool.
        acquisition.cancel()
        finish_invalidation.set()
        with pytest.raises(asyncio.CancelledError):
            await acquisition
        assert borrowed_connection.invalidated

    async with barrier_engine.connect() as observer:
        leaked_locks = await observer.scalar(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = :pid"
            ),
            {"pid": backend_pid},
        )
        assert int(leaked_locks) == 0


async def test_owned_ambiguous_acquisition_is_not_returned_to_pool(
    barrier_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_acquired = asyncio.Event()
    invalidation_started = asyncio.Event()
    finish_invalidation = asyncio.Event()
    wait_forever = asyncio.Event()
    backend_pid = 0

    async def acquire_then_lose_result(
        connection,
        *,
        key: int,
        mode,
        deadline: float,
        label: str,
        finish_transaction: bool,
    ) -> None:
        nonlocal backend_pid
        _ = (mode, deadline, label)
        assert finish_transaction is True
        result = await connection.execute(
            text("SELECT pg_advisory_lock_shared(:key)"),
            {"key": key},
        )
        assert result.scalar_one() is None
        backend_pid = int(await connection.scalar(text("SELECT pg_backend_pid()")))
        lock_acquired.set()
        await wait_forever.wait()

    original_invalidate = write_barrier_module.AsyncConnection.invalidate

    async def delayed_invalidate(connection, *args, **kwargs) -> None:
        invalidation_started.set()
        await finish_invalidation.wait()
        await original_invalidate(connection, *args, **kwargs)

    with monkeypatch.context() as acquisition_patch:
        acquisition_patch.setattr(
            write_barrier_module,
            "_acquire_lock",
            acquire_then_lose_result,
        )
        acquisition_patch.setattr(
            write_barrier_module.AsyncConnection,
            "invalidate",
            delayed_invalidate,
        )

        async def acquire_owned_barrier() -> None:
            async with mutation_write_barrier(
                barrier_engine,
                timeout_seconds=2,
            ):
                pytest.fail("An ambiguously acquired lease must not be entered")

        acquisition = asyncio.create_task(acquire_owned_barrier())
        await asyncio.wait_for(lock_acquired.wait(), timeout=1)
        acquisition.cancel()
        await asyncio.wait_for(invalidation_started.wait(), timeout=1)
        acquisition.cancel()
        finish_invalidation.set()
        with pytest.raises(asyncio.CancelledError):
            await acquisition

    assert backend_pid > 0
    async with backup_write_barrier(
        barrier_engine,
        timeout_seconds=2,
    ) as lease:
        assert lease.backend_pid != backend_pid
        leaked_locks = await lease.connection.scalar(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = :pid"
            ),
            {"pid": backend_pid},
        )
        assert int(leaked_locks) == 0
        await lease.connection.rollback()


async def test_ambiguous_migration_lock_acquisition_invalidates_pool_session(
    barrier_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_lock_acquired = asyncio.Event()
    invalidation_started = asyncio.Event()
    finish_invalidation = asyncio.Event()
    wait_forever = asyncio.Event()
    advisory_acquisition_count = 0
    backend_pid = 0
    original_execute = migration_runner_module.AsyncConnection.execute
    original_invalidate = migration_runner_module.AsyncConnection.invalidate

    async def delay_second_exclusive_lock(
        connection,
        statement,
        *args,
        **kwargs,
    ):
        nonlocal advisory_acquisition_count, backend_pid
        result = await original_execute(connection, statement, *args, **kwargs)
        if (
            str(statement).strip()
            == "SELECT pg_catalog.pg_try_advisory_lock(:key)"
        ):
            advisory_acquisition_count += 1
            if advisory_acquisition_count == 2:
                pid_result = await original_execute(
                    connection,
                    text("SELECT pg_backend_pid()"),
                )
                backend_pid = int(pid_result.scalar_one())
                migration_lock_acquired.set()
                await wait_forever.wait()
        return result

    async def delayed_invalidate(connection, *args, **kwargs) -> None:
        invalidation_started.set()
        await finish_invalidation.wait()
        await original_invalidate(connection, *args, **kwargs)

    with monkeypatch.context() as migration_patch:
        migration_patch.setattr(
            migration_runner_module.AsyncConnection,
            "execute",
            delay_second_exclusive_lock,
        )
        migration_patch.setattr(
            migration_runner_module.AsyncConnection,
            "invalidate",
            delayed_invalidate,
        )

        async def acquire_backup_locks() -> None:
            async with backup_write_barrier(
                barrier_engine,
                timeout_seconds=3,
            ):
                pytest.fail("Migration-lock acquisition must remain interrupted")

        acquisition = asyncio.create_task(acquire_backup_locks())
        await asyncio.wait_for(migration_lock_acquired.wait(), timeout=1)
        acquisition.cancel()
        await asyncio.wait_for(invalidation_started.wait(), timeout=1)
        acquisition.cancel()
        finish_invalidation.set()
        with pytest.raises(asyncio.CancelledError):
            await acquisition

    assert advisory_acquisition_count == 2
    assert backend_pid > 0
    async with backup_write_barrier(
        barrier_engine,
        timeout_seconds=2,
    ) as lease:
        assert lease.backend_pid != backend_pid
        leaked_locks = await lease.connection.scalar(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = :pid"
            ),
            {"pid": backend_pid},
        )
        assert int(leaked_locks) == 0
        await lease.connection.rollback()


async def test_migration_unlock_invalidation_resists_repeated_cancellation(
    barrier_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlock_started = asyncio.Event()
    invalidation_started = asyncio.Event()
    finish_invalidation = asyncio.Event()
    wait_forever = asyncio.Event()
    backend_pid = 0
    original_execute = migration_runner_module.AsyncConnection.execute
    original_invalidate = migration_runner_module.AsyncConnection.invalidate

    async def interrupt_migration_unlock(
        connection,
        statement,
        *args,
        **kwargs,
    ):
        if (
            str(statement).strip()
            == "SELECT pg_catalog.pg_advisory_unlock(:key)"
        ):
            unlock_started.set()
            await wait_forever.wait()
        return await original_execute(connection, statement, *args, **kwargs)

    async def delayed_invalidate(connection, *args, **kwargs) -> None:
        invalidation_started.set()
        await finish_invalidation.wait()
        await original_invalidate(connection, *args, **kwargs)

    with monkeypatch.context() as unlock_patch:
        unlock_patch.setattr(
            migration_runner_module.AsyncConnection,
            "execute",
            interrupt_migration_unlock,
        )
        unlock_patch.setattr(
            migration_runner_module.AsyncConnection,
            "invalidate",
            delayed_invalidate,
        )

        async def release_backup_locks() -> None:
            nonlocal backend_pid
            async with backup_write_barrier(
                barrier_engine,
                timeout_seconds=3,
            ) as lease:
                backend_pid = lease.backend_pid

        release = asyncio.create_task(release_backup_locks())
        await asyncio.wait_for(unlock_started.wait(), timeout=1)
        release.cancel()
        await asyncio.wait_for(invalidation_started.wait(), timeout=1)
        release.cancel()
        finish_invalidation.set()
        with pytest.raises(WriteBarrierConnectionLostError):
            await release

    assert backend_pid > 0
    async with backup_write_barrier(
        barrier_engine,
        timeout_seconds=2,
    ) as lease:
        assert lease.backend_pid != backend_pid
        leaked_locks = await lease.connection.scalar(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = :pid"
            ),
            {"pid": backend_pid},
        )
        assert int(leaked_locks) == 0
        await lease.connection.rollback()


async def test_local_to_s3_switch_waits_for_active_storage_mutation(
    write_barrier_url: str,
    tmp_path: Path,
) -> None:
    mutation_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    switch_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    mutation_entered = asyncio.Event()
    release_mutation = asyncio.Event()
    switch_entered = asyncio.Event()

    file_storage._set_storage_settings_unchecked(
        Settings(data_dir=str(tmp_path), storage_backend="local")
    )
    file_storage._clear_runtime_storage_overrides_unchecked()

    async def hold_mutation() -> None:
        async with mutation_write_barrier(mutation_engine):
            mutation_entered.set()
            await release_mutation.wait()

    async def switch_to_s3() -> None:
        async with storage_runtime_switch_barrier(switch_engine) as lease:
            switch_entered.set()
            file_storage.configure_storage_runtime_from_settings_payload(
                {
                    "s3": {
                        "enabled": True,
                        "bucket": "barrier-bucket",
                        "accessKey": "barrier-access",
                        "secret": "barrier-secret",
                    }
                },
                lease=lease,
            )

    mutation_task = asyncio.create_task(hold_mutation())
    switch_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(mutation_entered.wait(), timeout=1)
        switch_task = asyncio.create_task(switch_to_s3())
        await asyncio.sleep(0.1)
        assert not switch_entered.is_set()
        assert file_storage.get_storage_backend() == "local"

        release_mutation.set()
        await asyncio.wait_for(switch_entered.wait(), timeout=2)
        await switch_task
        assert file_storage.get_storage_backend() == "s3"
    finally:
        release_mutation.set()
        if not mutation_task.done():
            mutation_task.cancel()
        if switch_task is not None and not switch_task.done():
            switch_task.cancel()
        await asyncio.gather(
            mutation_task,
            *([switch_task] if switch_task is not None else []),
            return_exceptions=True,
        )
        file_storage._clear_runtime_storage_overrides_unchecked()
        file_storage._set_storage_settings_unchecked(None)
        await mutation_engine.dispose()
        await switch_engine.dispose()


async def test_storage_mutation_waits_for_s3_to_local_switch(
    write_barrier_url: str,
    tmp_path: Path,
) -> None:
    switch_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    mutation_engine = create_async_engine(write_barrier_url, poolclass=NullPool)
    switch_entered = asyncio.Event()
    release_switch = asyncio.Event()
    mutation_entered = asyncio.Event()

    file_storage._set_storage_settings_unchecked(
        Settings(data_dir=str(tmp_path), storage_backend="local")
    )
    file_storage._configure_storage_runtime_from_settings_payload_unchecked(
        {
            "s3": {
                "enabled": True,
                "bucket": "barrier-bucket",
                "accessKey": "barrier-access",
                "secret": "barrier-secret",
            }
        }
    )

    async def switch_to_local() -> None:
        async with storage_runtime_switch_barrier(switch_engine) as lease:
            file_storage.configure_storage_runtime_from_settings_payload(
                {"s3": {"enabled": False}},
                lease=lease,
            )
            switch_entered.set()
            await release_switch.wait()

    async def mutate() -> None:
        async with mutation_write_barrier(mutation_engine):
            mutation_entered.set()

    switch_task = asyncio.create_task(switch_to_local())
    mutation_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(switch_entered.wait(), timeout=1)
        assert file_storage.get_storage_backend() == "local"
        mutation_task = asyncio.create_task(mutate())
        await asyncio.sleep(0.1)
        assert not mutation_entered.is_set()

        release_switch.set()
        await asyncio.wait_for(mutation_entered.wait(), timeout=2)
        await asyncio.gather(switch_task, mutation_task)
    finally:
        release_switch.set()
        if not switch_task.done():
            switch_task.cancel()
        if mutation_task is not None and not mutation_task.done():
            mutation_task.cancel()
        await asyncio.gather(
            switch_task,
            *([mutation_task] if mutation_task is not None else []),
            return_exceptions=True,
        )
        file_storage._clear_runtime_storage_overrides_unchecked()
        file_storage._set_storage_settings_unchecked(None)
        await switch_engine.dispose()
        await mutation_engine.dispose()


async def test_settings_commit_ambiguity_reconciles_runtime_from_durable_value(
    barrier_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _seed_durable_settings(
        barrier_engine,
        {"s3": {"enabled": False}},
    )
    runtime_settings = _prepare_local_settings_runtime(tmp_path)
    request = _SettingsPatchRequest(
        {
            "s3": {
                "enabled": True,
                "bucket": "durable-bucket",
                "accessKey": "durable-access",
                "secret": "durable-secret",
            }
        },
        runtime_settings,
    )
    original_commit = settings_api._commit_settings_update

    async def commit_then_lose_acknowledgement(writer: AsyncSession) -> None:
        await original_commit(writer)
        raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(settings_api, "get_engine", lambda: barrier_engine)
    monkeypatch.setattr(
        settings_api,
        "_commit_settings_update",
        commit_then_lose_acknowledgement,
    )

    try:
        with pytest.raises(RuntimeError, match="acknowledgement lost"):
            await settings_api.update_settings(
                request,  # type: ignore[arg-type]
                _admin={},
                session=_SettingsDependencySession(),  # type: ignore[arg-type]
            )

        durable = await _read_durable_settings(barrier_engine)
        assert durable["s3"]["enabled"] is True
        _assert_runtime_reconciled_to_s3(request)
    finally:
        file_storage._clear_runtime_storage_overrides_unchecked()
        file_storage._set_storage_settings_unchecked(None)


async def test_settings_cancellation_after_commit_waits_for_reconciliation(
    barrier_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _seed_durable_settings(
        barrier_engine,
        {"s3": {"enabled": False}},
    )
    runtime_settings = _prepare_local_settings_runtime(tmp_path)
    request = _SettingsPatchRequest(
        {
            "s3": {
                "enabled": True,
                "bucket": "durable-bucket",
                "accessKey": "durable-access",
                "secret": "durable-secret",
            }
        },
        runtime_settings,
    )
    original_commit = settings_api._commit_settings_update
    original_reconciliation = (
        settings_api._reconcile_storage_runtime_from_durable_settings
    )
    committed = asyncio.Event()
    never_release = asyncio.Event()
    reconciliation_started = asyncio.Event()
    continue_reconciliation = asyncio.Event()

    async def commit_then_pause(writer: AsyncSession) -> None:
        await original_commit(writer)
        committed.set()
        await never_release.wait()

    async def pause_then_reconcile(request_arg, engine_arg):
        reconciliation_started.set()
        await continue_reconciliation.wait()
        return await original_reconciliation(request_arg, engine_arg)

    monkeypatch.setattr(settings_api, "get_engine", lambda: barrier_engine)
    monkeypatch.setattr(
        settings_api,
        "_commit_settings_update",
        commit_then_pause,
    )
    monkeypatch.setattr(
        settings_api,
        "_reconcile_storage_runtime_from_durable_settings",
        pause_then_reconcile,
    )

    update_task = asyncio.create_task(
        settings_api.update_settings(
            request,  # type: ignore[arg-type]
            _admin={},
            session=_SettingsDependencySession(),  # type: ignore[arg-type]
        )
    )
    try:
        await asyncio.wait_for(committed.wait(), timeout=2)
        update_task.cancel()
        await asyncio.wait_for(reconciliation_started.wait(), timeout=2)
        # A repeated cancellation while recovery is already waiting must not
        # abandon the independent reconciliation task.
        update_task.cancel()
        continue_reconciliation.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(update_task), timeout=3)

        durable = await _read_durable_settings(barrier_engine)
        assert durable["s3"]["enabled"] is True
        _assert_runtime_reconciled_to_s3(request)
    finally:
        never_release.set()
        continue_reconciliation.set()
        if not update_task.done():
            update_task.cancel()
        await asyncio.gather(update_task, return_exceptions=True)
        file_storage._clear_runtime_storage_overrides_unchecked()
        file_storage._set_storage_settings_unchecked(None)


async def test_settings_connection_loss_after_commit_reconciles_on_new_lease(
    barrier_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _seed_durable_settings(
        barrier_engine,
        {"s3": {"enabled": False}},
    )
    runtime_settings = _prepare_local_settings_runtime(tmp_path)
    request = _SettingsPatchRequest(
        {
            "s3": {
                "enabled": True,
                "bucket": "durable-bucket",
                "accessKey": "durable-access",
                "secret": "durable-secret",
            }
        },
        runtime_settings,
    )
    original_commit = settings_api._commit_settings_update

    async def commit_then_drop_connection(writer: AsyncSession) -> None:
        await original_commit(writer)
        connection = await writer.connection()
        await connection.invalidate()
        raise WriteBarrierConnectionLostError("connection lost after commit")

    monkeypatch.setattr(settings_api, "get_engine", lambda: barrier_engine)
    monkeypatch.setattr(
        settings_api,
        "_commit_settings_update",
        commit_then_drop_connection,
    )

    try:
        with pytest.raises(
            WriteBarrierConnectionLostError,
            match="connection lost after commit",
        ):
            await settings_api.update_settings(
                request,  # type: ignore[arg-type]
                _admin={},
                session=_SettingsDependencySession(),  # type: ignore[arg-type]
            )

        durable = await _read_durable_settings(barrier_engine)
        assert durable["s3"]["enabled"] is True
        _assert_runtime_reconciled_to_s3(request)
    finally:
        file_storage._clear_runtime_storage_overrides_unchecked()
        file_storage._set_storage_settings_unchecked(None)
