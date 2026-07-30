"""Application orchestration for native backup and destructive restore."""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
import secrets
from typing import Any, BinaryIO, Callable, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ppbase import __version__
from ppbase.backup.control import (
    ControlPlaneSafetyError,
    RuntimeDataRoot,
    absolute_path_without_symlink_resolution,
    ensure_runtime_backup_roots,
)
from ppbase.backup.canonical import (
    fingerprint_collection_objects,
    validate_canonical_collection_objects,
)
from ppbase.backup.archive_store import BackupArchiveStore, validate_backup_key
from ppbase.backup.destructive import (
    build_file_reference_inventory,
    DestructiveRestoreError,
    DestructiveRestoreJournal,
    normalize_file_reference_inventory,
    PreparedStorageRestore,
    prepare_storage_restore,
    read_database_restore_marker,
    validate_committed_destructive_restore,
)
from ppbase.backup.disk import (
    BackupDiskSpaceError,
    local_tree_size,
    require_disk_space,
)
from ppbase.backup.models import (
    BackupError,
    BackupIntegrityError,
    BackupManifest,
    BackupNotFoundError,
    format_manifest_timestamp,
)
from ppbase.backup.operations import (
    BackupOperationCoordinator,
    BackupOperationError,
    BackupOperationLease,
    BackupOperationSafetyError,
)
from ppbase.backup.postgres import (
    DatabaseContract,
    PostgresBackupError,
    inspect_database_contract,
    preflight_destructive_restore_role,
    set_backup_control_search_path,
)
from ppbase.backup.native import (
    collect_applied_migrations,
    export_native_database,
    restore_native_database,
    verify_migration_hashes_on_disk,
)
from ppbase.backup.schema_contract import DatabaseSchema
from ppbase.backup.storage import (
    DATA_COPY_RESOURCE,
    SCHEMA_JSON_RESOURCE,
    VerifiedBackupInspection,
    BackupDeletionUncertainError,
    BackupSealCancelledError,
    BackupSealGate,
    LocalBackupStore,
)
from ppbase.backup.transport import (
    BackupTransportError,
    BackupTransportLimits,
    PinnedBackupZip,
    PreparedBackupImport,
    backup_transport_filename_from_parts,
    materialize_backup_zip,
    prepare_backup_zip_import,
    validate_backup_transport_filename,
)
from ppbase.services.async_utils import (
    to_thread_quiescent as _to_thread_quiescent,
)
from ppbase.services.file_storage import (
    get_storage_file_path,
    open_file_stream,
    pin_storage_config,
    resolve_storage_config_from_settings_payload,
)
from ppbase.services.file_references import (
    LocalFileReference,
    read_canonical_local_file_references,
)
from ppbase.services.process_control import (
    ProcessRestartReservation,
    get_restart_command,
    is_restart_scheduled,
    reserve_process_restart,
)
from ppbase.services.write_barrier import (
    RetainedBackupWriteBarrier,
    WriteBarrierConnectionLostError,
    WriteBarrierError,
    WriteBarrierTimeoutError,
    acquire_retained_backup_write_barrier,
    backup_write_barrier,
)


_FILE_REFERENCE_INVENTORY_KEY = "local_file_reference_inventory"
_T = TypeVar("_T")
_RETAINED_CUTOVER_GUARDS: set["BackupCutoverGuard"] = set()


class BackupCutoverGuard:
    """Retain every cutover exclusion until exec or durable rollback.

    The PostgreSQL barrier is released first and filesystem operation leases
    second, but only after the destructive restore journal has recorded a safe
    outcome. Instances keep themselves alive so an exceptional
    scheduling path cannot accidentally drop the last Python reference and
    reopen the mutation window.
    """

    def __init__(
        self,
        barrier: RetainedBackupWriteBarrier,
        *,
        restart_reservation: ProcessRestartReservation,
    ) -> None:
        self._barrier = barrier
        self.restart_reservation = restart_reservation
        self._operation_cleanups = ExitStack()
        self._operation_cleanups.callback(restart_reservation.release)
        self._loop = asyncio.get_running_loop()
        self._close_lock = asyncio.Lock()
        self._closed = False
        _RETAINED_CUTOVER_GUARDS.add(self)

    @property
    def active(self) -> bool:
        return not self._closed and self._barrier.active

    async def verify_held(self) -> None:
        async with self._close_lock:
            if self._closed:
                raise WriteBarrierConnectionLostError(
                    "The backup cutover guard is already closed."
                )
            await self._barrier.verify_held()

    def retain_operation_context(self, context: Any) -> None:
        """Transfer an already-entered synchronous lease context to the guard."""
        if self._closed:
            raise RuntimeError("Cannot retain an operation lease on a closed guard")
        self._operation_cleanups.callback(context.__exit__, None, None, None)

    async def close(self) -> None:
        """Release all guards after the caller has durably rolled back."""
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Cutover guard must close on its originating event loop")
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            first_error: BaseException | None = None
            try:
                await self._barrier.close()
            except BaseException as exc:
                first_error = exc
            try:
                self._operation_cleanups.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            _RETAINED_CUTOVER_GUARDS.discard(self)
            if first_error is not None:
                raise first_error

    def close_from_restart_thread(self, *, timeout_seconds: float = 30.0) -> None:
        """Release loop-affine resources after a failed ``os.execvpe``."""
        if self._closed:
            return
        if self._loop.is_closed() or not self._loop.is_running():
            raise RuntimeError(
                "The cutover event loop stopped before its guards were released"
            )
        future = asyncio.run_coroutine_threadsafe(self.close(), self._loop)
        future.result(timeout=timeout_seconds)

    def verify_from_restart_thread(self, *, timeout_seconds: float = 5.0) -> None:
        """Revalidate the PostgreSQL session at the last point before exec."""
        if self._closed:
            raise WriteBarrierConnectionLostError(
                "The cutover guard closed before process replacement."
            )
        if self._loop.is_closed() or not self._loop.is_running():
            raise WriteBarrierConnectionLostError(
                "The cutover event loop stopped before process replacement."
            )
        future = asyncio.run_coroutine_threadsafe(self.verify_held(), self._loop)
        future.result(timeout=timeout_seconds)



