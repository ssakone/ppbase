"""One coordinated transaction for record rows and local storage changes.

The coordinator pins record SQL, synchronous hooks, file writes, commit,
ambiguous-outcome reconciliation, and deferred deletes to one shared
write-barrier lease. Nested repository calls reuse the same connection and
trackers instead of checking out another connection or acquiring another lock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ppbase.core.storage_safety import StorageSafetyError
from ppbase.services.file_references import read_canonical_local_file_references
from ppbase.services.record_service import get_all_collections
from ppbase.services.write_barrier import (
    WriteBarrierConnectionLostError,
    WriteBarrierError,
    WriteBarrierLease,
    assert_write_barrier_held,
    mutation_write_barrier,
)


logger = logging.getLogger(__name__)
T = TypeVar("T")


class ConnectionEngineAdapter:
    """Minimal engine-shaped adapter pinned to one ``AsyncConnection``."""

    def __init__(
        self,
        connection: AsyncConnection,
        *,
        source_engine: AsyncEngine | None = None,
    ):
        self.connection = connection
        self.source_engine = source_engine

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        yield self.connection

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncConnection]:
        # The outer coordinator owns the root transaction. Record service
        # helpers may freely use their normal engine.begin() API without
        # committing or checking out another connection.
        yield self.connection


@dataclass(slots=True)
class _RecordStorageState:
    lease: WriteBarrierLease
    engine: ConnectionEngineAdapter
    source_engine: AsyncEngine
    owner_task: asyncio.Task[Any]
    active: bool = True


_current_record_storage_state: ContextVar[_RecordStorageState | None] = ContextVar(
    "ppbase_record_storage_state",
    default=None,
)


def current_record_storage_engine() -> ConnectionEngineAdapter | None:
    """Return the active pinned engine for synchronous hook/repository reuse."""
    state = _current_record_storage_state.get()
    if (
        state is None
        or not state.active
        or not state.lease.active
        or state.owner_task is not asyncio.current_task()
    ):
        return None
    return state.engine


async def _final_file_references(
    engine: Any,
    all_collections: list[Any],
    targets: set[tuple[str, str]],
) -> set[tuple[str, str, str]]:
    async with engine.connect() as connection:
        return set(
            await read_canonical_local_file_references(
                connection,
                all_collections,
                targets=targets,
            )
        )


def _affected_records(
    created_file_targets: set[tuple[str, str, str]],
    deferred_storage_deletes: list[tuple[str, str, str, tuple[str, ...]]],
) -> set[tuple[str, str]]:
    affected = {
        (collection_id, record_id)
        for collection_id, record_id, _filename in created_file_targets
    }
    affected.update(
        (collection_id, record_id)
        for _action, collection_id, record_id, _filenames
        in deferred_storage_deletes
    )
    return affected


def _cleanup_created_files(
    created_targets: set[tuple[str, str, str]],
    *,
    lease: WriteBarrierLease,
) -> None:
    from ppbase.services.file_storage import (
        delete_files,
        delete_storage_dir_if_empty,
    )

    record_targets: set[tuple[str, str]] = set()
    for collection_id, record_id, filename in created_targets:
        record_targets.add((collection_id, record_id))
        try:
            delete_files(
                collection_id,
                record_id,
                [filename],
                lease=lease,
            )
        except (OSError, StorageSafetyError):
            logger.warning("Skipped unsafe or failed storage cleanup")

    for collection_id, record_id in record_targets:
        try:
            delete_storage_dir_if_empty(
                collection_id,
                record_id,
                lease=lease,
            )
        except (OSError, StorageSafetyError):
            logger.warning("Skipped unsafe or failed empty storage-dir cleanup")


def _cleanup_unreferenced_created_files(
    created_file_targets: set[tuple[str, str, str]],
    final_file_references: set[tuple[str, str, str]],
    *,
    lease: WriteBarrierLease,
) -> None:
    orphaned_writes = created_file_targets - final_file_references
    if orphaned_writes:
        _cleanup_created_files(orphaned_writes, lease=lease)


async def _reconcile_durable_references(
    engine: ConnectionEngineAdapter,
    targets: set[tuple[str, str]],
) -> set[tuple[str, str, str]] | None:
    if not targets:
        return set()
    try:
        all_collections = await get_all_collections(engine)
        return await _final_file_references(engine, all_collections, targets)
    except Exception:
        logger.exception(
            "Unable to reconcile durable storage references after an "
            "ambiguous database commit"
        )
        return None


def _nested_state_for(engine: Any) -> _RecordStorageState | None:
    state = _current_record_storage_state.get()
    if (
        state is None
        or not state.active
        or not state.lease.active
        or state.owner_task is not asyncio.current_task()
    ):
        return None
    if engine not in {state.source_engine, state.engine}:
        raise WriteBarrierError(
            "A nested record mutation cannot switch database engines."
        )
    return state


async def run_record_storage_transaction(
    engine: AsyncEngine | ConnectionEngineAdapter,
    operation: Callable[[ConnectionEngineAdapter], Awaitable[T]],
    *,
    barrier_timeout_seconds: float = 30.0,
) -> T:
    """Run one record/files mutation under a shared session-level barrier."""
    nested = _nested_state_for(engine)
    if nested is not None:
        return await operation(nested.engine)
    if isinstance(engine, ConnectionEngineAdapter):
        if engine.source_engine is None:
            raise WriteBarrierError(
                "A detached record mutation cannot recover its PPBase "
                "AsyncEngine from this connection adapter."
            )
        engine = engine.source_engine
    if not isinstance(engine, AsyncEngine):
        raise WriteBarrierError(
            "A standalone record mutation requires the PPBase AsyncEngine."
        )

    from ppbase.services.file_storage import (
        capture_storage_writes,
        defer_storage_deletes,
        flush_deferred_storage_deletes,
        pin_storage_config,
    )

    created_file_targets: set[tuple[str, str, str]] = set()
    deferred_storage_deletes: list[
        tuple[str, str, str, tuple[str, ...]]
    ] = []
    final_file_references: set[tuple[str, str, str]] = set()
    transaction_body_finished = False

    async with mutation_write_barrier(
        engine,
        timeout_seconds=barrier_timeout_seconds,
    ) as lease:
        active_engine = ConnectionEngineAdapter(
            lease.connection,
            source_engine=engine,
        )
        owner_task = asyncio.current_task()
        if owner_task is None:  # pragma: no cover - async function invariant
            raise WriteBarrierError(
                "A record/storage transaction requires an owning asyncio task."
            )
        state = _RecordStorageState(
            lease=lease,
            engine=active_engine,
            source_engine=engine,
            owner_task=owner_task,
        )
        token = _current_record_storage_state.set(state)
        try:
            with pin_storage_config():
                try:
                    with (
                        capture_storage_writes(created_file_targets),
                        defer_storage_deletes(deferred_storage_deletes),
                    ):
                        async with lease.connection.begin():
                            result = await operation(active_engine)
                            affected_records = _affected_records(
                                created_file_targets,
                                deferred_storage_deletes,
                            )
                            if affected_records:
                                all_collections = await get_all_collections(
                                    active_engine
                                )
                                final_file_references = await _final_file_references(
                                    active_engine,
                                    all_collections,
                                    affected_records,
                                )
                            transaction_body_finished = True
                except BaseException as exc:
                    try:
                        if lease.connection.in_transaction():
                            await lease.connection.rollback()
                        await assert_write_barrier_held(lease)
                    except WriteBarrierConnectionLostError as lease_error:
                        if created_file_targets or deferred_storage_deletes:
                            logger.warning(
                                "Preserving storage changes because the shared "
                                "write-barrier session was lost"
                            )
                        raise lease_error from exc

                    if transaction_body_finished and isinstance(exc, Exception):
                        durable_references = await _reconcile_durable_references(
                            active_engine,
                            _affected_records(
                                created_file_targets,
                                deferred_storage_deletes,
                            ),
                        )
                        if durable_references is not None:
                            _cleanup_unreferenced_created_files(
                                created_file_targets,
                                durable_references,
                                lease=lease,
                            )
                        elif created_file_targets:
                            logger.warning(
                                "Preserving %d new storage file(s) because the "
                                "database commit outcome could not be reconciled",
                                len(created_file_targets),
                            )
                        if deferred_storage_deletes:
                            logger.warning(
                                "Preserving %d deferred storage deletion(s) "
                                "after an ambiguous database commit",
                                len(deferred_storage_deletes),
                            )
                    elif transaction_body_finished:
                        logger.warning(
                            "Preserving storage changes because database commit "
                            "was interrupted before its outcome could be reconciled"
                        )
                    elif created_file_targets:
                        _cleanup_created_files(
                            created_file_targets,
                            lease=lease,
                        )
                    raise

                # The commit completed. Prove the shared lease immediately
                # before and after all filesystem reconciliation. If the lease
                # was lost, preserve files rather than acting under a false lock.
                await assert_write_barrier_held(lease)
                _cleanup_unreferenced_created_files(
                    created_file_targets,
                    final_file_references,
                    lease=lease,
                )
                cleanup_failures = flush_deferred_storage_deletes(
                    deferred_storage_deletes,
                    preserved_files=final_file_references,
                    lease=lease,
                )
                if cleanup_failures:
                    logger.warning(
                        "Skipped %d unsafe or conflicting committed storage "
                        "cleanup(s)",
                        cleanup_failures,
                    )
                await assert_write_barrier_held(lease)
                return result
        finally:
            state.active = False
            _current_record_storage_state.reset(token)
