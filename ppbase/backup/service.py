"""Application orchestration for signed backup and destructive restore."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import os
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
    ControlPlaneRoot,
    ControlPlaneSafetyError,
    absolute_path_without_symlink_resolution,
    ensure_runtime_backup_roots,
)
from ppbase.backup.canonical import (
    fingerprint_collection_objects,
    validate_canonical_collection_objects,
)
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
from ppbase.backup.identity import (
    BackupIdentity,
    BackupIdentityError,
    BackupIdentityMissingError,
)
from ppbase.backup.models import (
    BackupError,
    BackupInspection,
    BackupIntegrityError,
    BackupManifest,
    BackupNotFoundError,
    BackupSetSummary,
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
    AuthenticatedBackupInspection,
    BackupDeleteCancelledError,
    BackupDeletionUncertainError,
    BackupDeleteGate,
    BackupSealCancelledError,
    BackupSealGate,
    LocalBackupStore,
)
from ppbase.backup.transport import (
    BackupTransportError,
    BackupTransportLimits,
    PinnedBackupZip,
    PreparedBackupImport,
    backup_transport_filename,
    backup_transport_filename_from_parts,
    backup_transport_size,
    materialize_backup_zip,
    prepare_backup_zip_import,
    validate_backup_transport_filename,
)
from ppbase.backup.trust import BackupTrustError, BackupTrustStore
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
BACKUP_INSPECTION_DEFAULT_RESOURCE_LIMIT = 100
BACKUP_INSPECTION_MAX_RESOURCE_LIMIT = 250
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
    inspection: AuthenticatedBackupInspection,
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


async def _delete_backup_atomically(
    function: Callable[..., None],
    /,
    *args: Any,
    delete_gate: BackupDeleteGate,
    **kwargs: Any,
) -> None:
    """Choose cancellation or a completed durable delete, never both."""
    worker = asyncio.create_task(
        asyncio.to_thread(
            function,
            *args,
            delete_gate=delete_gate,
            **kwargs,
        )
    )
    try:
        await asyncio.shield(worker)
        return
    except asyncio.CancelledError as cancellation:
        deletion_prevented = await _thread_result_while_resolving_cancellation(
            delete_gate.cancel
        )
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            worker.result()
        except BaseException as worker_error:
            if deletion_prevented and isinstance(
                worker_error,
                BackupDeleteCancelledError,
            ):
                raise cancellation
            raise
        if deletion_prevented:  # pragma: no cover - gate invariant
            raise cancellation
        return


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
    """Create signed local sets and restore them into the active targets."""

    def __init__(self, engine: AsyncEngine, settings: Any) -> None:
        self._closed = False
        self.engine = engine
        self.settings = settings
        self.backup_root = absolute_path_without_symlink_resolution(
            settings.backup_root
        )
        self.control_dir = absolute_path_without_symlink_resolution(
            settings.backup_control_dir
        )
        try:
            ensure_runtime_backup_roots(settings)
        except ControlPlaneSafetyError as exc:
            raise BackupServiceError(
                500,
                "backup_roots_invalid",
                "The native backup directories are missing or unsafe.",
            ) from exc
        self._validate_roots()
        try:
            self.control_root = ControlPlaneRoot.open(
                self.control_dir,
            )
        except ControlPlaneSafetyError as exc:
            raise BackupServiceError(
                500,
                "backup_control_invalid",
                "The native backup control plane is missing or unsafe.",
            ) from exc
        try:
            self.operations = BackupOperationCoordinator(self.control_root)
            self.destructive_journal = DestructiveRestoreJournal(self.control_root)
            self.trust = BackupTrustStore(self.control_root)
            self.identity = self._load_identity()
            self.store = LocalBackupStore(
                self.backup_root,
                identity=self.identity,
            )
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
        except BackupTrustError as exc:
            self.close()
            raise BackupServiceError(
                500,
                "backup_control_invalid",
                "The native backup signer trust store is missing or unsafe.",
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
        identity = getattr(self, "identity", None)
        trust = getattr(self, "trust", None)
        operations = getattr(self, "operations", None)
        destructive_journal = getattr(self, "destructive_journal", None)
        control_root = getattr(self, "control_root", None)
        try:
            if store is not None:
                store.close()
        finally:
            try:
                if identity is not None:
                    identity.close()
            finally:
                try:
                    if trust is not None:
                        trust.close()
                finally:
                    try:
                        if operations is not None:
                            operations.close()
                    finally:
                        try:
                            if destructive_journal is not None:
                                destructive_journal.close()
                        finally:
                            if control_root is not None:
                                control_root.close()

    def __enter__(self) -> "NativeBackupService":
        self._require_control_identity_attached()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def get_identity(self) -> dict[str, Any]:
        self._require_control_identity_attached()
        encoded_key = base64.urlsafe_b64encode(
            self.identity.public_key_bytes
        ).decode("ascii").rstrip("=")
        result = {
            "algorithm": "Ed25519",
            "publicKey": encoded_key,
            "fingerprintSha256": self.identity.fingerprint_sha256,
        }
        self._require_control_identity_attached()
        return result

    def list_trusted_signers(self) -> list[dict[str, Any]]:
        """List explicitly approved external Ed25519 identities."""
        self._require_control_identity_attached()
        try:
            result = [record.to_dict() for record in self.trust.list()]
            self._require_control_identity_attached()
            return result
        except BackupTrustError as exc:
            raise BackupServiceError(
                500,
                "backup_trust_store_invalid",
                "The backup signer trust store is missing or unsafe.",
            ) from exc

    async def _backup_reference_summaries(self) -> list[BackupSetSummary]:
        """Return one attached snapshot used for ID/SDK-key resolution."""
        self._require_control_identity_attached()
        try:
            summaries = await _to_thread_quiescent(self.store.list_sets)
        except BackupError as exc:
            raise self._map_error(exc, operation="resolve") from exc
        self._require_control_identity_attached()
        return summaries

    @staticmethod
    def _backup_summary_aliases(summary: BackupSetSummary) -> set[str]:
        aliases = {str(summary.backup_id)}
        if summary.manifest is not None:
            aliases.add(backup_transport_filename(summary.manifest))
        return aliases

    async def _resolve_backup_reference(self, reference: str) -> str:
        """Resolve an exact internal ID or PocketBase-visible ZIP key."""
        if not isinstance(reference, str) or not reference:
            raise BackupServiceError(
                404,
                "backup_not_found",
                "The sealed local backup was not found.",
            )
        summaries = await self._backup_reference_summaries()
        matches = {
            str(summary.backup_id)
            for summary in summaries
            if reference in self._backup_summary_aliases(summary)
        }
        if not matches:
            raise BackupServiceError(
                404,
                "backup_not_found",
                "The sealed local backup was not found.",
            )
        if len(matches) != 1:
            raise BackupServiceError(
                409,
                "backup_reference_ambiguous",
                "The backup reference matches more than one sealed backup.",
            )
        return matches.pop()

    async def _assert_backup_aliases_available(
        self,
        *,
        backup_id: str | None,
        filename: str,
    ) -> None:
        """Refuse additions that would make an ID-or-key route ambiguous."""
        candidate_aliases = {filename}
        if backup_id is not None:
            candidate_aliases.add(backup_id)
        for summary in await self._backup_reference_summaries():
            collisions = candidate_aliases & self._backup_summary_aliases(summary)
            if collisions:
                raise BackupServiceError(
                    409,
                    "backup_reference_conflict",
                    "A sealed backup already uses this backup ID or ZIP filename.",
                    {"reference": sorted(collisions)[0]},
                )

    async def approve_backup_signer(
        self,
        backup_id: str,
        *,
        expected_fingerprint_sha256: str,
        actor_id: str | None,
    ) -> dict[str, Any]:
        """Approve only the exact signer embedded in one verified backup."""
        self._require_control_identity_attached()
        try:
            with self.operations.global_exclusive() as operation_lease:
                backup_id = await self._resolve_backup_reference(backup_id)
                with self.operations.backup_shared(backup_id) as backup_lease:
                    inspection = await _to_thread_quiescent(
                        self.store.inspect_set,
                        backup_id,
                        expected_public_key=None,
                        verify_resources=True,
                    )
                    actual_fingerprint = inspection.manifest.signer_fingerprint_sha256
                    if not secrets.compare_digest(
                        actual_fingerprint,
                        str(expected_fingerprint_sha256 or ""),
                    ):
                        raise BackupServiceError(
                            409,
                            "backup_signer_approval_mismatch",
                            "The confirmed fingerprint does not match the verified backup signer.",
                            {"signerFingerprintSha256": actual_fingerprint},
                        )
                    self._operation_commit_guard(operation_lease, backup_lease)
                    if secrets.compare_digest(
                        inspection.signer_public_key,
                        self.identity.public_key_bytes,
                    ):
                        return {
                            "backupId": backup_id,
                            "algorithm": "Ed25519",
                            "fingerprintSha256": actual_fingerprint,
                            "publicKey": base64.urlsafe_b64encode(
                                inspection.signer_public_key
                            ).decode("ascii").rstrip("="),
                            "approvedAt": None,
                            "actorId": actor_id,
                            "trustStatus": "trusted_local",
                        }
                    record = await _to_thread_quiescent(
                        self.trust.approve,
                        inspection.signer_public_key,
                        actor_id=actor_id,
                    )
                    self._operation_commit_guard(operation_lease, backup_lease)
                    return {"backupId": backup_id, **record.to_dict()}
        except BackupServiceError:
            raise
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc
        except BackupTrustError as exc:
            raise BackupServiceError(
                500,
                "backup_trust_store_invalid",
                "The backup signer approval could not be persisted safely.",
            ) from exc
        except BackupError as exc:
            raise self._map_error(exc, operation="inspect") from exc

    def revoke_backup_signer(self, fingerprint_sha256: str) -> bool:
        """Revoke one external signer approval without touching its backups."""
        self._require_control_identity_attached()
        if (
            not isinstance(fingerprint_sha256, str)
            or len(fingerprint_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in fingerprint_sha256
            )
        ):
            raise BackupServiceError(
                422,
                "backup_signer_fingerprint_invalid",
                "The signer fingerprint SHA-256 is invalid.",
            )
        try:
            with self.operations.global_exclusive() as operation_lease:
                revoked = self.trust.revoke(fingerprint_sha256)
                self._operation_commit_guard(operation_lease)
                return revoked
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc
        except BackupTrustError as exc:
            raise BackupServiceError(
                500,
                "backup_trust_store_invalid",
                "The backup signer approval could not be revoked safely.",
            ) from exc

    @contextmanager
    def mutation_operation(self) -> Any:
        """Return the single cross-worker mutation context for API streaming."""
        self._require_control_identity_attached()
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
        self._require_control_identity_attached()
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
        preflight_warnings: list[str] = []
        manifest_created_at: datetime | None = None
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
                "Backup preparation did not produce its signed metadata.",
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
            "preflight_warnings": preflight_warnings,
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
                identity_guard=lambda: self._operation_commit_guard(
                    operation_lease
                ),
            )
        except BackupError as exc:
            raise self._map_error(exc, operation="seal") from exc
        return self._inspection_dict(inspection)

    async def list_local_backups(self) -> list[dict[str, Any]]:
        self._require_control_identity_attached()
        try:
            summaries = await _to_thread_quiescent(self.store.list_sets)
            self._require_control_identity_attached()
            result: list[dict[str, Any]] = []
            for item in summaries:
                filename = (
                    backup_transport_filename(item.manifest)
                    if item.manifest is not None
                    else None
                )
                trust_status = (
                    self._trust_status_for_public_key(item.signer_public_key)
                    if item.integrity_status == "valid"
                    and item.signer_public_key is not None
                    else "invalid"
                )
                authenticated = trust_status in {"trusted_local", "trusted_external"}
                result.append({
                    "id": item.backup_id,
                    "key": filename or item.backup_id,
                    "createdAt": item.created_at,
                    "modified": item.created_at,
                    "signerFingerprintSha256": item.signer_fingerprint_sha256,
                    "resourceCount": item.resource_count,
                    "totalSize": item.total_size,
                    "size": (
                        backup_transport_size(item.manifest)
                        if item.manifest is not None
                        else None
                    ),
                    "filename": filename,
                    "status": (
                        "invalid"
                        if item.integrity_status != "valid"
                        else "sealed"
                        if authenticated
                        else "quarantined"
                    ),
                    "authenticated": authenticated,
                    "signatureVerified": item.integrity_status == "valid",
                    "trustStatus": trust_status,
                    "integrityStatus": item.integrity_status,
                    "errorCode": item.error_code,
                })
            return result
        except BackupTrustError as exc:
            raise BackupServiceError(
                500,
                "backup_trust_store_invalid",
                "The backup signer trust store is missing or unsafe.",
            ) from exc
        except BackupError as exc:
            raise self._map_error(exc, operation="list") from exc

    async def inspect_local_backup(
        self,
        backup_id: str,
        *,
        resource_offset: int = 0,
        resource_limit: int = BACKUP_INSPECTION_DEFAULT_RESOURCE_LIMIT,
    ) -> dict[str, Any]:
        self._validate_inspection_resource_page(resource_offset, resource_limit)
        backup_id = await self._resolve_backup_reference(backup_id)
        try:
            with self.operations.backup_shared(backup_id) as backup_lease:
                result = await self._inspect_local_backup_under_lease(
                    backup_id,
                    resource_offset=resource_offset,
                    resource_limit=resource_limit,
                )
                self._operation_commit_guard(backup_lease)
                return result
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc

    async def _inspect_local_backup_under_lease(
        self,
        backup_id: str,
        *,
        resource_offset: int = 0,
        resource_limit: int = BACKUP_INSPECTION_DEFAULT_RESOURCE_LIMIT,
    ) -> dict[str, Any]:
        self._validate_inspection_resource_page(resource_offset, resource_limit)
        self._require_control_identity_attached()
        try:
            inspection = await _to_thread_quiescent(
                self.store.inspect_set,
                backup_id,
                expected_public_key=None,
                verify_resources=True,
            )
            self._require_control_identity_attached()
        except BackupError as exc:
            raise self._map_error(exc, operation="inspect") from exc
        return self._inspection_dict(
            inspection,
            resource_offset=resource_offset,
            resource_limit=resource_limit,
        )

    async def materialize_local_backup_zip(
        self,
        backup_id: str,
    ) -> PinnedBackupZip:
        self._require_control_identity_attached()
        backup_id = await self._resolve_backup_reference(backup_id)
        leases = ExitStack()
        try:
            backup_lease = leases.enter_context(
                self.operations.backup_shared(backup_id)
            )
            materialization_lease = leases.enter_context(
                self.operations.backup_materialization_exclusive(backup_id)
            )
            pinned = await _to_thread_quiescent(
                materialize_backup_zip,
                self.store,
                backup_id,
                expected_public_key=None,
                chunk_size=int(self.settings.backup_transport_chunk_size),
                cancel_cleanup=lambda archive: archive.close(),
            )
            try:
                self._operation_commit_guard(materialization_lease)
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
        operation_lease: BackupOperationLease | None = None,
    ) -> dict[str, Any]:
        if operation_lease is None:
            try:
                with self.operations.global_exclusive() as owned_lease:
                    return await self._upload_local_backup_under_lease(
                        source,
                        operation_lease=owned_lease,
                    )
            except BackupOperationError as exc:
                raise self._map_operation_error(exc) from exc
        return await self._upload_local_backup_under_lease(
            source,
            operation_lease=operation_lease,
        )

    async def _upload_local_backup_under_lease(
        self,
        source: BinaryIO,
        *,
        operation_lease: BackupOperationLease,
    ) -> dict[str, Any]:
        if operation_lease.scope != "global" or operation_lease.mode != "exclusive":
            raise BackupServiceError(
                500,
                "backup_operation_control_invalid",
                "Upload requires the global native backup mutation lease.",
            )
        operation_lease.verify_attached()
        self._require_control_identity_attached()
        try:
            limits = BackupTransportLimits.from_settings(self.settings)
        except (AttributeError, TypeError, ValueError) as exc:
            raise BackupServiceError(
                500,
                "backup_transport_limits_invalid",
                "The native backup ZIP limits are invalid.",
            ) from exc

        prepared_import: PreparedBackupImport | None = None
        finalized = False
        try:
            prepared_import = await _to_thread_quiescent(
                prepare_backup_zip_import,
                self.store,
                source,
                expected_public_key=None,
                limits=limits,
                cancel_cleanup=lambda prepared: prepared.abort(),
            )
            operation_lease.verify_attached()
            self._require_control_identity_attached()
            imported_manifest = BackupManifest.from_bytes(
                prepared_import.manifest_bytes
            )
            await self._assert_backup_aliases_available(
                backup_id=imported_manifest.backup_id,
                filename=backup_transport_filename(imported_manifest),
            )
            operation_lease.verify_attached()
            self._require_control_identity_attached()
            with self.operations.backup_exclusive(
                prepared_import.prepared.backup_id
            ) as backup_lease:
                inspection = await _finalize_backup_atomically(
                    self.store.finalize_imported_set,
                    prepared_import.prepared,
                    manifest_bytes=prepared_import.manifest_bytes,
                    signature=prepared_import.signature,
                    signer_public_key=prepared_import.signer_public_key,
                    expected_public_key=prepared_import.signer_public_key,
                    seal_gate=BackupSealGate(),
                    identity_guard=lambda: self._operation_commit_guard(
                        operation_lease,
                        backup_lease,
                    ),
                )
            finalized = True
            return self._inspection_dict(inspection)
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc
        except BackupTransportError as exc:
            raise self._map_transport_error(exc) from exc
        except BackupError as exc:
            raise self._map_error(exc, operation="upload") from exc
        finally:
            if prepared_import is not None and not finalized:
                try:
                    await _to_thread_cleanup_quiescent(prepared_import.abort)
                except asyncio.CancelledError:
                    raise
                except BaseException as cleanup_error:
                    raise BackupServiceError(
                        500,
                        "backup_upload_partial_cleanup_failed",
                        "The rejected upload could not be removed safely.",
                    ) from cleanup_error

    async def delete_local_backup(self, backup_id: str) -> None:
        self._require_control_identity_attached()
        try:
            with self.operations.global_exclusive() as operation_lease:
                backup_id = await self._resolve_backup_reference(backup_id)
                with self.operations.backup_exclusive(backup_id) as backup_lease:
                    self._operation_commit_guard(operation_lease, backup_lease)
                    await _delete_backup_atomically(
                        self.store.delete_set,
                        backup_id,
                        delete_gate=BackupDeleteGate(),
                        pre_commit_guard=lambda: self._operation_commit_guard(
                            operation_lease,
                            backup_lease,
                        ),
                    )
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc
        except BackupError as exc:
            raise self._map_error(exc, operation="delete") from exc


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
            backup_id = await self._resolve_backup_reference(backup_id)
            with self.operations.backup_shared(backup_id) as backup_lease:
                inspection = await self._trusted_inspection(backup_id)
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

                preflight_warnings: list[str] = []
                async with self.engine.connect() as connection:
                    runtime_identity = await self._postgres_server_identity(connection)
                    report = await preflight_destructive_restore_role(connection)
                    report.require_ok()
                    preflight_warnings.extend(report.warnings)
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

                # Reauthenticate and pin the archive only after all writers are
                # excluded. No path is reopened between this point and restore.
                inspection = await self._trusted_inspection(backup_id)
                refreshed_reference_inventory = (
                    _require_manifest_file_reference_inventory(inspection)
                )
                if refreshed_reference_inventory != expected_reference_inventory:
                    raise BackupIntegrityError(
                        "signed local-file reference inventory changed during restore"
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
                        "backupId": backup_id,
                        "restoreId": restore_id,
                        "status": "restart_scheduled",
                        "destructive": True,
                        "actorId": actor_id,
                        "preflightWarnings": preflight_warnings,
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


    def _load_identity(self) -> BackupIdentity:
        sets_dir = self.backup_root / "sets"
        has_sealed_set = False
        if sets_dir.is_dir() and not sets_dir.is_symlink():
            try:
                has_sealed_set = any(
                    entry.is_dir(follow_symlinks=False)
                    and not entry.name.startswith(".")
                    and (Path(entry.path) / "SEALED").is_file()
                    for entry in os.scandir(sets_dir)
                )
            except OSError as exc:
                raise BackupServiceError(
                    500,
                    "backup_store_unreadable",
                    "The local backup store cannot be inspected safely.",
                ) from exc
        try:
            if has_sealed_set:
                return BackupIdentity.load_existing_at(self.control_root)
            return BackupIdentity.load_or_create_at(self.control_root)
        except BackupIdentityMissingError as exc:
            raise BackupServiceError(
                409,
                "backup_identity_missing",
                "Sealed backups exist but their local signing identity is missing.",
            ) from exc
        except BackupIdentityError as exc:
            raise BackupServiceError(
                500,
                "backup_identity_invalid",
                "The local backup signing identity is missing or unsafe.",
            ) from exc

    def _require_control_root_attached(self) -> None:
        if self._closed:
            raise BackupServiceError(
                500,
                "backup_service_closed",
                "The native backup service is closed.",
            )
        try:
            self.control_root.verify_attached()
        except ControlPlaneSafetyError as exc:
            raise BackupServiceError(
                500,
                "backup_control_detached",
                "The native backup control plane is detached or unsafe.",
            ) from exc

    def _require_control_identity_attached(self) -> None:
        self._require_control_root_attached()
        try:
            self.identity.verify_attached()
        except BackupIdentityError as exc:
            raise BackupServiceError(
                500,
                "backup_identity_invalid",
                "The local backup signing identity is detached or unsafe.",
            ) from exc
        self._require_control_root_attached()

    def _operation_commit_guard(
        self,
        *leases: BackupOperationLease,
    ) -> None:
        for lease in leases:
            lease.verify_attached()
        self._require_control_identity_attached()
        for lease in leases:
            lease.verify_attached()

    def _validate_roots(self) -> None:
        active_data_dir = Path(self.settings.data_dir).expanduser().resolve(
            strict=False
        )
        named_roots = {
            "backup_root": self.backup_root,
            "backup_control_dir": self.control_dir,
        }
        resolved_roots = {
            label: root.resolve(strict=False)
            for label, root in named_roots.items()
        }
        for label, root in resolved_roots.items():
            if (
                root == active_data_dir
                or root.is_relative_to(active_data_dir)
                or active_data_dir.is_relative_to(root)
            ):
                raise BackupServiceError(
                    409,
                    "unsafe_backup_root",
                    f"{label} must be outside the active data_dir.",
                )
        values = list(resolved_roots.items())
        for index, (left_name, left) in enumerate(values):
            for right_name, right in values[index + 1 :]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise BackupServiceError(
                        409,
                        "overlapping_backup_roots",
                        f"{left_name} and {right_name} must not overlap.",
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

    async def _trusted_inspection(
        self,
        backup_id: str,
    ) -> AuthenticatedBackupInspection:
        self._require_control_identity_attached()
        try:
            unsigned_capability = await _to_thread_quiescent(
                self.store.inspect_set,
                backup_id,
                expected_public_key=None,
                verify_resources=True,
            )
            signer_public_key = unsigned_capability.signer_public_key
            if secrets.compare_digest(
                signer_public_key,
                self.identity.public_key_bytes,
            ):
                approved_public_key = self.identity.public_key_bytes
            else:
                fingerprint = unsigned_capability.manifest.signer_fingerprint_sha256
                approved_public_key = await _to_thread_quiescent(
                    self.trust.approved_public_key,
                    fingerprint,
                )
                if approved_public_key is None or not secrets.compare_digest(
                    approved_public_key,
                    signer_public_key,
                ):
                    encoded_key = base64.urlsafe_b64encode(
                        signer_public_key
                    ).decode("ascii").rstrip("=")
                    raise BackupServiceError(
                        409,
                        "backup_signer_untrusted",
                        "The backup is signed correctly, but its external signer has not been approved.",
                        {
                            "signerFingerprintSha256": fingerprint,
                            "signerPublicKey": encoded_key,
                            "trustStatus": "authenticated_untrusted",
                        },
                    )
            inspection = await _to_thread_quiescent(
                self.store.authenticate_set,
                backup_id,
                approved_public_key=approved_public_key,
            )
            self._require_control_identity_attached()
            return inspection
        except BackupServiceError:
            raise
        except BackupTrustError as exc:
            raise BackupServiceError(
                500,
                "backup_trust_store_invalid",
                "The backup signer trust store is missing or unsafe.",
            ) from exc
        except BackupError as exc:
            raise self._map_error(exc, operation="inspect") from exc

    @staticmethod
    def _manifest_contract(
        inspection: BackupInspection | AuthenticatedBackupInspection,
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

    def _trust_status_for_public_key(self, public_key: bytes | None) -> str:
        if public_key is None:
            return "invalid"
        if secrets.compare_digest(public_key, self.identity.public_key_bytes):
            return "trusted_local"
        fingerprint = hashlib.sha256(public_key).hexdigest()
        try:
            approved = self.trust.approved_public_key(fingerprint)
        except BackupTrustError as exc:
            raise BackupServiceError(
                500,
                "backup_trust_store_invalid",
                "The backup signer trust store is missing or unsafe.",
            ) from exc
        if approved is not None and secrets.compare_digest(approved, public_key):
            return "trusted_external"
        return "authenticated_untrusted"

    @staticmethod
    def _validate_inspection_resource_page(
        resource_offset: int,
        resource_limit: int,
    ) -> None:
        if (
            isinstance(resource_offset, bool)
            or not isinstance(resource_offset, int)
            or resource_offset < 0
            or isinstance(resource_limit, bool)
            or not isinstance(resource_limit, int)
            or resource_limit < 1
            or resource_limit > BACKUP_INSPECTION_MAX_RESOURCE_LIMIT
        ):
            raise BackupServiceError(
                422,
                "backup_inspection_page_invalid",
                "Backup resource pagination is outside the supported bounds.",
                data={
                    "maximumResourceLimit": BACKUP_INSPECTION_MAX_RESOURCE_LIMIT,
                },
            )

    def _inspection_dict(
        self,
        inspection: BackupInspection,
        *,
        resource_offset: int = 0,
        resource_limit: int = BACKUP_INSPECTION_DEFAULT_RESOURCE_LIMIT,
    ) -> dict[str, Any]:
        self._validate_inspection_resource_page(resource_offset, resource_limit)
        manifest = inspection.manifest
        resource_count = len(manifest.resources)
        resource_page = manifest.resources[
            resource_offset : resource_offset + resource_limit
        ]
        filename = backup_transport_filename(manifest)
        signer = base64.urlsafe_b64encode(
            inspection.signer_public_key
        ).decode("ascii").rstrip("=")
        trust_status = self._trust_status_for_public_key(inspection.signer_public_key)
        authenticated = trust_status in {"trusted_local", "trusted_external"}
        return {
            "id": manifest.backup_id,
            "key": filename,
            "createdAt": manifest.created_at,
            "modified": manifest.created_at,
            "status": "sealed" if authenticated else "quarantined",
            "authenticated": authenticated,
            "signatureVerified": True,
            "trustStatus": trust_status,
            "signerFingerprintSha256": manifest.signer_fingerprint_sha256,
            "signerPublicKey": signer,
            "resourcesVerified": inspection.resources_verified,
            "resourceCount": resource_count,
            "resourceOffset": resource_offset,
            "resourceLimit": resource_limit,
            "resourcesReturned": len(resource_page),
            "hasMoreResources": resource_offset + len(resource_page) < resource_count,
            "totalSize": manifest.total_size,
            "size": backup_transport_size(manifest),
            "filename": filename,
            "metadata": dict(manifest.metadata),
            "resources": [
                {
                    "path": resource.path,
                    "size": resource.size,
                    "sha256": resource.sha256,
                }
                for resource in resource_page
            ],
        }

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
        elif exc.code == "backup_signer_untrusted":
            status_code = 409
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
                f"The signed backup failed integrity verification: {exc}",
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