class PreparedDestructiveRestore(dict[str, Any]):
    """Restore response carrying the guards that remain held until restart."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        cutover_guard: BackupCutoverGuard,
    ) -> None:
        super().__init__(payload)
        self.cutover_guard = cutover_guard


class _StorageSwapRollbackFailed(DestructiveRestoreError):
    """The active file selection may be ambiguous and must stay fenced."""


async def _swap_prepared_storage_or_rollback(
    prepared_files: PreparedStorageRestore,
) -> None:
    """Finish a file swap or durably restore the previous active files.

    Cancellation can arrive after the blocking swap worker has completed but
    before its await returns.  Always finish a rollback before propagating any
    swap error; a rollback failure is distinguished so the caller can retain
    the process/database cutover fence for startup recovery.
    """
    try:
        await _to_thread_cleanup_quiescent(prepared_files.swap_into_place)
    except BaseException:
        try:
            await _thread_result_while_resolving_cancellation(
                prepared_files.rollback
            )
        except BaseException as rollback_error:
            raise _StorageSwapRollbackFailed(
                "restored files could not be activated or rolled back safely"
            ) from rollback_error
        raise


def _file_reference_inventory(
    references: tuple[LocalFileReference, ...],
) -> dict[str, Any]:
    return build_file_reference_inventory(references)


def _require_manifest_file_reference_inventory(
    inspection: VerifiedBackupInspection,
) -> dict[str, Any]:
    raw = inspection.manifest.metadata.get(_FILE_REFERENCE_INVENTORY_KEY)
    try:
        return normalize_file_reference_inventory(raw)
    except ValueError as exc:
        raise BackupIntegrityError(
            "backup manifest has no valid local-file reference inventory"
        ) from exc


def _require_copied_file_references(
    references: tuple[LocalFileReference, ...],
    prepared: Any,
    storage_config: Any,
) -> None:
    """Bind every logical DB file reference to one copied backup resource."""
    file_resource_paths = {
        resource.path
        for resource in prepared.resources
        if resource.path.startswith("resources/files/")
    }
    storage_root = (
        Path(storage_config.data_dir).expanduser().resolve(strict=False) / "storage"
    ).resolve(strict=False)
    for collection_id, record_id, filename in references:
        opened = open_file_stream(
            collection_id,
            record_id,
            filename,
            config=storage_config,
        )
        if opened is None or opened.backend != "local" or opened.storage_path is None:
            try:
                missing_path = get_storage_file_path(
                    collection_id,
                    record_id,
                    filename,
                )
            except Exception:
                missing_path = storage_root / collection_id / record_id / filename
            if opened is not None:
                opened.close()
            raise BackupIntegrityError(
                "database references a local file missing from active storage: "
                f"{missing_path}"
            )
        try:
            try:
                relative = opened.storage_path.relative_to(storage_root)
            except ValueError as exc:
                raise BackupIntegrityError(
                    "database file reference resolved outside active storage"
                ) from exc
            resource_path = PurePosixPath(
                "resources",
                "files",
                *relative.parts,
            ).as_posix()
            if resource_path not in file_resource_paths:
                raise BackupIntegrityError(
                    "database references a local file missing from copied resources"
                )
        finally:
            opened.close()


async def _to_thread_cleanup_quiescent(
    function: Callable[..., _T],
    /,
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Finish cleanup despite cancellation, but never hide its own failure."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation: asyncio.CancelledError | None = None
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as exc:
        cancellation = exc

    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            continue
        except BaseException:
            break

    result = worker.result()
    if cancellation is not None:
        raise cancellation
    return result


async def _cancel_task_quiescent(task: asyncio.Task[Any]) -> None:
    """Cancel a worker and do not return until all of its cleanup has finished."""
    if not task.done():
        task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if task.done():
        try:
            task.result()
        except BaseException:
            pass


async def _thread_result_while_resolving_cancellation(
    function: Callable[..., _T],
    /,
    *args: Any,
) -> _T:
    """Finish one gate decision off-loop despite repeated cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    while True:
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue


async def _finalize_backup_atomically(
    function: Callable[..., _T],
    /,
    *args: Any,
    seal_gate: BackupSealGate,
    **kwargs: Any,
) -> _T:
    """Choose exactly one outcome: cancellation or a completed SEALED commit."""
    worker = asyncio.create_task(
        asyncio.to_thread(
            function,
            *args,
            seal_gate=seal_gate,
            **kwargs,
        )
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        sealing_prevented = await _thread_result_while_resolving_cancellation(
            seal_gate.cancel
        )
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            result = worker.result()
        except BaseException as worker_error:
            if sealing_prevented and isinstance(
                worker_error,
                BackupSealCancelledError,
            ):
                raise cancellation
            raise
        if sealing_prevented:  # pragma: no cover - gate invariant
            raise cancellation
        return result


class BackupServiceError(RuntimeError):
    """Stable HTTP-facing error without credentials or subprocess output."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data or {}


async def _abort_partial_backup_quiescent(builder: Any) -> None:
    """Remove one partial set; cleanup failure takes precedence over cancellation."""
    try:
        await _to_thread_cleanup_quiescent(builder.abort)
    except asyncio.CancelledError:
        raise
    except BaseException as cleanup_error:
        raise BackupServiceError(
            500,
            "backup_partial_cleanup_failed",
            "Backup creation failed and its unsealed partial set "
            "could not be removed safely.",
        ) from cleanup_error


def _restore_engine_connect_args(settings: Any) -> dict[str, float]:
    """Return bounded asyncpg connection and command timeouts for cutover."""
    connect_timeout = float(settings.backup_restore_connect_timeout)
    command_timeout = float(settings.backup_restore_command_timeout)
    if (
        not math.isfinite(connect_timeout)
        or connect_timeout <= 0
        or not math.isfinite(command_timeout)
        or command_timeout <= 0
    ):
        raise BackupServiceError(
            500,
            "backup_restore_timeout_invalid",
            "The destructive restore PostgreSQL timeouts must be positive.",
        )
    return {
        "timeout": connect_timeout,
        "command_timeout": command_timeout,
    }


def _create_restore_cutover_engine(
    settings: Any,
    connect_args: dict[str, float],
) -> AsyncEngine:
    """Create one unpooled engine with the restore cutover time bounds."""
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args=dict(connect_args),
    )


class NativeBackupService:
    """Create native backups and restore them into the active targets."""

    def __init__(self, engine: AsyncEngine, settings: Any) -> None:
        self._closed = False
        self.engine = engine
        self.settings = settings
        self.backup_root = absolute_path_without_symlink_resolution(
            Path(settings.data_dir).expanduser() / "backups"
        )
        try:
            ensure_runtime_backup_roots(settings)
            self.data_root = RuntimeDataRoot.open(settings.data_dir)
        except ControlPlaneSafetyError as exc:
            raise BackupServiceError(
                500,
                "backup_roots_invalid",
                "The native backup directories are missing or unsafe.",
            ) from exc
        self._validate_roots()
        try:
            self.archive_store = BackupArchiveStore(settings.data_dir)
            self.operations = BackupOperationCoordinator(self.data_root)
            self.destructive_journal = DestructiveRestoreJournal(self.data_root)
            self.workspace_root = Path(
                tempfile.mkdtemp(prefix="ppbase-backup-workspace-")
            ).resolve(strict=True)
            os.chmod(self.workspace_root, 0o700, follow_symlinks=False)
            self.store = LocalBackupStore(self.workspace_root)
        except BackupServiceError:
            self.close()
            raise
        except BackupOperationError as exc:
            self.close()
            raise BackupServiceError(
                500,
                exc.code,
                "The native backup operation coordinator is missing or unsafe.",
            ) from exc
        except BackupError as exc:
            self.close()
            raise BackupServiceError(
                500,
                "backup_store_invalid",
                "The native backup store is missing or unsafe.",
            ) from exc
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        store = getattr(self, "store", None)
        archive_store = getattr(self, "archive_store", None)
        workspace_root = getattr(self, "workspace_root", None)
        operations = getattr(self, "operations", None)
        destructive_journal = getattr(self, "destructive_journal", None)
        data_root = getattr(self, "data_root", None)
        try:
            if store is not None:
                store.close()
        finally:
            try:
                if archive_store is not None:
                    archive_store.close()
            finally:
                try:
                    if workspace_root is not None and workspace_root.exists():
                        shutil.rmtree(workspace_root)
                finally:
                    try:
                        if operations is not None:
                            operations.close()
                    finally:
                        try:
                            if destructive_journal is not None:
                                destructive_journal.close()
                        finally:
                            if data_root is not None:
                                data_root.close()

    def __enter__(self) -> "NativeBackupService":
        self._require_runtime_attached()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    async def _resolve_backup_reference(self, reference: str) -> str:
        """Resolve the exact PocketBase-visible ZIP key."""
        try:
            key = validate_backup_key(reference)
        except ValueError:
            raise BackupServiceError(
                404,
                "backup_not_found",
                "The local backup was not found.",
            )
        if not await _to_thread_quiescent(self.archive_store.exists, key):
            raise BackupServiceError(
                404,
                "backup_not_found",
                "The local backup was not found.",
            )
        return key

    async def _assert_backup_aliases_available(
        self,
        *,
        backup_id: str | None,
        filename: str,
    ) -> None:
        """Refuse a duplicate PocketBase-visible ZIP key."""
        del backup_id
        if await _to_thread_quiescent(self.archive_store.exists, filename):
            raise BackupServiceError(
                409,
                "backup_reference_conflict",
                "A backup already uses this ZIP filename.",
                {"reference": filename},
            )

    @contextmanager
    def mutation_operation(self) -> Any:
        """Return the single cross-worker mutation context for API streaming."""
        self._require_runtime_attached()
        try:
            with self.operations.global_exclusive() as lease:
                yield lease
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc

    async def create_local_backup(
        self,
        *,
        actor_id: str | None = None,
        transport_filename: str | None = None,
    ) -> dict[str, Any]:
        if transport_filename == "":
            transport_filename = None
        if transport_filename is not None:
            try:
                transport_filename = validate_backup_transport_filename(
                    transport_filename
                )
            except ValueError as exc:
                raise BackupServiceError(
                    422,
                    "backup_filename_invalid",
                    "The requested backup filename must be a safe .zip basename.",
                ) from exc
        try:
            with self.operations.global_exclusive() as operation_lease:
                return await self._create_local_backup_under_lease(
                    actor_id=actor_id,
                    transport_filename=transport_filename,
                    operation_lease=operation_lease,
                )
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc

    async def _create_local_backup_under_lease(
        self,
        *,
        actor_id: str | None = None,
        transport_filename: str | None = None,
        operation_lease: BackupOperationLease,
    ) -> dict[str, Any]:
        self._require_runtime_attached()
        if transport_filename is not None:
            await self._assert_backup_aliases_available(
                backup_id=None,
                filename=transport_filename,
            )
            self._operation_commit_guard(operation_lease)

        local_secret_path: Path | None = None
        explicit_jwt_secret: str | None = None
        configured_jwt_secret = str(
            getattr(self.settings, "jwt_secret", "") or ""
        )
        if configured_jwt_secret:
            try:
                encoded_jwt_secret = configured_jwt_secret.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise BackupServiceError(
                    409,
                    "jwt_secret_unavailable",
                    "The configured JWT secret is not valid UTF-8 text.",
                ) from exc
            if (
                configured_jwt_secret != configured_jwt_secret.strip()
                or "\x00" in configured_jwt_secret
                or len(encoded_jwt_secret) + 1 > 4096
            ):
                raise BackupServiceError(
                    409,
                    "jwt_secret_unavailable",
                    "The configured JWT secret cannot be exported safely.",
                )
            explicit_jwt_secret = configured_jwt_secret
        else:
            # Ensure the server's already-selected project secret is persisted
            # before the exclusive backup phase starts.
            self.settings.get_jwt_secret()
            candidate = Path(self.settings.data_dir).expanduser() / ".jwt_secret"
            if not candidate.is_file() or candidate.is_symlink():
                raise BackupServiceError(
                    409,
                    "jwt_secret_unavailable",
                    "The project-local JWT secret is not safely readable.",
                )
            local_secret_path = candidate

        builder = None
        prepared = None
        contract: DatabaseContract | None = None
        source_summary: dict[str, int] | None = None
        source_file_references: tuple[LocalFileReference, ...] | None = None
        source_app_name = "PPBase"
        manifest_created_at: datetime | None = None
        candidate_filename: str | None = None
        jwt_secret_included = False
        try:
            async with backup_write_barrier(
                self.engine,
                timeout_seconds=float(self.settings.backup_barrier_timeout),
            ) as lease:
                with pin_storage_config(self.settings) as storage_config:
                    if storage_config.backend != "local":
                        raise BackupServiceError(
                            409,
                            "unsupported_storage_backend",
                            "Native backup supports the local storage "
                            "backend only.",
                        )
                    # Canonicalizing expected views/indexes uses temporary
                    # PostgreSQL objects inside savepoints. PostgreSQL forbids
                    # even TEMP DDL in a READ ONLY transaction, so perform this
                    # non-persistent reconciliation in its own transaction
                    # while the write barrier is already held, immediately
                    # before opening the read-only export snapshot.  The
                    # transaction is REPEATABLE READ (but still read-write, so
                    # the temp DDL is allowed) so that validation and the
                    # fingerprint taken right after it observe one identical
                    # snapshot — otherwise an external client could mutate an
                    # already-validated object before the fingerprint records it,
                    # letting the unvalidated state pass the export re-check.
                    async with lease.connection.begin():
                        await lease.connection.execute(
                            text(
                                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                            )
                        )
                        await set_backup_control_search_path(lease.connection)
                        await validate_canonical_collection_objects(
                            lease.connection
                        )
                        validated_fingerprint = (
                            await fingerprint_collection_objects(
                                lease.connection
                            )
                        )
                    async with lease.connection.begin():
                        await lease.connection.execute(
                            text(
                                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, "
                                "READ ONLY"
                            )
                        )
                        # Re-check the validated objects inside the export
                        # snapshot before the first COPY.  The write barrier does
                        # not fence external PostgreSQL clients, so a view, index,
                        # or _collections row could have drifted after validation;
                        # this REPEATABLE READ read pins the snapshot and fails
                        # closed on any mismatch.  The search path must match the
                        # validation transaction: ``pg_get_viewdef`` schema-
                        # qualifies relations only when they are outside the
                        # search path, so both fingerprints must resolve names the
                        # same way or a valid view would look like drift.
                        await set_backup_control_search_path(lease.connection)
                        snapshot_fingerprint = (
                            await fingerprint_collection_objects(
                                lease.connection
                            )
                        )
                        if snapshot_fingerprint != validated_fingerprint:
                            raise BackupServiceError(
                                409,
                                "schema_drift_during_backup",
                                "Collection metadata, views, or indexes changed "
                                "after validation; backup was refused.",
                            )
                        durable_settings = (
                            await lease.connection.execute(
                                text(
                                    'SELECT value FROM public."_params" '
                                    "WHERE key = 'settings'"
                                )
                            )
                        ).scalar_one_or_none()
                        if isinstance(durable_settings, dict):
                            durable_meta = durable_settings.get("meta")
                            if isinstance(durable_meta, dict):
                                candidate_app_name = str(
                                    durable_meta.get("appName", "") or ""
                                ).strip()
                                if candidate_app_name:
                                    source_app_name = candidate_app_name[:200]
                        durable_storage_config = (
                            resolve_storage_config_from_settings_payload(
                                self.settings,
                                durable_settings
                                if isinstance(durable_settings, dict)
                                else None,
                            )
                        )
                        if durable_storage_config != storage_config:
                            raise BackupServiceError(
                                409,
                                "storage_runtime_not_reconciled",
                                "The durable storage settings and live runtime "
                                "configuration differ; backup was refused.",
                            )
                        database_size = int(
                            (
                                await lease.connection.execute(
                                    text("SELECT pg_catalog.pg_database_size(pg_catalog.current_database())")
                                )
                            ).scalar_one()
                        )
                        storage_size = await _to_thread_quiescent(
                            local_tree_size,
                            Path(storage_config.data_dir).expanduser() / "storage",
                        )
                        await _to_thread_quiescent(
                            require_disk_space,
                            self.backup_root,
                            database_size + storage_size,
                            operation="native backup",
                        )
                        contract = await inspect_database_contract(lease.connection)
                        # Stream the live schema out with COPY on the raw asyncpg
                        # connection underlying this same REPEATABLE READ, READ ONLY
                        # transaction — no external binaries and no separate
                        # exported snapshot.
                        builder = self.store.begin_set()
                        builder.create_database_directory()
                        raw_connection = await lease.connection.get_raw_connection()
                        pg_conn = raw_connection.driver_connection
                        migrations_dir = Path(
                            self.settings.migrations_dir
                        ).expanduser()
                        migrations = await collect_applied_migrations(
                            pg_conn, migrations_dir=migrations_dir
                        )
                        source_summary = await self._source_summary(
                            lease.connection
                        )
                        source_file_references = (
                            await read_canonical_local_file_references(
                                lease.connection
                            )
                        )
                        await export_native_database(
                            pg_conn,
                            schema_path=builder.database_schema_path,
                            copy_path=builder.database_copy_path,
                            migrations=migrations,
                            source_postgres_version=contract.server_version_num,
                        )
                    await _to_thread_quiescent(
                        builder.copy_storage,
                        Path(storage_config.data_dir).expanduser() / "storage",
                    )
                    if local_secret_path is not None:
                        await _to_thread_quiescent(
                            builder.copy_jwt_secret,
                            local_secret_path,
                        )
                    elif explicit_jwt_secret is not None:
                        await _to_thread_quiescent(
                            builder.write_jwt_secret,
                            explicit_jwt_secret,
                        )
                    else:  # pragma: no cover - guarded before the barrier
                        raise BackupServiceError(
                            409,
                            "jwt_secret_unavailable",
                            "The active JWT secret is unavailable for backup.",
                        )
                    jwt_secret_included = True
                    prepared = await _to_thread_quiescent(builder.prepare)
                    await _to_thread_quiescent(
                        _require_copied_file_references,
                        source_file_references,
                        prepared,
                        storage_config,
                    )
                    manifest_created_at = datetime.now(UTC)
                    candidate_filename = transport_filename or (
                        backup_transport_filename_from_parts(
                            prepared.backup_id,
                            format_manifest_timestamp(manifest_created_at),
                            source_app_name,
                        )
                    )
                    await self._assert_backup_aliases_available(
                        backup_id=prepared.backup_id,
                        filename=candidate_filename,
                    )
                    self._operation_commit_guard(operation_lease)
        except BaseException as exc:
            cleanup_cancellation: asyncio.CancelledError | None = None
            if builder is not None:
                try:
                    await _abort_partial_backup_quiescent(builder)
                except asyncio.CancelledError as cancellation:
                    cleanup_cancellation = cancellation
            if isinstance(exc, asyncio.CancelledError):
                raise
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            if isinstance(exc, BackupServiceError):
                raise
            if isinstance(exc, BackupDiskSpaceError):
                raise BackupServiceError(
                    507,
                    "backup_insufficient_disk_space",
                    str(exc),
                ) from exc
            if isinstance(exc, (BackupError, PostgresBackupError)):
                raise self._map_error(exc, operation="create") from exc
            raise BackupServiceError(
                500,
                "backup_creation_failed",
                "The local backup could not be prepared and was not sealed.",
            ) from exc

        if (
            contract is None
            or source_summary is None
            or source_file_references is None
            or prepared is None
            or manifest_created_at is None
        ):  # pragma: no cover
            raise BackupServiceError(
                500,
                "backup_creation_failed",
                "Backup preparation did not produce its metadata.",
            )
        metadata = {
            "ppbase_version": __version__,
            "app_name": source_app_name,
            "storage_backend": "local",
            "database_contract": contract.to_dict(),
            "database_summary": source_summary,
            _FILE_REFERENCE_INVENTORY_KEY: _file_reference_inventory(
                source_file_references
            ),
            "jwt_secret": {
                "mode": (
                    "included_resource"
                    if jwt_secret_included
                    else "external_required"
                )
            },
            "created_by": actor_id,
        }
        if transport_filename is not None:
            metadata["transport"] = {"filename": transport_filename}
        try:
            inspection = await _finalize_backup_atomically(
                self.store.finalize_set,
                prepared,
                seal_gate=BackupSealGate(),
                metadata=metadata,
                created_at=manifest_created_at,
                pre_commit_guard=lambda: self._operation_commit_guard(
                    operation_lease
                ),
            )
        except BackupError as exc:
            raise self._map_error(exc, operation="seal") from exc
        if candidate_filename is None:  # pragma: no cover - guarded above
            raise BackupServiceError(
                500,
                "backup_creation_failed",
                "Backup creation did not produce a ZIP filename.",
            )
        pinned: PinnedBackupZip | None = None
        try:
            pinned = await _to_thread_quiescent(
                materialize_backup_zip,
                self.store,
                inspection.manifest.backup_id,
                chunk_size=int(self.settings.backup_transport_chunk_size),
                cancel_cleanup=lambda archive: archive.close(),
            )
            self._operation_commit_guard(operation_lease)
            archive = await _to_thread_quiescent(
                self.archive_store.publish_pinned,
                pinned,
                candidate_filename,
            )
            pinned = None
            self._operation_commit_guard(operation_lease)
            return archive.to_dict()
        except BackupTransportError as exc:
            raise self._map_transport_error(exc) from exc
        except BackupError as exc:
            raise self._map_error(exc, operation="create") from exc
        finally:
            if pinned is not None:
                pinned.close()

    async def list_local_backups(self) -> list[dict[str, Any]]:
        self._require_runtime_attached()
        try:
            archives = await _to_thread_quiescent(self.archive_store.list)
            self._require_runtime_attached()
            return [archive.to_dict() for archive in archives]
        except BackupError as exc:
            raise self._map_error(exc, operation="list") from exc

    async def materialize_local_backup_zip(
        self,
        backup_id: str,
    ) -> PinnedBackupZip:
        self._require_runtime_attached()
        backup_id = await self._resolve_backup_reference(backup_id)
        leases = ExitStack()
        try:
            backup_lease = leases.enter_context(
                self.operations.backup_shared(backup_id)
            )
            pinned = await _to_thread_quiescent(
                self.archive_store.pin,
                backup_id,
                cancel_cleanup=lambda archive: archive.close(),
            )
            try:
                self._operation_commit_guard(backup_lease)
                retained_leases = leases.pop_all()
                try:
                    pinned.add_close_callback(retained_leases.close)
                except BaseException:
                    retained_leases.close()
                    raise
            except BaseException:
                pinned.close()
                raise
            return pinned
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc
        except BackupTransportError as exc:
            raise self._map_transport_error(exc) from exc
        except BackupError as exc:
            raise self._map_error(exc, operation="download") from exc
        finally:
            leases.close()

    async def upload_local_backup(
        self,
        source: BinaryIO,
        *,
        filename: str,
        operation_lease: BackupOperationLease | None = None,
    ) -> dict[str, Any]:
        if operation_lease is None:
            try:
                with self.operations.global_exclusive() as owned_lease:
                    return await self._upload_local_backup_under_lease(
                        source,
                        filename=filename,
                        operation_lease=owned_lease,
                    )
            except BackupOperationError as exc:
                raise self._map_operation_error(exc) from exc
        return await self._upload_local_backup_under_lease(
            source,
            filename=filename,
            operation_lease=operation_lease,
        )

    async def _upload_local_backup_under_lease(
        self,
        source: BinaryIO,
        *,
        filename: str,
        operation_lease: BackupOperationLease,
    ) -> dict[str, Any]:
        if operation_lease.scope != "global" or operation_lease.mode != "exclusive":
            raise BackupServiceError(
                500,
                "backup_operation_control_invalid",
                "Upload requires the global native backup mutation lease.",
            )
        operation_lease.verify_attached()
        self._require_runtime_attached()
        try:
            selected = validate_backup_transport_filename(filename)
            await self._assert_backup_aliases_available(
                backup_id=None,
                filename=selected,
            )
            archive = await _to_thread_quiescent(
                self.archive_store.publish,
                source,
                selected,
                require_zip_suffix=True,
                sniff_zip=True,
                max_size=int(self.settings.backup_max_upload_bytes),
            )
            self._operation_commit_guard(operation_lease)
            return archive.to_dict()
        except ValueError as exc:
            raise BackupServiceError(
                422,
                "backup_filename_invalid",
                "The uploaded backup filename must be a safe .zip basename.",
            ) from exc
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc
        except BackupTransportError as exc:
            raise self._map_transport_error(exc) from exc
        except BackupError as exc:
            raise self._map_error(exc, operation="upload") from exc

    async def delete_local_backup(self, backup_id: str) -> None:
        self._require_runtime_attached()
        try:
            with self.operations.global_exclusive() as operation_lease:
                backup_id = await self._resolve_backup_reference(backup_id)
                with self.operations.backup_exclusive(backup_id) as backup_lease:
                    self._operation_commit_guard(operation_lease, backup_lease)
                    await _to_thread_quiescent(
                        self.archive_store.delete,
                        backup_id,
                    )
                    self._operation_commit_guard(operation_lease, backup_lease)
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc
        except BackupError as exc:
            raise self._map_error(exc, operation="delete") from exc

    async def _extract_archive_for_restore(
        self,
        backup_key: str,
        *,
        operation_lease: BackupOperationLease,
        backup_lease: BackupOperationLease,
    ) -> VerifiedBackupInspection:
        """Validate one stored ZIP into the private per-service workspace."""
        try:
            limits = BackupTransportLimits.from_settings(self.settings)
        except (AttributeError, TypeError, ValueError) as exc:
            raise BackupServiceError(
                500,
                "backup_transport_limits_invalid",
                "The native backup ZIP limits are invalid.",
            ) from exc

        pinned: PinnedBackupZip | None = None
        prepared_import: PreparedBackupImport | None = None
        finalized = False
        try:
            pinned = await _to_thread_quiescent(
                self.archive_store.pin,
                backup_key,
                cancel_cleanup=lambda archive: archive.close(),
            )
            prepared_import = await _to_thread_quiescent(
                prepare_backup_zip_import,
                self.store,
                pinned._handle,
                limits=limits,
                cancel_cleanup=lambda prepared: prepared.abort(),
            )
            self._operation_commit_guard(operation_lease, backup_lease)
            manifest = BackupManifest.from_bytes(prepared_import.manifest_bytes)
            await _finalize_backup_atomically(
                self.store.finalize_imported_set,
                prepared_import.prepared,
                manifest_bytes=prepared_import.manifest_bytes,
                seal_gate=BackupSealGate(),
                pre_commit_guard=lambda: self._operation_commit_guard(
                    operation_lease,
                    backup_lease,
                ),
            )
            finalized = True
            verified = await self._verified_inspection(manifest.backup_id)
            self._operation_commit_guard(operation_lease, backup_lease)
            return verified
        except BackupTransportError as exc:
            raise self._map_transport_error(exc) from exc
        finally:
            if pinned is not None:
                pinned.close()
            if prepared_import is not None and not finalized:
                try:
                    await _to_thread_cleanup_quiescent(prepared_import.abort)
                except asyncio.CancelledError:
                    raise
                except BaseException as cleanup_error:
                    raise BackupServiceError(
                        500,
                        "backup_restore_workspace_cleanup_failed",
                        "The rejected backup workspace could not be removed safely.",
                    ) from cleanup_error


    async def restore_local_backup(
        self,
        backup_id: str,
        *,
        actor_id: str | None,
        operation_lease: BackupOperationLease | None = None,
    ) -> dict[str, Any]:
        """Destructively restore the selected backup into the active targets."""
        if operation_lease is None:
            try:
                with self.operations.global_exclusive() as owned_lease:
                    return await self._restore_local_backup_under_lease(
                        backup_id,
                        actor_id=actor_id,
                        operation_lease=owned_lease,
                    )
            except BackupOperationError as exc:
                raise self._map_operation_error(exc) from exc
        return await self._restore_local_backup_under_lease(
            backup_id,
            actor_id=actor_id,
            operation_lease=operation_lease,
        )

    async def resolve_restore_reference(
        self,
        backup_id: str,
        *,
        operation_lease: BackupOperationLease,
    ) -> str:
        """Resolve an SDK restore key before its optimistic 204 response.

        The caller retains the global exclusive lease through the background
        restore, so a successfully resolved backup cannot be deleted between
        this precheck and the task acquiring its backup-scoped shared lease.
        """
        if operation_lease.scope != "global" or operation_lease.mode != "exclusive":
            raise BackupServiceError(
                500,
                "backup_operation_control_invalid",
                "SDK restore preflight requires the global mutation lease.",
            )
        try:
            operation_lease.verify_attached()
            resolved = await self._resolve_backup_reference(backup_id)
            self._operation_commit_guard(operation_lease)
            return resolved
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc

    async def _restore_local_backup_under_lease(
        self,
        backup_id: str,
        *,
        actor_id: str | None,
        operation_lease: BackupOperationLease,
    ) -> dict[str, Any]:
        if operation_lease.scope != "global" or operation_lease.mode != "exclusive":
            raise BackupServiceError(
                500,
                "backup_operation_control_invalid",
                "SDK restore requires the global native backup mutation lease.",
            )
        restore_id = secrets.token_hex(16)
        prepared_files: PreparedStorageRestore | None = None
        cutover_guard: BackupCutoverGuard | None = None
        database_committed = False
        cutover_must_remain = False
        try:
            restore_connect_args = _restore_engine_connect_args(self.settings)
            operation_lease.verify_attached()
            backup_key = await self._resolve_backup_reference(backup_id)
            with self.operations.backup_shared(backup_key) as backup_lease:
                inspection = await self._extract_archive_for_restore(
                    backup_key,
                    operation_lease=operation_lease,
                    backup_lease=backup_lease,
                )
                workspace_backup_id = inspection.manifest.backup_id
                contract = self._manifest_contract(inspection)
                expected_reference_inventory = (
                    _require_manifest_file_reference_inventory(inspection)
                )
                # Validate the native contract (format_version already checked by
                # the manifest; here contract_version, system_schema_version,
                # archive completeness and every recorded application-migration
                # SHA-256) BEFORE any destructive action. A mismatch is refused
                # while the live database and storage are still untouched. The
                # descriptor-pinned copy is re-parsed under the write barrier
                # immediately before the in-process rebuild.
                await _to_thread_quiescent(
                    self._validate_native_contract_preflight,
                    inspection.path / SCHEMA_JSON_RESOURCE,
                    Path(self.settings.migrations_dir).expanduser(),
                )

                async with self.engine.connect() as connection:
                    runtime_identity = await self._postgres_server_identity(connection)
                    report = await preflight_destructive_restore_role(connection)
                    report.require_ok()
                    target_server_major = int(runtime_identity["server_version_num"]) // 10000
                    if target_server_major < contract.server_major:
                        raise PostgresBackupError(
                            "the active PostgreSQL server is older than the backup source"
                        )
                    await connection.rollback()

                restore_workspace_bytes = sum(
                    int(resource.size)
                    for resource in inspection.manifest.resources
                )
                await _to_thread_quiescent(
                    require_disk_space,
                    Path(self.settings.data_dir).expanduser(),
                    restore_workspace_bytes,
                    operation="destructive native restore",
                )
                if self.destructive_journal.read() is not None:
                    raise BackupServiceError(
                        409,
                        "backup_restore_recovery_required",
                        "An interrupted destructive restore must be recovered by restarting PPBase.",
                    )
                prepared_files = await _to_thread_quiescent(
                    prepare_storage_restore,
                    store=self.store,
                    inspection=inspection,
                    settings=self.settings,
                    journal=self.destructive_journal,
                    restore_id=restore_id,
                    file_reference_inventory=expected_reference_inventory,
                )

                if not get_restart_command():
                    raise BackupServiceError(
                        409,
                        "backup_restore_restart_unavailable",
                        "PPBase was not started with a reusable serve command.",
                    )
                if is_restart_scheduled():
                    raise BackupServiceError(
                        409,
                        "backup_restore_restart_pending",
                        "Another PPBase process restart is already pending.",
                    )

                retained_barrier = await acquire_retained_backup_write_barrier(
                    self.engine,
                    timeout_seconds=float(self.settings.backup_barrier_timeout),
                )
                restart_reservation = reserve_process_restart()
                if restart_reservation is None:
                    await retained_barrier.close()
                    raise BackupServiceError(
                        409,
                        "backup_restore_restart_pending",
                        "Another PPBase process restart is already pending.",
                    )
                cutover_guard = BackupCutoverGuard(
                    retained_barrier,
                    restart_reservation=restart_reservation,
                )
                await cutover_guard.verify_held()
                self._operation_commit_guard(operation_lease, backup_lease)

                # Reverify and pin the extracted workspace only after all writers
                # are excluded. No resource path is reopened during rebuild.
                inspection = await self._verified_inspection(workspace_backup_id)
                refreshed_reference_inventory = (
                    _require_manifest_file_reference_inventory(inspection)
                )
                if refreshed_reference_inventory != expected_reference_inventory:
                    raise BackupIntegrityError(
                        "local-file reference inventory changed during restore"
                    )
                pinned_schema = None
                pinned_copy = None
                pinned_schema = await _to_thread_quiescent(
                    self.store.pin_database_resource,
                    inspection,
                    resource_path=SCHEMA_JSON_RESOURCE,
                    directory=prepared_files.target,
                    cancel_cleanup=lambda archive: archive.close(),
                )
                pinned_copy = await _to_thread_quiescent(
                    self.store.pin_database_resource,
                    inspection,
                    resource_path=DATA_COPY_RESOURCE,
                    directory=prepared_files.target,
                    cancel_cleanup=lambda archive: archive.close(),
                )
                restore_error: BaseException | None = None
                try:
                    # Re-parse the descriptor-pinned schema and re-hash local
                    # migrations under the retained write barrier. This closes
                    # the interval between the early path-based preflight and
                    # the first destructive storage operation.
                    schema_bytes = os.pread(
                        pinned_schema.fileno(), pinned_schema.size, 0
                    )
                    await _to_thread_quiescent(
                        self._validate_native_contract_bytes,
                        schema_bytes,
                        Path(self.settings.migrations_dir).expanduser(),
                    )
                    # Fail closed before entering the blocking swap. If
                    # cancellation lands after the worker has changed the active
                    # files, the helper completes a rollback before cancellation
                    # can escape.
                    cutover_must_remain = True
                    try:
                        await _swap_prepared_storage_or_rollback(prepared_files)
                    except _StorageSwapRollbackFailed:
                        raise
                    except BaseException:
                        cutover_must_remain = False
                        raise
                    try:
                        # Rebuild the database in-process from the pinned, verified
                        # schema.json + data.copy inodes — no external binaries are
                        # involved.
                        restore_engine = _create_restore_cutover_engine(
                            self.settings,
                            restore_connect_args,
                        )
                        try:
                            async with restore_engine.connect() as connection:
                                raw_connection = (
                                    await connection.get_raw_connection()
                                )
                                pg_conn = raw_connection.driver_connection
                                # Dup the pinned fd so the SegmentReader's file
                                # object can close without releasing the pin.
                                copy_file = os.fdopen(
                                    os.dup(pinned_copy.fileno()), "rb"
                                )
                                try:
                                    await restore_native_database(
                                        pg_conn,
                                        schema_bytes=schema_bytes,
                                        copy_file=copy_file,
                                        copy_size=pinned_copy.size,
                                        restore_id=restore_id,
                                    )
                                finally:
                                    copy_file.close()
                        finally:
                            await restore_engine.dispose()
                    except BaseException as exc:
                        restore_error = exc
                finally:
                    if pinned_schema is not None:
                        pinned_schema.close()
                    if pinned_copy is not None:
                        pinned_copy.close()

                marker_engine = _create_restore_cutover_engine(
                    self.settings,
                    restore_connect_args,
                )
                try:
                    async with marker_engine.connect() as connection:
                        marker = await read_database_restore_marker(connection)
                        database_committed = marker == restore_id
                        if database_committed:
                            await validate_committed_destructive_restore(
                                self.settings,
                                connection,
                                expected_reference_inventory,
                            )
                        await connection.rollback()
                finally:
                    await marker_engine.dispose()

                if not database_committed:
                    await _to_thread_quiescent(prepared_files.rollback)
                    cutover_must_remain = False
                    await cutover_guard.close()
                    cutover_guard = None
                    if restore_error is not None:
                        raise restore_error
                    raise PostgresBackupError(
                        "destructive restore ended without its atomic commit marker"
                    )

                prepared_files.mark_database_committed()
                prepared_files.target.close()
                return PreparedDestructiveRestore(
                    {
                        "backupId": backup_key,
                        "restoreId": restore_id,
                        "status": "restart_scheduled",
                        "destructive": True,
                        "actorId": actor_id,
                    },
                    cutover_guard=cutover_guard,
                )
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc
        except BackupDiskSpaceError as exc:
            raise BackupServiceError(
                507,
                "backup_restore_insufficient_disk_space",
                str(exc),
            ) from exc
        except WriteBarrierTimeoutError as exc:
            raise BackupServiceError(
                409,
                "backup_restore_barrier_timeout",
                "Timed out waiting for active writes and migrations to finish.",
            ) from exc
        except (WriteBarrierConnectionLostError, WriteBarrierError) as exc:
            raise BackupServiceError(
                503,
                "backup_restore_barrier_lost",
                "The destructive restore write barrier was lost.",
            ) from exc
        except BackupServiceError:
            raise
        except (BackupError, PostgresBackupError, DestructiveRestoreError) as exc:
            raise self._map_error(exc, operation="restore") from exc
        finally:
            if prepared_files is not None and not cutover_must_remain:
                try:
                    prepared_files.cleanup_work_dir()
                except Exception:
                    pass
            # Once PostgreSQL committed, an unexpected verification failure is
            # fail-closed: the self-retained guard and startup journal stay in
            # place for explicit process restart recovery.
            if cutover_guard is not None and not cutover_must_remain:
                try:
                    await cutover_guard.close()
                except Exception:
                    pass

    def _require_runtime_root_attached(self) -> None:
        if self._closed:
            raise BackupServiceError(
                500,
                "backup_service_closed",
                "The native backup service is closed.",
            )
        try:
            self.data_root.verify_attached()
        except ControlPlaneSafetyError as exc:
            raise BackupServiceError(
                500,
                "backup_runtime_detached",
                "The PPBase data directory is detached or unsafe.",
            ) from exc

    def _require_runtime_attached(self) -> None:
        self._require_runtime_root_attached()

    def _operation_commit_guard(
        self,
        *leases: BackupOperationLease,
    ) -> None:
        for lease in leases:
            lease.verify_attached()
        self._require_runtime_attached()
        for lease in leases:
            lease.verify_attached()

    def _validate_roots(self) -> None:
        active_data_dir = Path(self.settings.data_dir).expanduser().resolve(
            strict=False
        )
        expected_backup_root = active_data_dir / "backups"
        backup_root = self.backup_root.resolve(strict=False)
        if backup_root != expected_backup_root:
            raise BackupServiceError(
                409,
                "unsafe_backup_root",
                "The local backup root must be pb_data/backups.",
            )
    @staticmethod
    async def _postgres_server_identity(connection: Any) -> dict[str, Any]:
        await set_backup_control_search_path(connection)
        row = (
            await connection.execute(
                text(
                    "SELECT current_user AS role, "
                    "pg_catalog.current_database() AS database, "
                    "COALESCE(pg_catalog.inet_server_addr()::text, '') "
                    "AS server_address, "
                    "COALESCE(pg_catalog.inet_server_port(), 0) AS server_port, "
                    "EXTRACT(EPOCH FROM "
                    "pg_catalog.pg_postmaster_start_time())::text "
                    "AS postmaster_started_at, "
                    "pg_catalog.current_setting('server_version_num') "
                    "AS server_version_num, "
                    "pg_catalog.pg_backend_pid() AS backend_pid, "
                    "d.oid::bigint AS database_oid, "
                    "pg_catalog.shobj_description(d.oid, 'pg_database') "
                    "AS database_marker "
                    "FROM pg_catalog.pg_database AS d "
                    "WHERE d.datname = pg_catalog.current_database()"
                )
            )
        ).mappings().one()
        database_marker = row["database_marker"]
        return {
            "role": str(row["role"]),
            "database": str(row["database"]),
            "server_address": str(row["server_address"]),
            "server_port": int(row["server_port"]),
            "postmaster_started_at": str(row["postmaster_started_at"]),
            "server_version_num": str(row["server_version_num"]),
            "backend_pid": int(row["backend_pid"]),
            "database_oid": int(row["database_oid"]),
            "database_marker": (
                "" if database_marker is None else str(database_marker)
            ),
        }

    async def _source_summary(self, connection: Any) -> dict[str, int]:
        row = (
            await connection.execute(
                text(
                    'SELECT (SELECT count(*) FROM public."_collections")::integer '
                    "AS collections, "
                    '(SELECT count(*) FROM public."_superusers")::integer '
                    "AS superusers, "
                    '(SELECT count(*) FROM public."_migrations")::integer '
                    "AS migrations"
                )
            )
        ).mappings().one()
        return {
            "collections": int(row["collections"]),
            "superusers": int(row["superusers"]),
            "migrations": int(row["migrations"]),
        }

    async def _verified_inspection(
        self,
        backup_id: str,
    ) -> VerifiedBackupInspection:
        self._require_runtime_attached()
        try:
            inspection = await _to_thread_quiescent(
                self.store.verify_set,
                backup_id,
            )
            self._require_runtime_attached()
            return inspection
        except BackupError as exc:
            raise self._map_error(exc, operation="inspect") from exc

    @staticmethod
    def _manifest_contract(
        inspection: VerifiedBackupInspection,
    ) -> DatabaseContract:
        raw = inspection.manifest.metadata.get("database_contract")
        if not isinstance(raw, dict):
            raise BackupIntegrityError("manifest database contract is missing")
        return DatabaseContract.from_dict(raw)

    @staticmethod
    def _validate_native_contract_preflight(
        schema_path: Path, migrations_dir: Path
    ) -> None:
        """Validate a native (v2) ``schema.json`` before any destructive action.

        Parses the archive contract with the strict, fail-closed loader so a
        mismatch is refused while the live database and storage are still
        untouched.  :meth:`DatabaseSchema.from_archive_bytes` enforces the exact
        ``contract_version`` and ``system_schema_version`` (the manifest already
        pinned ``format_version``) and the completeness of the COPY segment
        cover.  It then recomputes every archived application-migration SHA-256
        against ``migrations_dir`` and refuses a missing, null, symlinked,
        path-escaping, or mismatched migration — so a divergent migration set is
        rejected before any destruction (extra unapplied local files are
        allowed).  Runs off the event loop via ``_to_thread_quiescent`` because
        it does blocking file IO.
        """
        schema_bytes = schema_path.read_bytes()
        NativeBackupService._validate_native_contract_bytes(
            schema_bytes,
            migrations_dir,
        )

    @staticmethod
    def _validate_native_contract_bytes(
        schema_bytes: bytes, migrations_dir: Path
    ) -> None:
        """Validate pinned native-contract bytes and their local migrations."""
        schema = DatabaseSchema.from_archive_bytes(schema_bytes)
        verify_migration_hashes_on_disk(
            schema.migrations, migrations_dir=migrations_dir
        )

    @staticmethod
    def _map_operation_error(exc: BackupOperationError) -> BackupServiceError:
        status_code = 500 if isinstance(exc, BackupOperationSafetyError) else 409
        return BackupServiceError(
            status_code,
            exc.code,
            str(exc),
        )

    @staticmethod
    def _map_transport_error(exc: BackupTransportError) -> BackupServiceError:
        if exc.code == "pocketbase_backup_unsupported":
            status_code = 422
        elif exc.code in {
            "backup_upload_too_large",
            "backup_zip_too_many_entries",
            "backup_zip_central_directory_too_large",
            "backup_zip_resource_too_large",
            "backup_zip_uncompressed_too_large",
            "backup_zip_ratio_exceeded",
            "backup_zip_metadata_too_large",
        }:
            status_code = 413
        else:
            status_code = 400
        return BackupServiceError(
            status_code,
            exc.code,
            str(exc),
        )

    def _map_error(self, exc: Exception, *, operation: str) -> BackupServiceError:
        if isinstance(exc, BackupServiceError):
            return exc
        if isinstance(exc, BackupNotFoundError):
            return BackupServiceError(
                404,
                "backup_not_found",
                "The sealed local backup was not found.",
            )
        if isinstance(exc, BackupIntegrityError):
            return BackupServiceError(
                409,
                "backup_integrity_failed",
                f"The backup failed integrity verification: {exc}",
            )
        if isinstance(exc, PostgresBackupError):
            return BackupServiceError(
                409,
                f"postgres_{operation}_contract_failed",
                f"The PostgreSQL {operation} contract was not satisfied.",
            )
        if isinstance(exc, BackupDeletionUncertainError):
            return BackupServiceError(
                500,
                "backup_delete_outcome_uncertain",
                "The backup deletion outcome is uncertain and requires manual "
                "recovery.",
            )
        if isinstance(exc, BackupError):
            return BackupServiceError(
                409,
                f"backup_{operation}_failed",
                f"The local backup {operation} operation failed safely.",
            )
        return BackupServiceError(
            500,
            f"backup_{operation}_failed",
            f"The backup {operation} operation failed.",
        )
