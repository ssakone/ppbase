"""PostgreSQL coordination barrier for database/file mutations and backups.

The barrier is a PostgreSQL session-level advisory lock.  Every mutation that
must keep database rows and local files coherent takes the shared variant;
backup creation takes the exclusive variant and then the migration lock.  A
lease owns exactly one checked-out connection, and callers must run all SQL
covered by the lease through ``lease.connection``.  This is required both for
session affinity and for operation with ``pool_size=1``.
PgBouncer transaction and statement pooling are therefore unsupported; only
direct PostgreSQL sessions or session-pooling proxies preserve the lease.

Backup resources must remain unsealed while ``backup_write_barrier`` is
active.  A caller may seal them only after the context exits successfully.
The exit path verifies that the original PostgreSQL backend still owns both
advisory locks.  A lost/replaced connection therefore fails closed and the
caller never reaches its sealing step.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ppbase.services.migration_runner import (
    MigrationLockError,
    migration_lock_on_connection,
)


logger = logging.getLogger(__name__)


class WriteBarrierError(RuntimeError):
    """Base error for the PostgreSQL database/file write barrier."""


class WriteBarrierTimeoutError(WriteBarrierError):
    """Raised when a connection or advisory lock cannot be acquired in time."""


class WriteBarrierConnectionLostError(WriteBarrierError):
    """Raised when the session holding a barrier lease is lost or replaced."""


class WriteBarrierMode(str, Enum):
    """Supported advisory-lock modes."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


_WRITE_BARRIER_SEED = 0x505042415345  # "PPBASE"
_WRITE_BARRIER_POLL_SECONDS = 0.05


@dataclass(slots=True)
class WriteBarrierLease:
    """A session-affine barrier lease held by one dedicated connection.

    The connection is intentionally public: mutation and backup code must
    reuse it instead of checking another connection out of the engine.  This
    prevents self-deadlock with a one-connection pool and preserves the
    PostgreSQL session that owns the advisory locks.
    """

    connection: AsyncConnection
    mode: WriteBarrierMode
    backend_pid: int
    barrier_key: int
    source_engine: AsyncEngine | None = field(default=None, repr=False)
    owns_connection: bool = field(default=True, repr=False)
    _barrier_acquired: bool = field(default=False, init=False, repr=False)
    _active: bool = field(default=False, init=False, repr=False)
    _owner_task: asyncio.Task[Any] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    @property
    def active(self) -> bool:
        """Whether this task may still perform work under the lease."""
        return (
            self._active
            and self._barrier_acquired
            and self.owned_by_current_task
        )

    @property
    def owned_by_current_task(self) -> bool:
        """Whether the current asyncio task owns this session-affine lease."""
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        return self._owner_task is current_task


_current_write_barrier: ContextVar[WriteBarrierLease | None] = ContextVar(
    "ppbase_current_write_barrier",
    default=None,
)


def _validate_timeout(timeout_seconds: float) -> None:
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError(
            "Write barrier timeout must be a finite non-negative value."
        )


async def _write_barrier_identity(
    connection: AsyncConnection,
) -> tuple[int, int]:
    result = await connection.execute(
        text(
            "SELECT "
            "pg_catalog.hashtextextended("
            "pg_catalog.concat("
            "pg_catalog.current_database(), "
            "':ppbase:backup-write-barrier'"
            "), :seed"
            ") AS barrier_key, "
            "pg_catalog.pg_backend_pid() AS backend_pid"
        ),
        {"seed": _WRITE_BARRIER_SEED},
    )
    row = result.one()
    return int(row.barrier_key), int(row.backend_pid)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


async def _acquire_lock(
    connection: AsyncConnection,
    *,
    key: int,
    mode: WriteBarrierMode,
    deadline: float,
    label: str,
    finish_transaction: bool,
) -> None:
    function_name = (
        "pg_catalog.pg_try_advisory_lock_shared"
        if mode is WriteBarrierMode.SHARED
        else "pg_catalog.pg_try_advisory_lock"
    )

    while True:
        try:
            result = await connection.execute(
                text(f"SELECT {function_name}(:key)"),
                {"key": key},
            )
            acquired = bool(result.scalar_one())
            if finish_transaction:
                # SELECT starts an implicit transaction. The advisory lock is
                # session-level and remains held across this commit.
                await connection.commit()
        except Exception as exc:
            raise WriteBarrierConnectionLostError(
                f"Lost the PostgreSQL session while acquiring {label}."
            ) from exc

        if acquired:
            return

        remaining = _remaining(deadline)
        if remaining <= 0:
            raise WriteBarrierTimeoutError(
                f"Timed out waiting for {label}."
            )

        if mode is WriteBarrierMode.EXCLUSIVE:
            # A failed try-lock is not queued by PostgreSQL, so repeatedly
            # polling here would let a continuous stream of compatible shared
            # lockers overtake the backup forever.  Join PostgreSQL's lock
            # wait queue instead: once this exclusive waiter is visible, later
            # shared requests are soft-blocked behind it while current shared
            # holders drain.
            timeout = asyncio.timeout(remaining)
            try:
                async with timeout:
                    await connection.execute(
                        text("SELECT pg_catalog.pg_advisory_lock(:key)"),
                        {"key": key},
                    )
                    if finish_transaction:
                        # The lock is session-level and survives this commit.
                        await connection.commit()
            except TimeoutError as exc:
                if timeout.expired():
                    raise WriteBarrierTimeoutError(
                        f"Timed out waiting for {label}."
                    ) from exc
                raise WriteBarrierConnectionLostError(
                    f"Lost the PostgreSQL session while acquiring {label}."
                ) from exc
            except Exception as exc:
                raise WriteBarrierConnectionLostError(
                    f"Lost the PostgreSQL session while acquiring {label}."
                ) from exc
            return

        await asyncio.sleep(min(_WRITE_BARRIER_POLL_SECONDS, remaining))


async def _release_lock(
    lease: WriteBarrierLease,
    *,
    key: int,
    mode: WriteBarrierMode,
    label: str,
    finish_transaction: bool,
) -> None:
    function_name = (
        "pg_catalog.pg_advisory_unlock_shared"
        if mode is WriteBarrierMode.SHARED
        else "pg_catalog.pg_advisory_unlock"
    )
    try:
        result = await lease.connection.execute(
            text(
                f"SELECT pg_catalog.pg_backend_pid() AS backend_pid, "
                f"{function_name}(:key) AS unlocked"
            ),
            {"key": key},
        )
        row = result.one()
        if finish_transaction:
            await lease.connection.commit()
    except Exception as exc:
        raise WriteBarrierConnectionLostError(
            f"Lost the PostgreSQL session while releasing {label}."
        ) from exc

    if int(row.backend_pid) != lease.backend_pid or not bool(row.unlocked):
        raise WriteBarrierConnectionLostError(
            f"The PostgreSQL session holding {label} was lost or replaced."
        )
    lease._barrier_acquired = False


async def _cleanup_connection(connection: AsyncConnection) -> None:
    """Invalidate a suspect session even if the owning task is cancelled again.

    Session-level advisory locks survive transaction rollback and normal pool
    return.  Once acquisition or release has an ambiguous outcome, the only
    safe cleanup is therefore to terminate the physical PostgreSQL session.
    Run invalidation in its own task and shield it until completion so repeated
    cancellation of the request cannot return a possibly locked session to the
    pool.
    """
    invalidation = asyncio.create_task(connection.invalidate())
    while True:
        try:
            await asyncio.shield(invalidation)
            break
        except asyncio.CancelledError:
            if invalidation.done():
                break
            continue
        except Exception:
            break

    try:
        invalidation.result()
    except Exception:
        logger.exception("Failed to invalidate a write-barrier connection")


async def _release_lease(lease: WriteBarrierLease) -> None:
    """Release locks in reverse order and prove session ownership.

    Successful unlock responses are the final lease-health check.  If the
    original backend disappeared, SQLAlchemy may reconnect transparently; the
    stored backend PID and PostgreSQL's unlock result detect that replacement.
    """
    connection = lease.connection
    if connection.closed or connection.invalidated:
        raise WriteBarrierConnectionLostError(
            "The PostgreSQL session holding the write barrier was lost."
        )

    try:
        # The lease never commits caller work implicitly.  Backup callers are
        # read-only on this connection; mutation callers must finish their
        # transaction before leaving the context.
        if lease.owns_connection and connection.in_transaction():
            await connection.rollback()

        if lease._barrier_acquired:
            await _release_lock(
                lease,
                key=lease.barrier_key,
                mode=lease.mode,
                label=f"the {lease.mode.value} PPBase write barrier",
                finish_transaction=lease.owns_connection,
            )
    except BaseException:
        await _cleanup_connection(connection)
        raise


async def _open_lease(
    engine: AsyncEngine,
    *,
    mode: WriteBarrierMode,
    timeout_seconds: float,
) -> WriteBarrierLease:
    _validate_timeout(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds

    try:
        connection = await asyncio.wait_for(
            engine.connect(),
            timeout=max(_remaining(deadline), 0.001),
        )
    except (TimeoutError, SQLAlchemyTimeoutError) as exc:
        raise WriteBarrierTimeoutError(
            "Timed out waiting for the dedicated PostgreSQL write-barrier "
            "connection."
        ) from exc
    except Exception as exc:
        raise WriteBarrierConnectionLostError(
            "Could not open the dedicated PostgreSQL write-barrier connection."
        ) from exc

    lease: WriteBarrierLease | None = None
    try:
        try:
            barrier_key, backend_pid = await _write_barrier_identity(connection)
            await connection.commit()
        except Exception as exc:
            raise WriteBarrierConnectionLostError(
                "Could not establish the PostgreSQL write-barrier session."
            ) from exc

        lease = WriteBarrierLease(
            connection=connection,
            mode=mode,
            backend_pid=backend_pid,
            barrier_key=barrier_key,
            source_engine=engine,
            owns_connection=True,
        )
        await _acquire_lock(
            connection,
            key=barrier_key,
            mode=mode,
            deadline=deadline,
            label=f"the {mode.value} PPBase write barrier",
            finish_transaction=True,
        )
        lease._barrier_acquired = True

        return lease
    except BaseException:
        if lease is not None and lease._barrier_acquired:
            try:
                await _release_lease(lease)
            except BaseException:
                logger.exception(
                    "Failed to clean up a partially acquired write barrier"
                )
        else:
            # AsyncConnection.close() normally returns its physical connection
            # to the pool.  It therefore cannot be the cleanup boundary for an
            # ambiguously acquired session-level advisory lock: invalidate the
            # backend first so a locked session is never pooled.
            await _cleanup_connection(connection)
        try:
            await connection.close()
        except BaseException:
            logger.exception(
                "Failed to close a partially acquired write-barrier connection"
            )
        raise


async def _open_lease_on_connection(
    connection: AsyncConnection,
    *,
    mode: WriteBarrierMode,
    timeout_seconds: float,
) -> WriteBarrierLease:
    """Acquire a barrier without committing or closing a caller connection."""
    _validate_timeout(timeout_seconds)
    if connection.closed or connection.invalidated:
        raise WriteBarrierConnectionLostError(
            "The supplied PostgreSQL write-barrier connection is not open."
        )

    deadline = time.monotonic() + timeout_seconds
    lease: WriteBarrierLease | None = None
    try:
        barrier_key, backend_pid = await _write_barrier_identity(connection)
        lease = WriteBarrierLease(
            connection=connection,
            mode=mode,
            backend_pid=backend_pid,
            barrier_key=barrier_key,
            owns_connection=False,
        )
        await _acquire_lock(
            connection,
            key=barrier_key,
            mode=mode,
            deadline=deadline,
            label=f"the {mode.value} PPBase write barrier",
            finish_transaction=False,
        )
        lease._barrier_acquired = True
        return lease
    except BaseException:
        if lease is not None and lease._barrier_acquired:
            try:
                await _release_lease(lease)
            except BaseException:
                logger.exception(
                    "Failed to clean up a borrowed write-barrier lease"
                )
        else:
            # A cancellation or transport error may arrive after PostgreSQL
            # granted the session-level lock but before the client observed the
            # result.  Rollback cannot release such a lock, so a borrowed
            # connection with an unproven acquisition outcome must never be
            # returned to the pool.
            await _cleanup_connection(connection)
        raise


def current_write_barrier_lease() -> WriteBarrierLease | None:
    """Return the active lease in this async context, if any.

    Detached tasks inherit ContextVars but never the right to use the owning
    task's PostgreSQL session.  They therefore see no current lease and must
    acquire their own connection/lock.
    """
    lease = _current_write_barrier.get()
    return (
        lease
        if lease is not None
        and lease.active
        and lease.owned_by_current_task
        else None
    )


def require_mutation_write_barrier(
    lease: WriteBarrierLease | None = None,
) -> WriteBarrierLease:
    """Fail closed unless a live shared mutation lease is available."""
    active = lease or current_write_barrier_lease()
    if (
        active is None
        or not active.active
        or not active.owned_by_current_task
    ):
        raise WriteBarrierError(
            "A live shared PPBase write-barrier lease owned by the current "
            "asyncio task is required."
        )
    if active.mode is not WriteBarrierMode.SHARED:
        raise WriteBarrierError(
            "Storage mutations are forbidden inside the exclusive PPBase "
            "write barrier."
        )
    return active


def require_exclusive_write_barrier(
    lease: WriteBarrierLease | None = None,
) -> WriteBarrierLease:
    """Fail closed unless this task owns a live exclusive barrier lease."""
    active = lease or current_write_barrier_lease()
    if (
        active is None
        or not active.active
        or not active.owned_by_current_task
    ):
        raise WriteBarrierError(
            "A live exclusive PPBase write-barrier lease owned by the current "
            "asyncio task is required."
        )
    if active.mode is not WriteBarrierMode.EXCLUSIVE:
        raise WriteBarrierError(
            "An exclusive PPBase write-barrier lease is required."
        )
    return active


async def assert_write_barrier_held(lease: WriteBarrierLease) -> None:
    """Prove that the original backend still owns this advisory lock."""
    if not lease.active or not lease.owned_by_current_task:
        raise WriteBarrierConnectionLostError(
            "The PostgreSQL write-barrier lease is no longer active in its "
            "owning asyncio task."
        )
    connection = lease.connection
    if connection.closed or connection.invalidated:
        raise WriteBarrierConnectionLostError(
            "The PostgreSQL session holding the write barrier was lost."
        )

    unsigned_key = lease.barrier_key & ((1 << 64) - 1)
    class_id = (unsigned_key >> 32) & 0xFFFFFFFF
    object_id = unsigned_key & 0xFFFFFFFF
    expected_mode = (
        "ShareLock"
        if lease.mode is WriteBarrierMode.SHARED
        else "ExclusiveLock"
    )
    try:
        result = await connection.execute(
            text(
                "SELECT pg_catalog.pg_backend_pid() AS backend_pid, EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_locks "
                "WHERE pid OPERATOR(pg_catalog.=) "
                "pg_catalog.pg_backend_pid() "
                "AND locktype OPERATOR(pg_catalog.=) 'advisory' AND granted "
                "AND objsubid OPERATOR(pg_catalog.=) 1 "
                "AND classid::bigint OPERATOR(pg_catalog.=) :class_id "
                "AND objid::bigint OPERATOR(pg_catalog.=) :object_id "
                "AND mode OPERATOR(pg_catalog.=) :lock_mode"
                ") AS owns_lock"
            ),
            {
                "class_id": class_id,
                "object_id": object_id,
                "lock_mode": expected_mode,
            },
        )
        row = result.one()
    except Exception as exc:
        await _cleanup_connection(connection)
        raise WriteBarrierConnectionLostError(
            "Could not verify the PostgreSQL write-barrier lease."
        ) from exc

    if int(row.backend_pid) != lease.backend_pid or not bool(row.owns_lock):
        await _cleanup_connection(connection)
        raise WriteBarrierConnectionLostError(
            "The PostgreSQL session holding the write barrier was lost or "
            "replaced."
        )


@asynccontextmanager
async def mutation_write_barrier(
    engine: AsyncEngine,
    *,
    timeout_seconds: float = 30.0,
) -> AsyncIterator[WriteBarrierLease]:
    """Hold the shared database/file mutation barrier.

    Database work covered by this lease must use ``lease.connection``.  Hooks,
    batches, thumbnail generation, and direct storage writers are not safe
    merely because this primitive exists; their DB/file mutation entry points
    must join this same context before they are considered coordinated.
    """
    current = current_write_barrier_lease()
    if current is not None:
        if current.mode is not WriteBarrierMode.SHARED:
            raise WriteBarrierError(
                "Cannot start a storage mutation inside the exclusive backup "
                "barrier."
            )
        if current.source_engine is not None and current.source_engine is not engine:
            raise WriteBarrierError(
                "A nested mutation cannot switch PostgreSQL engines while a "
                "write-barrier lease is active."
            )
        yield current
        return

    async with _lease_context(
        engine,
        mode=WriteBarrierMode.SHARED,
        timeout_seconds=timeout_seconds,
    ) as lease:
        yield lease


@asynccontextmanager
async def mutation_write_barrier_on_connection(
    connection: AsyncConnection,
    *,
    timeout_seconds: float = 30.0,
) -> AsyncIterator[WriteBarrierLease]:
    """Hold a shared barrier on a caller-owned connection/transaction.

    This is intended for request paths, such as thumbnail cache publication,
    that already checked out the only pool connection. The context neither
    commits nor rolls back nor closes the supplied connection.
    """
    current = current_write_barrier_lease()
    if current is not None:
        if current.mode is not WriteBarrierMode.SHARED:
            raise WriteBarrierError(
                "Cannot start a storage mutation inside the exclusive backup "
                "barrier."
            )
        if current.connection is not connection:
            raise WriteBarrierError(
                "A nested mutation cannot switch PostgreSQL connections while "
                "a write-barrier lease is active."
            )
        yield current
        return

    lease = await _open_lease_on_connection(
        connection,
        mode=WriteBarrierMode.SHARED,
        timeout_seconds=timeout_seconds,
    )
    async with _active_lease(lease):
        yield lease


@asynccontextmanager
async def backup_write_barrier(
    engine: AsyncEngine,
    *,
    timeout_seconds: float = 30.0,
) -> AsyncIterator[WriteBarrierLease]:
    """Hold the exclusive write barrier and then the migration lock.

    Both session-level locks use one dedicated connection, so this context is
    safe with ``pool_size=1``.  Build only an unsealed backup inside the
    context.  Seal/publish it after the context exits successfully.
    """
    if current_write_barrier_lease() is not None:
        raise WriteBarrierError(
            "The exclusive backup barrier cannot be nested or promoted from "
            "a shared mutation lease."
        )
    _validate_timeout(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    async with _lease_context(
        engine,
        mode=WriteBarrierMode.EXCLUSIVE,
        timeout_seconds=timeout_seconds,
    ) as lease:
        body_error: BaseException | None = None
        try:
            async with migration_lock_on_connection(
                lease.connection,
                timeout_seconds=_remaining(deadline),
            ):
                try:
                    yield lease
                except BaseException as exc:
                    body_error = exc
                    raise
        except MigrationLockError as exc:
            if body_error is not None:
                raise
            if "Timed out" in str(exc):
                raise WriteBarrierTimeoutError(
                    "Timed out waiting for the PPBase migration lock."
                ) from exc
            raise WriteBarrierConnectionLostError(
                "The PostgreSQL session holding the ordered backup locks was "
                "lost."
            ) from exc


@asynccontextmanager
async def storage_runtime_switch_barrier(
    engine: AsyncEngine,
    *,
    timeout_seconds: float = 30.0,
) -> AsyncIterator[WriteBarrierLease]:
    """Serialize a live local/S3 switch against every storage mutation.

    The switch uses the same ordered exclusive-barrier then migration-lock
    protocol as backup creation.  Both locks and settings SQL share one
    dedicated connection, including when the pool has size one.
    """
    async with backup_write_barrier(
        engine,
        timeout_seconds=timeout_seconds,
    ) as lease:
        yield lease


@asynccontextmanager
async def _lease_context(
    engine: AsyncEngine,
    *,
    mode: WriteBarrierMode,
    timeout_seconds: float,
) -> AsyncIterator[WriteBarrierLease]:
    lease = await _open_lease(
        engine,
        mode=mode,
        timeout_seconds=timeout_seconds,
    )
    async with _active_lease(lease):
        yield lease


@asynccontextmanager
async def _active_lease(
    lease: WriteBarrierLease,
) -> AsyncIterator[WriteBarrierLease]:
    """Expose one lease to nested code and clean it up exactly once."""
    body_error: BaseException | None = None
    release_error: BaseException | None = None
    token = _current_write_barrier.set(lease)
    owner_task = asyncio.current_task()
    if owner_task is None:  # pragma: no cover - async context invariant
        raise WriteBarrierError(
            "A write-barrier lease requires an owning asyncio task."
        )
    lease._owner_task = owner_task
    lease._active = True

    try:
        try:
            yield lease
        except BaseException as exc:
            body_error = exc
            raise
    finally:
        lease._active = False
        lease._owner_task = None
        _current_write_barrier.reset(token)
        try:
            await _release_lease(lease)
        except BaseException as exc:
            release_error = exc
            if body_error is not None:
                logger.exception(
                    "Failed to release the write barrier while handling "
                    "another operation error"
                )

        close_error: BaseException | None = None
        if lease.owns_connection:
            try:
                await lease.connection.close()
            except BaseException as exc:
                close_error = exc
                if body_error is not None or release_error is not None:
                    logger.exception(
                        "Failed to close the write-barrier connection while "
                        "handling another error"
                    )

        if body_error is None:
            if release_error is not None:
                raise release_error
            if close_error is not None:
                raise close_error
