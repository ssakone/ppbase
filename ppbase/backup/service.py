"""Application orchestration for signed backup, staged restore, and activation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import stat
import socket
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
import secrets
from typing import Any, BinaryIO, Callable, TypeVar

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ppbase import __version__
from ppbase.backup.activation import (
    ActivationError,
    BackupActivationStore,
    activation_restart_spec,
    replace_serve_command_targets,
)
from ppbase.backup.control import ControlPlaneRoot, ControlPlaneSafetyError
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
    canonical_json_bytes,
    format_manifest_timestamp,
)
from ppbase.backup.operations import (
    BackupOperationCoordinator,
    BackupOperationError,
    BackupOperationLease,
    BackupOperationSafetyError,
)
from ppbase.backup.plans import StagingPlan, StagingPlanError, StagingPlanStore
from ppbase.backup.postgres import (
    DatabaseContract,
    LibpqConnectionInfo,
    PostgresBackupError,
    create_target_database,
    detect_postgres_versions,
    grant_dump_role_read_access,
    grant_dump_role_runtime_default_privileges,
    inspect_database_contract,
    inspect_pg_restore_archive,
    preflight_database_contract,
    preflight_dump_role,
    replace_sqlalchemy_database,
    run_pg_dump,
    run_pg_restore_from_fd,
    set_backup_control_search_path,
    sqlalchemy_url_to_libpq,
    validate_postgres_identifier,
)
from ppbase.backup.storage import (
    JWT_SECRET_RESOURCE,
    AnchoredStagingDataDir,
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
from ppbase.backup.validation import (
    generate_clone_jwt_secret,
    rotate_clone_database_secrets,
    validate_staged_database,
)
from ppbase.services.async_utils import (
    to_thread_quiescent as _to_thread_quiescent,
)
from ppbase.services.file_storage import (
    open_file_stream,
    pin_storage_config,
    resolve_storage_config_from_settings_payload,
)
from ppbase.services.file_references import (
    LocalFileReference,
    read_canonical_local_file_references,
)
from ppbase.services.migration_runner import (
    MigrationLockError,
    apply_pending_on_connection,
    get_pending_migrations,
    migration_lock_on_connection,
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


_DESTINATION_DOMAIN = b"PPBASE-RESTORE-DESTINATION-V1\0"
_FILE_REFERENCE_INVENTORY_DOMAIN = b"PPBASE-LOCAL-FILE-REFERENCES-V1\0"
_FILE_REFERENCE_INVENTORY_KEY = "local_file_reference_inventory"
_FILE_REFERENCE_INVENTORY_VERSION = 1
_RESTORE_GUARD_POLL_SECONDS = 0.1
_RESTORE_GUARD_VERIFY_TIMEOUT_SECONDS = 2.0
BACKUP_INSPECTION_DEFAULT_RESOURCE_LIMIT = 100
BACKUP_INSPECTION_MAX_RESOURCE_LIMIT = 250
_T = TypeVar("_T")
_RETAINED_CUTOVER_GUARDS: set["BackupCutoverGuard"] = set()
_FAILED_CUTOVER_OPERATION_CONTEXTS: list[ExitStack] = []


class BackupCutoverGuard:
    """Retain every cutover exclusion until exec or durable rollback.

    The PostgreSQL barrier is released first and filesystem operation leases
    second, but only after the activation journal has already selected the
    previous target again.  Instances keep themselves alive so an exceptional
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


class PreparedBackupActivation(dict[str, Any]):
    """JSON-compatible activation response carrying its process-local guard."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        cutover_guard: BackupCutoverGuard,
    ) -> None:
        super().__init__(payload)
        self.cutover_guard = cutover_guard


def _file_reference_inventory(
    references: tuple[LocalFileReference, ...],
) -> dict[str, Any]:
    canonical = tuple(sorted(set(references)))
    digest = hashlib.sha256(_FILE_REFERENCE_INVENTORY_DOMAIN)
    for reference in canonical:
        encoded = canonical_json_bytes(list(reference))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {
        "version": _FILE_REFERENCE_INVENTORY_VERSION,
        "count": len(canonical),
        "sha256": digest.hexdigest(),
    }


def _require_manifest_file_reference_inventory(
    inspection: AuthenticatedBackupInspection,
    references: tuple[LocalFileReference, ...],
) -> None:
    raw = inspection.manifest.metadata.get(_FILE_REFERENCE_INVENTORY_KEY)
    if not isinstance(raw, dict) or set(raw) != {"version", "count", "sha256"}:
        raise BackupIntegrityError(
            "backup manifest has no valid local-file reference inventory"
        )
    version = raw.get("version")
    count = raw.get("count")
    sha256 = raw.get("sha256")
    if (
        isinstance(version, bool)
        or version != _FILE_REFERENCE_INVENTORY_VERSION
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise BackupIntegrityError(
            "backup manifest local-file reference inventory is malformed"
        )
    actual = _file_reference_inventory(references)
    if count != actual["count"] or not secrets.compare_digest(
        sha256,
        str(actual["sha256"]),
    ):
        raise BackupIntegrityError(
            "restored local-file references differ from the signed inventory"
        )


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
            if opened is not None:
                opened.close()
            raise BackupIntegrityError(
                "database references a local file missing from active storage"
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


@dataclass(frozen=True, slots=True)
class _RestoreDestinationPreflight:
    creator_url: str
    restore_url: str
    creator_tool_url: URL
    restore_tool_url: URL
    target_owner: str
    creator_info: LibpqConnectionInfo
    restore_info: LibpqConnectionInfo
    runtime_info: LibpqConnectionInfo
    dump_info: LibpqConnectionInfo
    creator_identity: dict[str, Any]
    restore_identity: dict[str, Any]
    runtime_identity: dict[str, Any]
    creator_hostaddr: str | None
    restore_hostaddr: str | None
    fingerprint_sha256: str
    warnings: tuple[str, ...]


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


class NativeBackupService:
    """Create signed local sets and validate restores in brand-new targets."""

    def __init__(self, engine: AsyncEngine, settings: Any) -> None:
        self._closed = False
        self.engine = engine
        self.settings = settings
        self.backup_root = Path(settings.backup_root).expanduser().resolve(strict=False)
        self.control_dir = Path(settings.backup_control_dir).expanduser().absolute()
        self.staging_root = Path(settings.backup_staging_root).expanduser().resolve(
            strict=False
        )
        configured_target_root = str(
            getattr(settings, "backup_target_root", "") or ""
        ).strip()
        self.target_root = Path(
            configured_target_root
            or f"{getattr(settings, 'backup_staging_root')}_targets"
        ).expanduser().resolve(strict=False)
        self._validate_roots()
        try:
            self.control_root = ControlPlaneRoot.open(self.control_dir)
        except ControlPlaneSafetyError as exc:
            raise BackupServiceError(
                500,
                "backup_control_invalid",
                "The native backup control plane is missing or unsafe.",
            ) from exc
        try:
            self.operations = BackupOperationCoordinator(self.control_root)
            self.plans = StagingPlanStore(
                self.control_root,
                self.staging_root,
                self.target_root,
            )
            self.trust = BackupTrustStore(self.control_root)
            self.activations = BackupActivationStore(self.control_root)
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
        except StagingPlanError as exc:
            self.close()
            raise BackupServiceError(
                500,
                "backup_control_invalid",
                "The native backup control plane is missing or unsafe.",
            ) from exc
        except BackupTrustError as exc:
            self.close()
            raise BackupServiceError(
                500,
                "backup_control_invalid",
                "The native backup signer trust store is missing or unsafe.",
            ) from exc
        except ActivationError as exc:
            self.close()
            raise BackupServiceError(
                500,
                "backup_control_invalid",
                "The native backup activation store is missing or unsafe.",
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
        activations = getattr(self, "activations", None)
        plans = getattr(self, "plans", None)
        operations = getattr(self, "operations", None)
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
                    if activations is not None:
                        activations.close()
                finally:
                    try:
                        if trust is not None:
                            trust.close()
                    finally:
                        try:
                            if plans is not None:
                                plans.close()
                        finally:
                            try:
                                if operations is not None:
                                    operations.close()
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
        dump_url = self._dump_configuration()
        try:
            dump_info = sqlalchemy_url_to_libpq(dump_url)
        except PostgresBackupError as exc:
            raise BackupServiceError(
                409,
                "dump_database_url_invalid",
                "The configured dump PostgreSQL DSN is invalid.",
            ) from exc

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
        dump_version = ""
        restore_version = ""
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
                            "Native backup v1 supports the local storage "
                            "backend only.",
                        )
                    builder = self.store.begin_set()
                    async with lease.connection.begin():
                        await lease.connection.execute(
                            text(
                                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, "
                                "READ ONLY"
                            )
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
                        runtime_identity = await self._postgres_server_identity(
                            lease.connection
                        )
                        runtime_role = runtime_identity["role"]
                        if dump_info.username == runtime_role:
                            raise PostgresBackupError(
                                "pg_dump must use a dedicated read-only login "
                                "distinct from the PPBase runtime role"
                            )
                        contract = await inspect_database_contract(lease.connection)
                        if dump_info.database != contract.database:
                            raise PostgresBackupError(
                                "pg_dump DSN must target the active PPBase database"
                            )
                        dump_engine = create_async_engine(
                            dump_url,
                            poolclass=NullPool,
                        )
                        try:
                            async with dump_engine.connect() as dump_connection:
                                dump_identity = await self._postgres_server_identity(
                                    dump_connection
                                )
                                if dump_identity["role"] != dump_info.username:
                                    raise PostgresBackupError(
                                        "pg_dump DSN login does not match current_user"
                                    )
                                if dump_identity["database"] != contract.database:
                                    raise PostgresBackupError(
                                        "pg_dump preflight reached a different "
                                        "database"
                                    )
                                if self._server_instance_key(
                                    dump_identity
                                ) != self._server_instance_key(runtime_identity):
                                    raise PostgresBackupError(
                                        "pg_dump DSN reached a different PostgreSQL "
                                        "server instance"
                                    )
                                dump_report = await preflight_dump_role(
                                    dump_connection
                                )
                                dump_report.require_ok()
                                preflight_warnings.extend(dump_report.warnings)
                                await dump_connection.rollback()
                        finally:
                            await dump_engine.dispose()
                        versions = await detect_postgres_versions(
                            lease.connection,
                            pg_dump=self.settings.backup_pg_dump_path,
                            pg_restore=self.settings.backup_pg_restore_path,
                        )
                        dump_version = versions.pg_dump.version
                        restore_version = versions.pg_restore.version
                        source_summary = await self._source_summary(lease.connection)
                        source_file_references = (
                            await read_canonical_local_file_references(
                                lease.connection
                            )
                        )
                        snapshot_id = str(
                            (
                                await lease.connection.execute(
                                    text("SELECT pg_export_snapshot()")
                                )
                            ).scalar_one()
                        )
                        await run_pg_dump(
                            dump_url,
                            builder.database_dump_path,
                            pg_dump=self.settings.backup_pg_dump_path,
                            expected_server_major=contract.server_major,
                            passfile_directory=builder.path,
                            snapshot_id=snapshot_id,
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
            "postgres_tools": {
                "pg_dump": dump_version,
                "pg_restore": restore_version,
            },
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
                    try:
                        references = self.plans.validated_references(backup_id)
                    except StagingPlanError as exc:
                        raise BackupServiceError(
                            500,
                            "backup_control_invalid",
                            "Staging plan references cannot be inspected safely.",
                        ) from exc
                    for plan in references:
                        try:
                            statuses = self.activations.statuses_for_plan(
                                plan.plan_id,
                                backup_id=backup_id,
                            )
                        except ActivationError as exc:
                            raise BackupServiceError(
                                500,
                                "backup_activation_state_invalid",
                                "Backup activation references cannot be inspected safely.",
                            ) from exc
                        if not statuses or any(
                            status
                            in {
                                "restart_scheduled",
                                "starting",
                                "rollback_pending",
                            }
                            for status in statuses
                        ):
                            raise BackupServiceError(
                                409,
                                "backup_in_use",
                                "The backup is still required by a validated "
                                "restore staging plan.",
                            )
                        if any(
                            status not in {"healthy", "rolled_back"}
                            for status in statuses
                        ):
                            raise BackupServiceError(
                                500,
                                "backup_activation_state_invalid",
                                "A staging plan has an unknown activation status.",
                            )
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

    async def create_staging_plan(
        self,
        backup_id: str,
        *,
        jwt_secret_mode: str,
        actor_id: str | None,
    ) -> dict[str, Any]:
        try:
            with self.operations.global_exclusive() as operation_lease:
                backup_id = await self._resolve_backup_reference(backup_id)
                with self.operations.backup_shared(backup_id) as backup_lease:
                    result = await self._create_staging_plan_under_lease(
                        backup_id,
                        jwt_secret_mode=jwt_secret_mode,
                        actor_id=actor_id,
                        backup_lease=backup_lease,
                    )
                    self._operation_commit_guard(operation_lease, backup_lease)
                    return result
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc

    async def _create_staging_plan_under_lease(
        self,
        backup_id: str,
        *,
        jwt_secret_mode: str,
        actor_id: str | None,
        backup_lease: BackupOperationLease,
    ) -> dict[str, Any]:
        self._require_control_identity_attached()
        try:
            inspection = await self._trusted_inspection(backup_id)
            jwt_resource_mode = self._manifest_jwt_secret_mode(inspection)
            if (
                jwt_secret_mode == "disaster_recovery"
                and jwt_resource_mode != "included_resource"
            ):
                raise BackupServiceError(
                    409,
                    "external_jwt_secret_unverifiable",
                    "Disaster recovery requires the signed JWT-secret resource; "
                    "an externally managed secret cannot yet be proven identical.",
                )
            contract = self._manifest_contract(inspection)
            destination = await self._preflight_restore_destination(
                inspection,
                contract,
            )
        except BackupServiceError:
            raise
        except (BackupError, PostgresBackupError) as exc:
            raise self._map_error(exc, operation="plan") from exc

        manifest_sha256 = hashlib.sha256(
            inspection.manifest.to_bytes()
        ).hexdigest()
        self._require_control_identity_attached()
        try:
            plan = self.plans.create(
                backup_id=backup_id,
                manifest_sha256=manifest_sha256,
                destination_fingerprint_sha256=(
                    destination.fingerprint_sha256
                ),
                jwt_secret_mode=jwt_secret_mode,
                actor_id=actor_id,
                pre_commit_guard=lambda: self._operation_commit_guard(
                    backup_lease
                ),
            )
        except StagingPlanError as exc:
            raise BackupServiceError(
                400,
                "invalid_staging_plan",
                str(exc),
            ) from exc
        payload = plan.as_dict()
        payload["preflightWarnings"] = list(destination.warnings)
        return payload

    def inspect_staging_plan(self, plan_id: str) -> dict[str, Any]:
        self._require_control_root_attached()
        try:
            return self.plans.inspect(plan_id).as_dict()
        except StagingPlanError as exc:
            raise BackupServiceError(
                404,
                "staging_plan_not_found",
                "The sealed staging plan was not found or is invalid.",
            ) from exc

    async def abandon_staging_plan(
        self,
        plan_id: str,
        *,
        expected_plan_hash: str,
        actor_id: str | None,
    ) -> dict[str, Any]:
        """Durably retire a quiescent plan so its backup can be released."""
        self._require_control_identity_attached()
        try:
            with self.operations.global_exclusive() as operation_lease:
                try:
                    plan = self.plans.inspect(plan_id)
                except StagingPlanError as exc:
                    raise BackupServiceError(
                        409,
                        "staging_plan_not_abandonable",
                        str(exc),
                    ) from exc
                with self.operations.backup_shared(plan.backup_id) as backup_lease:
                    try:
                        statuses = self.activations.statuses_for_plan(
                            plan.plan_id,
                            backup_id=plan.backup_id,
                        )
                    except ActivationError as exc:
                        raise BackupServiceError(
                            500,
                            "backup_activation_state_invalid",
                            "The staging plan activation state cannot be inspected safely.",
                        ) from exc
                    if any(
                        status
                        in {
                            "restart_scheduled",
                            "starting",
                            "rollback_pending",
                        }
                        for status in statuses
                    ):
                        raise BackupServiceError(
                            409,
                            "backup_activation_in_progress",
                            "A staging plan cannot be abandoned while its "
                            "activation or rollback is in progress.",
                        )
                    self._operation_commit_guard(operation_lease, backup_lease)
                    try:
                        abandoned = self.plans.abandon(
                            plan.plan_id,
                            expected_plan_hash=expected_plan_hash,
                            actor_id=actor_id,
                            pre_commit_guard=lambda: self._operation_commit_guard(
                                operation_lease,
                                backup_lease,
                            ),
                        )
                    except StagingPlanError as exc:
                        raise BackupServiceError(
                            409,
                            "staging_plan_not_abandonable",
                            str(exc),
                        ) from exc
                    self._operation_commit_guard(operation_lease, backup_lease)
                    return abandoned.as_dict()
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc

    async def execute_staging_plan(
        self,
        plan_id: str,
        *,
        expected_plan_hash: str,
    ) -> dict[str, Any]:
        self._require_control_identity_attached()
        try:
            planned = self.plans.inspect(plan_id)
        except StagingPlanError as exc:
            raise BackupServiceError(
                409,
                "staging_plan_not_executable",
                str(exc),
            ) from exc
        try:
            with self.operations.global_exclusive() as operation_lease:
                with self.operations.backup_shared(
                    planned.backup_id
                ) as backup_lease:
                    return await self._execute_staging_plan_under_lease(
                        plan_id,
                        expected_plan_hash=expected_plan_hash,
                        operation_leases=(operation_lease, backup_lease),
                    )
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc

    async def activate_staging_plan(
        self,
        plan_id: str,
        *,
        expected_plan_hash: str,
        actor_id: str | None,
        activation_id: str | None = None,
        resume_token: str | None = None,
    ) -> PreparedBackupActivation:
        """Publish a validated target and the durable restart overlay."""
        self._require_control_identity_attached()
        try:
            planned = self.plans.inspect(plan_id)
        except StagingPlanError as exc:
            raise BackupServiceError(
                409,
                "staging_plan_not_activatable",
                str(exc),
            ) from exc
        operation_contexts = ExitStack()
        try:
            operation_lease = operation_contexts.enter_context(
                self.operations.global_exclusive()
            )
            backup_lease = operation_contexts.enter_context(
                self.operations.backup_shared(planned.backup_id)
            )
            try:
                canonical_plan = self.plans.inspect(plan_id)
            except StagingPlanError as exc:
                raise BackupServiceError(
                    409,
                    "staging_plan_not_activatable",
                    "The staging plan changed before activation acquired its leases.",
                ) from exc
            if canonical_plan.backup_id != planned.backup_id:
                raise BackupServiceError(
                    409,
                    "staging_plan_changed",
                    "The staging plan backup changed before activation.",
                )
            activation_arguments: dict[str, Any] = {}
            if activation_id is not None or resume_token is not None:
                activation_arguments = {
                    "activation_id": activation_id,
                    "resume_token": resume_token,
                }
            prepared = await self._activate_staging_plan_under_lease(
                canonical_plan,
                expected_plan_hash=expected_plan_hash,
                actor_id=actor_id,
                operation_leases=(operation_lease, backup_lease),
                **activation_arguments,
            )
            transferred_contexts = operation_contexts.pop_all()
            try:
                prepared.cutover_guard.retain_operation_context(
                    transferred_contexts
                )
            except BaseException:
                try:
                    self.abandon_prepared_activation(
                        str(prepared.get("activationId", "")),
                        error_code="activation_guard_transfer_failed",
                    )
                except BaseException:
                    # ExitStack does not promise implicit cleanup, but its
                    # generator contexts could still be finalized later. Keep
                    # the global/backup leases strongly reachable so a failed
                    # durable rollback remains fail-closed.
                    _FAILED_CUTOVER_OPERATION_CONTEXTS.append(
                        transferred_contexts
                    )
                    raise
                transferred_contexts.close()
                await prepared.cutover_guard.close()
                raise
            return prepared
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc
        finally:
            operation_contexts.close()

    async def _activate_staging_plan_under_lease(
        self,
        plan: StagingPlan,
        *,
        expected_plan_hash: str,
        actor_id: str | None,
        activation_id: str | None = None,
        resume_token: str | None = None,
        operation_leases: tuple[BackupOperationLease, ...],
    ) -> PreparedBackupActivation:
        self._operation_commit_guard(*operation_leases)
        if plan.plan_hash != expected_plan_hash:
            raise BackupServiceError(
                409,
                "staging_plan_hash_mismatch",
                "The supplied planHash does not match the sealed staging plan.",
            )
        if plan.status != "validated":
            raise BackupServiceError(
                409,
                "staging_plan_not_validated",
                "Only a validated staging plan can be activated.",
            )
        restart_command = get_restart_command()
        if not restart_command:
            raise BackupServiceError(
                409,
                "backup_activation_restart_unavailable",
                "PPBase was not started with a reusable restart command.",
            )
        if is_restart_scheduled():
            raise BackupServiceError(
                409,
                "backup_activation_restart_pending",
                "Another PPBase process restart is already pending.",
            )
        try:
            current_activation = self.activations.active()
        except ActivationError as exc:
            raise BackupServiceError(
                500,
                "backup_activation_state_invalid",
                "The durable activation state cannot be inspected safely.",
            ) from exc
        if current_activation is not None and current_activation.get("status") in {
            "restart_scheduled",
            "starting",
            "rollback_pending",
        }:
            raise BackupServiceError(
                409,
                "backup_activation_in_progress",
                "Another backup activation or rollback is already in progress.",
            )

        inspection = await self._trusted_inspection(plan.backup_id)
        self._require_plan_manifest(inspection, plan)
        contract = self._manifest_contract(inspection)
        destination = await self._preflight_restore_destination(
            inspection,
            contract,
        )
        if not secrets.compare_digest(
            destination.fingerprint_sha256,
            plan.destination_fingerprint_sha256,
        ):
            raise BackupServiceError(
                409,
                "staging_destination_changed",
                "The restore destination or PostgreSQL policy changed after validation.",
            )

        target = await _to_thread_quiescent(
            self.store.open_existing_staging_data_dir,
            self.target_root,
            Path(plan.plan_id) / "data",
            cancel_cleanup=lambda opened: opened.close(),
        )
        target_runtime_url = replace_sqlalchemy_database(
            self.settings.database_url,
            plan.target_database,
        )
        target_runtime_url_string = target_runtime_url.render_as_string(
            hide_password=False
        )
        target_engine = create_async_engine(target_runtime_url, poolclass=NullPool)
        cutover_guard: BackupCutoverGuard | None = None
        cutover_guard_transferred = False
        cutover_guard_must_remain = False
        try:
            if target.path != Path(plan.target_data_dir):
                raise BackupIntegrityError(
                    "validated staging data_dir no longer matches the sealed plan"
                )
            await _to_thread_quiescent(target.verify_attached)
            async with target_engine.connect() as connection:
                identity = await self._postgres_server_identity(connection)
                if (
                    identity["role"] != destination.runtime_info.username
                    or identity["database"] != plan.target_database
                    or self._server_instance_key(identity)
                    != self._server_instance_key(destination.runtime_identity)
                ):
                    raise PostgresBackupError(
                        "runtime DSN cannot pin the validated staging database"
                    )
                validation = await validate_staged_database(
                    connection,
                    expected_database=plan.target_database,
                    expected_owner=destination.target_owner,
                    expected_restore_role=destination.restore_info.username,
                    expected_runtime_role=destination.runtime_info.username,
                    expected_dump_role=destination.dump_info.username,
                    expected_contract=contract,
                )
                validation.require_valid()
                references = await read_canonical_local_file_references(connection)
                _require_manifest_file_reference_inventory(inspection, references)
                await _to_thread_quiescent(
                    target.verify_local_file_references,
                    references,
                )
                await connection.rollback()
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    async with session.begin():
                        pending = await get_pending_migrations(
                            session,
                            Path(self.settings.migrations_dir).expanduser(),
                        )
                if pending:
                    raise BackupServiceError(
                        409,
                        "staging_migrations_changed",
                        "Migration files changed after staging validation; create a new plan.",
                    )

            expected_jwt_sha256 = await _to_thread_quiescent(
                target.read_secret_sha256
            )
            signer_fingerprint = hashlib.sha256(
                inspection.signer_public_key
            ).hexdigest()
            previous_database_url = str(self.settings.database_url)
            previous_dump_database_url = self._dump_configuration()
            target_dump_database_url = replace_sqlalchemy_database(
                previous_dump_database_url,
                plan.target_database,
            ).render_as_string(hide_password=False)
            try:
                previous_data_path = Path(self.settings.data_dir).expanduser().resolve(
                    strict=True
                )
                previous_data_fd = os.open(
                    previous_data_path,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError as exc:
                raise BackupIntegrityError(
                    "active data_dir cannot be pinned before activation"
                ) from exc
            try:
                previous_data_info = os.fstat(previous_data_fd)
                if not stat.S_ISDIR(previous_data_info.st_mode):
                    raise BackupIntegrityError(
                        "active data_dir is not a directory"
                    )
            finally:
                os.close(previous_data_fd)
            previous_data_dir = str(previous_data_path)
            previous_jwt_sha256 = hashlib.sha256(
                self.settings.get_jwt_secret().encode("utf-8")
            ).hexdigest()
            previous_command = replace_serve_command_targets(
                restart_command,
                database_url=previous_database_url,
                data_dir=previous_data_dir,
            )
            target_command = replace_serve_command_targets(
                restart_command,
                database_url=target_runtime_url_string,
                data_dir=plan.target_data_dir,
            )

            def activation_commit_guard() -> None:
                target.verify_attached()
                self._operation_commit_guard(*operation_leases)

            retained_barrier = await acquire_retained_backup_write_barrier(
                self.engine,
                timeout_seconds=float(self.settings.backup_barrier_timeout),
            )
            restart_reservation = reserve_process_restart()
            if restart_reservation is None:
                await retained_barrier.close()
                raise BackupServiceError(
                    409,
                    "backup_activation_restart_pending",
                    "Another PPBase process restart is already pending.",
                )
            try:
                cutover_guard = BackupCutoverGuard(
                    retained_barrier,
                    restart_reservation=restart_reservation,
                )
            except BaseException:
                restart_reservation.release()
                await retained_barrier.close()
                raise
            await cutover_guard.verify_held()
            selected_activation_id = activation_id or secrets.token_hex(16)
            try:
                prepared = self.activations.prepare(
                    activation_id=selected_activation_id,
                    resume_token=resume_token,
                    plan_id=plan.plan_id,
                    backup_id=plan.backup_id,
                    plan_hash=plan.plan_hash,
                    manifest_sha256=plan.manifest_sha256,
                    signer_fingerprint_sha256=signer_fingerprint,
                    jwt_secret_mode=plan.jwt_secret_mode,
                    previous_database_url=previous_database_url,
                    previous_dump_database_url=previous_dump_database_url,
                    previous_data_dir=previous_data_dir,
                    previous_restart_command=previous_command,
                    target_database_url=target_runtime_url_string,
                    target_dump_database_url=target_dump_database_url,
                    target_data_dir=plan.target_data_dir,
                    target_restart_command=target_command,
                    expected_jwt_sha256=expected_jwt_sha256,
                    actor_id=actor_id,
                    previous_data_identity=(
                        previous_data_info.st_dev,
                        previous_data_info.st_ino,
                    ),
                    expected_previous_jwt_sha256=previous_jwt_sha256,
                    expected_previous_database_identity={
                        "role": destination.runtime_identity["role"],
                        "database": destination.runtime_identity["database"],
                        "serverAddress": destination.runtime_identity[
                            "server_address"
                        ],
                        "serverPort": destination.runtime_identity["server_port"],
                        "postmasterStartedAt": destination.runtime_identity[
                            "postmaster_started_at"
                        ],
                        "serverVersionNum": destination.runtime_identity[
                            "server_version_num"
                        ],
                        "databaseOid": destination.runtime_identity[
                            "database_oid"
                        ],
                        "databaseMarker": destination.runtime_identity[
                            "database_marker"
                        ],
                    },
                    target_data_identity=(
                        os.fstat(target.fileno()).st_dev,
                        os.fstat(target.fileno()).st_ino,
                    ),
                    expected_database_identity={
                        "role": identity["role"],
                        "database": identity["database"],
                        "serverAddress": identity["server_address"],
                        "serverPort": identity["server_port"],
                        "postmasterStartedAt": identity[
                            "postmaster_started_at"
                        ],
                        "serverVersionNum": identity["server_version_num"],
                        "databaseOid": identity["database_oid"],
                        "databaseMarker": identity["database_marker"],
                    },
                    pre_commit_guard=activation_commit_guard,
                )
            except BaseException:
                try:
                    visible_activation = self.activations.active()
                except BaseException:
                    cutover_guard_must_remain = True
                    raise
                if (
                    visible_activation is not None
                    and visible_activation.get("activationId")
                    == selected_activation_id
                ):
                    cutover_guard_must_remain = True
                    try:
                        self.abandon_prepared_activation(
                            selected_activation_id,
                            error_code="activation_publication_failed",
                        )
                    except BaseException:
                        raise
                    cutover_guard_must_remain = False
                    await cutover_guard.close()
                raise
            cutover_guard_must_remain = True
            try:
                await cutover_guard.verify_held()
            except BaseException:
                # Publication is already durable.  Never reopen writes until
                # the journal has durably selected the previous target again.
                try:
                    self.abandon_prepared_activation(
                        str(prepared.get("activationId", "")),
                        error_code="activation_cutover_guard_lost",
                    )
                except BaseException:
                    # Fail closed if the durable target selection cannot be
                    # reverted.  The self-retained guard intentionally stays
                    # alive until process exit or manual recovery.
                    cutover_guard_must_remain = True
                    raise
                cutover_guard_must_remain = False
                await cutover_guard.close()
                raise
            result = PreparedBackupActivation(
                prepared,
                cutover_guard=cutover_guard,
            )
            cutover_guard_transferred = True
            return result
        except BackupServiceError:
            raise
        except WriteBarrierTimeoutError as exc:
            raise BackupServiceError(
                409,
                "backup_activation_barrier_timeout",
                "Timed out waiting for active DB/file mutations to finish.",
            ) from exc
        except (WriteBarrierConnectionLostError, WriteBarrierError) as exc:
            raise BackupServiceError(
                503,
                "backup_activation_barrier_lost",
                "The activation was not accepted because its write barrier was lost.",
            ) from exc
        except ActivationError as exc:
            raise BackupServiceError(
                409,
                "backup_activation_state_invalid",
                str(exc),
            ) from exc
        except (BackupError, PostgresBackupError) as exc:
            raise self._map_error(exc, operation="activation") from exc
        finally:
            if (
                cutover_guard is not None
                and not cutover_guard_transferred
                and not cutover_guard_must_remain
            ):
                await cutover_guard.close()
            await target_engine.dispose()
            target.close()

    def inspect_activation(self, activation_id: str) -> dict[str, Any]:
        try:
            return self.activations.public_payload(
                self.activations.inspect(activation_id)
            )
        except ActivationError as exc:
            raise BackupServiceError(
                404,
                "backup_activation_not_found",
                "The requested backup activation was not found or is invalid.",
            ) from exc

    def authenticate_activation(self, activation_id: str, resume_token: str) -> bool:
        return self.activations.authenticate(activation_id, resume_token)

    def get_activation_restart_spec(
        self,
        activation_id: str,
    ) -> tuple[list[str], dict[str, str]]:
        try:
            return activation_restart_spec(
                self.activations.inspect(activation_id)
            )
        except ActivationError as exc:
            raise BackupServiceError(
                404,
                "backup_activation_not_found",
                "The requested backup activation was not found or is invalid.",
            ) from exc

    def abandon_prepared_activation(
        self,
        activation_id: str,
        *,
        error_code: str,
    ) -> dict[str, Any]:
        """Clear an activation that could not schedule its first restart."""
        try:
            self.activations.mark_rollback_pending(
                activation_id,
                error_code=error_code,
            )
            state = self.activations.mark_rolled_back(activation_id)
            return self.activations.public_payload(state)
        except ActivationError as exc:
            raise BackupServiceError(
                500,
                "backup_activation_cancel_failed",
                "The failed activation could not be cleared safely.",
            ) from exc

    async def restore_local_backup(
        self,
        backup_id: str,
        *,
        actor_id: str | None,
        operation_lease: BackupOperationLease | None = None,
    ) -> dict[str, Any]:
        """Run SDK disaster recovery under one retained mutation lease."""
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
        try:
            operation_lease.verify_attached()
            backup_id = await self._resolve_backup_reference(backup_id)
            with self.operations.backup_shared(backup_id) as backup_lease:
                planned = await self._create_staging_plan_under_lease(
                    backup_id,
                    jwt_secret_mode="disaster_recovery",
                    actor_id=actor_id,
                    backup_lease=backup_lease,
                )
                self._operation_commit_guard(operation_lease, backup_lease)
                plan_id = str(planned["id"])
                plan_hash = str(planned["planHash"])
                validated = await self._execute_staging_plan_under_lease(
                    plan_id,
                    expected_plan_hash=plan_hash,
                    operation_leases=(operation_lease, backup_lease),
                )
                if validated.get("status") != "validated":
                    raise BackupServiceError(
                        409,
                        "staging_plan_not_validated",
                        "Backup restore staging did not reach validated status.",
                    )
                self._operation_commit_guard(operation_lease, backup_lease)
                try:
                    validated_plan = self.plans.inspect(plan_id)
                except StagingPlanError as exc:
                    raise BackupServiceError(
                        409,
                        "staging_plan_not_activatable",
                        "The validated staging plan is no longer available.",
                    ) from exc
                return await self._activate_staging_plan_under_lease(
                    validated_plan,
                    expected_plan_hash=plan_hash,
                    actor_id=actor_id,
                    operation_leases=(operation_lease, backup_lease),
                )
        except BackupOperationError as exc:
            raise self._map_operation_error(exc) from exc

    async def _execute_staging_plan_under_lease(
        self,
        plan_id: str,
        *,
        expected_plan_hash: str,
        operation_leases: tuple[BackupOperationLease, ...] = (),
    ) -> dict[str, Any]:
        self._require_control_identity_attached()
        if operation_leases:
            self._operation_commit_guard(*operation_leases)
        try:
            plan = self.plans.begin_execution(
                plan_id,
                expected_plan_hash=expected_plan_hash,
            )
        except StagingPlanError as exc:
            raise BackupServiceError(
                409,
                "staging_plan_not_executable",
                str(exc),
            ) from exc
        attempt_id = str(plan.status_data.get("attemptId", ""))
        if not attempt_id:
            raise BackupServiceError(
                500,
                "staging_attempt_missing",
                "The durable staging execution attempt is missing.",
            )

        try:
            result = await self._execute_started_plan(plan)
            if operation_leases:
                self._operation_commit_guard(*operation_leases)
                terminal_guard: Callable[[], None] = lambda: (
                    self._operation_commit_guard(*operation_leases)
                )
            else:
                self._require_control_identity_attached()
                terminal_guard = self._require_control_identity_attached
            return self.plans.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
                data=result,
                pre_commit_guard=terminal_guard,
            ).as_dict()
        except BaseException as exc:
            failure_code = (
                "staging_cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else self._failure_code(exc)
            )
            quarantine_error: BaseException | None = None
            try:
                self.plans.finish(
                    plan.plan_id,
                    status="quarantined",
                    expected_attempt_id=attempt_id,
                    data={
                        "failureCode": failure_code,
                        "targetDatabase": plan.target_database,
                        "targetDataDir": plan.target_data_dir,
                    },
                )
            except Exception as persistence_exc:
                quarantine_error = persistence_exc
            if quarantine_error is not None:
                try:
                    self.plans.abandon_execution(
                        plan.plan_id,
                        expected_attempt_id=attempt_id,
                    )
                except Exception:
                    # The lease registry is popped before its descriptor is
                    # closed, so even a close error cannot leave a false
                    # in-process owner. Preserve the original operation result.
                    pass
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise BackupServiceError(
                    500,
                    "staging_quarantine_persistence_failed",
                    "Staging failed and its quarantine status could not be "
                    "persisted safely.",
                    {
                        "failureCode": failure_code,
                        "targetDatabase": plan.target_database,
                        "targetDataDir": plan.target_data_dir,
                    },
                ) from exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, BackupServiceError):
                raise
            if isinstance(exc, (BackupError, PostgresBackupError, StagingPlanError)):
                raise self._map_error(exc, operation="restore") from exc
            raise BackupServiceError(
                500,
                "staging_validation_failed",
                "Restore staging failed; its new targets were quarantined.",
                {
                    "targetDatabase": plan.target_database,
                    "targetDataDir": plan.target_data_dir,
                },
            ) from exc

    async def _execute_started_plan(self, plan: StagingPlan) -> dict[str, Any]:
        inspection = await self._trusted_inspection(plan.backup_id)
        self._require_plan_manifest(inspection, plan)
        contract = self._manifest_contract(inspection)
        destination = await self._preflight_restore_destination(
            inspection,
            contract,
        )
        if not secrets.compare_digest(
            destination.fingerprint_sha256,
            plan.destination_fingerprint_sha256,
        ):
            raise BackupServiceError(
                409,
                "staging_destination_changed",
                "The restore destination or PostgreSQL policy changed after "
                "this plan was sealed.",
            )
        restore_url = destination.restore_url
        target_owner = destination.target_owner
        restore_info = destination.restore_info

        staging_target = await _to_thread_quiescent(
            self.store.open_staging_data_dir,
            self.target_root,
            Path(plan.plan_id) / "data",
            cancel_cleanup=lambda target: target.close(),
        )
        if staging_target.path != Path(plan.target_data_dir):
            staging_target.close()
            raise BackupIntegrityError(
                "staging filesystem target no longer matches the sealed plan"
            )
        try:
            return await self._restore_into_anchored_target(
                plan,
                inspection,
                contract,
                destination,
                staging_target,
            )
        finally:
            staging_target.close()

    async def _restore_into_anchored_target(
        self,
        plan: StagingPlan,
        inspection: AuthenticatedBackupInspection,
        contract: DatabaseContract,
        destination: _RestoreDestinationPreflight,
        staging_target: AnchoredStagingDataDir,
    ) -> dict[str, Any]:
        creator_url = destination.creator_url
        restore_url = destination.restore_url
        target_owner = destination.target_owner
        restore_info = destination.restore_info

        await _to_thread_quiescent(staging_target.restore_files, inspection)
        jwt_mode = self._manifest_jwt_secret_mode(inspection)
        clone_rotation: dict[str, int] | None = None
        if plan.jwt_secret_mode == "disaster_recovery":
            if jwt_mode != "included_resource":
                raise BackupServiceError(
                    409,
                    "external_jwt_secret_unverifiable",
                    "Disaster recovery requires the signed JWT-secret resource; "
                    "an externally managed secret cannot yet be proven identical.",
                )
            await _to_thread_quiescent(staging_target.install_jwt, inspection)
        else:
            clone_secret = (generate_clone_jwt_secret() + "\n").encode("ascii")
            await _to_thread_quiescent(staging_target.write_secret, clone_secret)

        await _to_thread_quiescent(staging_target.verify_attached)
        created_database = await create_target_database(
            destination.creator_tool_url,
            plan.target_database,
            target_owner=target_owner,
            restore_role=restore_info.username,
            runtime_role=destination.runtime_info.username,
            dump_role=destination.dump_info.username,
            contract=contract,
            expected_server_identity=destination.creator_identity,
            psql=self.settings.backup_psql_path,
            passfile_factory=staging_target.temporary_file,
        )
        target_restore_url = replace_sqlalchemy_database(
            restore_url,
            plan.target_database,
        )
        target_dump_url = replace_sqlalchemy_database(
            self._dump_configuration(),
            plan.target_database,
        )
        target_runtime_url = replace_sqlalchemy_database(
            self.settings.database_url,
            plan.target_database,
        )
        target_restore_tool_url = replace_sqlalchemy_database(
            destination.restore_tool_url,
            plan.target_database,
        )
        target_marker = created_database.marker_comment
        target_engine = create_async_engine(target_restore_url, poolclass=NullPool)
        target_dump_engine = create_async_engine(target_dump_url, poolclass=NullPool)
        target_runtime_engine = create_async_engine(
            target_runtime_url,
            poolclass=NullPool,
        )
        migrations_applied: tuple[str, ...] = ()
        try:
            async with target_engine.connect() as connection:
                async with connection.begin():
                    guard_identity = await self._verify_target_guard_connection(
                        connection,
                        target_database=plan.target_database,
                        expected_restore_role=restore_info.username,
                        expected_server_identity=destination.restore_identity,
                        expected_marker=target_marker,
                    )
                    guard_backend_pid = int(guard_identity["backend_pid"])

                    inspection = await self._trusted_inspection(plan.backup_id)
                    self._require_plan_manifest(inspection, plan)
                    pinned_archive = await _to_thread_quiescent(
                        self.store.pin_database_dump,
                        inspection,
                        directory=staging_target,
                        cancel_cleanup=lambda archive: archive.close(),
                    )
                    try:
                        restore_task = asyncio.create_task(
                            run_pg_restore_from_fd(
                                target_restore_tool_url,
                                pinned_archive.fileno(),
                                archive_label=pinned_archive.source_path,
                                target_owner=target_owner,
                                pg_restore=self.settings.backup_pg_restore_path,
                                expected_server_major=contract.server_major,
                                passfile_factory=staging_target.temporary_file,
                            )
                        )
                        await self._await_restore_with_guard(
                            restore_task,
                            connection=connection,
                            target_database=plan.target_database,
                            expected_restore_role=restore_info.username,
                            expected_server_identity=destination.restore_identity,
                            expected_marker=target_marker,
                            expected_backend_pid=guard_backend_pid,
                        )
                    finally:
                        pinned_archive.close()

                    await self._verify_target_guard_connection(
                        connection,
                        target_database=plan.target_database,
                        expected_restore_role=restore_info.username,
                        expected_server_identity=destination.restore_identity,
                        expected_marker=target_marker,
                        expected_backend_pid=guard_backend_pid,
                    )
                    await _to_thread_quiescent(staging_target.verify_attached)

                    inspection = await self._trusted_inspection(plan.backup_id)
                    self._require_plan_manifest(inspection, plan)

                    await connection.execute(text(f'SET LOCAL ROLE "{target_owner}"'))
                    if plan.jwt_secret_mode == "clone":
                        rotation = await rotate_clone_database_secrets(connection)
                        clone_rotation = {
                            "authCollectionCount": rotation.auth_collection_count,
                            "authRecordCount": rotation.auth_record_count,
                            "collectionSecretCount": rotation.collection_secret_count,
                        }
                    await connection.execute(text("ANALYZE"))
                    validation = await validate_staged_database(
                        connection,
                        expected_database=plan.target_database,
                        expected_owner=target_owner,
                        expected_runtime_role=destination.runtime_info.username,
                        expected_dump_role=destination.dump_info.username,
                        expected_contract=contract,
                    )
                    validation.require_valid()
                    restored_file_references = (
                        await read_canonical_local_file_references(connection)
                    )
                    _require_manifest_file_reference_inventory(
                        inspection,
                        restored_file_references,
                    )
                    await _to_thread_quiescent(
                        staging_target.verify_local_file_references,
                        restored_file_references,
                    )
                    await self._validate_source_summary(
                        connection,
                        inspection,
                        validation.collection_count,
                        validation.migration_count,
                    )

                await self._verify_target_guard_connection(
                    connection,
                    target_database=plan.target_database,
                    expected_restore_role=restore_info.username,
                    expected_server_identity=destination.restore_identity,
                    expected_marker=target_marker,
                    expected_backend_pid=guard_backend_pid,
                )
                await connection.rollback()
                migrations_applied = await self._apply_missing_staged_migrations(
                    connection,
                    target_owner=target_owner,
                )

                async with connection.begin():
                    await self._verify_target_guard_connection(
                        connection,
                        target_database=plan.target_database,
                        expected_restore_role=restore_info.username,
                        expected_server_identity=destination.restore_identity,
                        expected_marker=target_marker,
                        expected_backend_pid=guard_backend_pid,
                    )
                    await _to_thread_quiescent(staging_target.verify_attached)
                    inspection = await self._trusted_inspection(plan.backup_id)
                    self._require_plan_manifest(inspection, plan)

                    await connection.execute(text(f'SET LOCAL ROLE "{target_owner}"'))
                    await grant_dump_role_read_access(
                        connection,
                        target_owner=target_owner,
                        dump_role=destination.dump_info.username,
                    )
                    await connection.execute(text("ANALYZE"))
                    validation = await validate_staged_database(
                        connection,
                        expected_database=plan.target_database,
                        expected_owner=target_owner,
                        expected_runtime_role=destination.runtime_info.username,
                        expected_dump_role=destination.dump_info.username,
                        expected_contract=contract,
                    )
                    validation.require_valid()
                    restored_file_references = (
                        await read_canonical_local_file_references(connection)
                    )
                    _require_manifest_file_reference_inventory(
                        inspection,
                        restored_file_references,
                    )
                    await _to_thread_quiescent(
                        staging_target.verify_local_file_references,
                        restored_file_references,
                    )
                await self._verify_target_guard_connection(
                    connection,
                    target_database=plan.target_database,
                    expected_restore_role=restore_info.username,
                    expected_server_identity=destination.restore_identity,
                    expected_marker=target_marker,
                    expected_backend_pid=guard_backend_pid,
                )
                async with target_runtime_engine.begin() as runtime_connection:
                    runtime_identity = await self._postgres_server_identity(
                        runtime_connection
                    )
                    if (
                        runtime_identity["role"] != destination.runtime_info.username
                        or runtime_identity["database"] != plan.target_database
                        or int(runtime_identity["database_oid"])
                        != int(guard_identity["database_oid"])
                        or not secrets.compare_digest(
                            str(runtime_identity.get("database_marker") or ""),
                            target_marker,
                        )
                        or self._server_instance_key(runtime_identity)
                        != self._server_instance_key(destination.runtime_identity)
                    ):
                        raise PostgresBackupError(
                            "retargeted runtime DSN does not pin the restored database"
                        )
                    await grant_dump_role_runtime_default_privileges(
                        runtime_connection,
                        runtime_role=destination.runtime_info.username,
                        dump_role=destination.dump_info.username,
                    )
                await self._verify_target_guard_connection(
                    connection,
                    target_database=plan.target_database,
                    expected_restore_role=restore_info.username,
                    expected_server_identity=destination.restore_identity,
                    expected_marker=target_marker,
                    expected_backend_pid=guard_backend_pid,
                )
                async with target_dump_engine.connect() as dump_connection:
                    dump_identity = await self._postgres_server_identity(dump_connection)
                    if (
                        dump_identity["role"] != destination.dump_info.username
                        or dump_identity["database"] != plan.target_database
                        or int(dump_identity["database_oid"])
                        != int(guard_identity["database_oid"])
                        or not secrets.compare_digest(
                            str(dump_identity.get("database_marker") or ""),
                            target_marker,
                        )
                        or self._server_instance_key(dump_identity)
                        != self._server_instance_key(destination.runtime_identity)
                    ):
                        raise PostgresBackupError(
                            "retargeted dump DSN does not pin the restored database"
                        )
                    dump_report = await preflight_dump_role(dump_connection)
                    dump_report.require_ok()
                    await dump_connection.rollback()
                await self._verify_target_guard_connection(
                    connection,
                    target_database=plan.target_database,
                    expected_restore_role=restore_info.username,
                    expected_server_identity=destination.restore_identity,
                    expected_marker=target_marker,
                    expected_backend_pid=guard_backend_pid,
                )
        finally:
            await target_runtime_engine.dispose()
            await target_dump_engine.dispose()
            await target_engine.dispose()

        await _to_thread_quiescent(staging_target.verify_attached)
        return {
            "targetDatabase": plan.target_database,
            "targetDataDir": plan.target_data_dir,
            "validation": validation.to_dict(),
            "jwtSecretMode": plan.jwt_secret_mode,
            "cloneRotation": clone_rotation,
            "migrationsApplied": list(migrations_applied),
            "activationPerformed": False,
        }

    async def _apply_missing_staged_migrations(
        self,
        connection: Any,
        *,
        target_owner: str,
    ) -> tuple[str, ...]:
        """Apply local pending migrations only on the isolated target session."""
        owner = validate_postgres_identifier(target_owner, label="target owner")
        migrations_dir = Path(self.settings.migrations_dir).expanduser()
        try:
            async with migration_lock_on_connection(
                connection,
                timeout_seconds=float(self.settings.migration_lock_timeout),
            ):
                role_selected = False
                body_error: BaseException | None = None
                try:
                    await connection.execute(text(f'SET ROLE "{owner}"'))
                    await connection.commit()
                    role_selected = True
                    applied = await apply_pending_on_connection(
                        connection,
                        migrations_dir,
                    )
                    async with AsyncSession(
                        bind=connection,
                        expire_on_commit=False,
                    ) as session:
                        async with session.begin():
                            remaining = await get_pending_migrations(
                                session,
                                migrations_dir,
                            )
                    if remaining:
                        raise PostgresBackupError(
                            "staged database still has pending migrations after application"
                        )
                    return tuple(applied)
                except BaseException as exc:
                    body_error = exc
                    raise
                finally:
                    if role_selected and not bool(getattr(connection, "closed", False)):
                        try:
                            if connection.in_transaction():
                                await connection.rollback()
                            await connection.execute(text("RESET ROLE"))
                            await connection.commit()
                        except BaseException as reset_error:
                            if body_error is None:
                                raise PostgresBackupError(
                                    "staged migration role could not be reset safely"
                                ) from reset_error
        except asyncio.CancelledError:
            raise
        except PostgresBackupError:
            raise
        except MigrationLockError as exc:
            raise PostgresBackupError(
                "staged database migration lock could not be acquired safely"
            ) from exc
        except Exception as exc:
            raise PostgresBackupError(
                "staged database migration application failed"
            ) from exc

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
            "backup_staging_root": self.staging_root,
            "backup_target_root": self.target_root,
        }
        resolved_roots = {
            label: root.resolve(strict=False)
            for label, root in named_roots.items()
        }
        for label, root in resolved_roots.items():
            if label == "backup_target_root":
                continue
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
        target_root = resolved_roots["backup_target_root"]
        if target_root == active_data_dir or target_root.is_relative_to(
            active_data_dir
        ):
            raise BackupServiceError(
                409,
                "unsafe_backup_root",
                "backup_target_root must contain isolated targets without being the active data_dir.",
            )
        if active_data_dir.is_relative_to(target_root):
            self._verify_active_target_is_journaled(active_data_dir)
        values = list(resolved_roots.items())
        for index, (left_name, left) in enumerate(values):
            for right_name, right in values[index + 1 :]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise BackupServiceError(
                        409,
                        "overlapping_backup_roots",
                        f"{left_name} and {right_name} must not overlap.",
                    )
        for label, root in (
            ("backup_staging_root", self.staging_root),
            ("backup_target_root", self.target_root),
        ):
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root_info = root.lstat()
            if not stat.S_ISDIR(root_info.st_mode) or root.is_symlink():
                raise BackupServiceError(
                    409,
                    (
                        "unsafe_staging_root"
                        if label == "backup_staging_root"
                        else "unsafe_target_root"
                    ),
                    f"{label} must be a private local directory.",
                )
            os.chmod(root, 0o700, follow_symlinks=False)
            if stat.S_IMODE(root.lstat().st_mode) != 0o700:
                raise BackupServiceError(
                    409,
                    (
                        "unsafe_staging_root"
                        if label == "backup_staging_root"
                        else "unsafe_target_root"
                    ),
                    f"{label} must have mode 0700.",
                )

    def _verify_active_target_is_journaled(self, active_data_dir: Path) -> None:
        control_root: ControlPlaneRoot | None = None
        store: BackupActivationStore | None = None
        try:
            control_root = ControlPlaneRoot.open(
                self.control_dir,
                create_missing=False,
            )
            store = BackupActivationStore(control_root)
            state = store.active()
            if state is None or state.get("selectedTarget") != "target":
                raise ActivationError("no active target is journaled")
            expected = Path(str(state.get("targetDataDir", ""))).expanduser().resolve(
                strict=True
            )
            if expected != active_data_dir.resolve(strict=True):
                raise ActivationError("active target path does not match the journal")
            info = expected.stat()
            expected_device = state.get("targetDataDevice")
            expected_inode = state.get("targetDataInode")
            if (
                isinstance(expected_device, bool)
                or isinstance(expected_inode, bool)
                or not isinstance(expected_device, int)
                or not isinstance(expected_inode, int)
                or (info.st_dev, info.st_ino) != (expected_device, expected_inode)
            ):
                raise ActivationError("active target inode does not match the journal")
        except (ActivationError, ControlPlaneSafetyError, OSError) as exc:
            raise BackupServiceError(
                409,
                "unsafe_backup_root",
                "The active data_dir under backup_target_root is not the exact durable journaled target.",
            ) from exc
        finally:
            if store is not None:
                store.close()
            if control_root is not None:
                control_root.close()

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

    @staticmethod
    def _server_instance_key(identity: dict[str, Any]) -> tuple[Any, ...]:
        return (
            identity["server_address"],
            identity["server_port"],
            identity["postmaster_started_at"],
            identity["server_version_num"],
        )

    @staticmethod
    async def _pin_restore_tool_url(
        database_url: str,
        info: LibpqConnectionInfo,
    ) -> tuple[URL, str | None]:
        """Resolve one direct tool endpoint and bind libpq with ``hostaddr``."""
        parsed = make_url(database_url)
        forbidden_query = {"hostaddr", "target_session_attrs"}.intersection(
            parsed.query
        )
        if forbidden_query:
            raise PostgresBackupError(
                "restore DSNs cannot override hostaddr or target_session_attrs"
            )
        host = str(info.host or "localhost")
        if "," in host or any(character.isspace() for character in host):
            raise PostgresBackupError(
                "restore DSNs must use one direct PostgreSQL endpoint"
            )
        if host.startswith("/"):
            return parsed, None

        try:
            address = ipaddress.ip_address(host).compressed
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                resolved = await loop.getaddrinfo(
                    host,
                    int(info.port),
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                )
                addresses = sorted({str(item[4][0]) for item in resolved})
                if not addresses:
                    resolved = await loop.getaddrinfo(
                        host,
                        int(info.port),
                        family=socket.AF_INET6,
                        type=socket.SOCK_STREAM,
                    )
                    addresses = sorted({str(item[4][0]) for item in resolved})
            except OSError as exc:
                raise PostgresBackupError(
                    "restore PostgreSQL endpoint could not be resolved"
                ) from exc
            if len(addresses) != 1:
                raise PostgresBackupError(
                    "restore DSN hostname must resolve to exactly one address"
                )
            address = ipaddress.ip_address(addresses[0]).compressed
        return parsed.update_query_dict({"hostaddr": address}), address

    async def _preflight_restore_destination(
        self,
        inspection: AuthenticatedBackupInspection,
        contract: DatabaseContract,
    ) -> _RestoreDestinationPreflight:
        creator_url, restore_url, target_owner = self._restore_configuration()
        creator_info = sqlalchemy_url_to_libpq(creator_url)
        restore_info = sqlalchemy_url_to_libpq(restore_url)
        runtime_info = sqlalchemy_url_to_libpq(self.settings.database_url)
        dump_info = sqlalchemy_url_to_libpq(self._dump_configuration())
        if dump_info.username in {
            creator_info.username,
            restore_info.username,
            runtime_info.username,
            target_owner,
        }:
            raise PostgresBackupError(
                "dump, creator, restore, runtime, and target owner roles must be distinct"
            )
        creator_tool_url, creator_hostaddr = await self._pin_restore_tool_url(
            creator_url,
            creator_info,
        )
        restore_tool_url, restore_hostaddr = await self._pin_restore_tool_url(
            restore_url,
            restore_info,
        )
        creator_tool_endpoint = (
            creator_hostaddr or creator_info.host,
            creator_info.port,
        )
        restore_tool_endpoint = (
            restore_hostaddr or restore_info.host,
            restore_info.port,
        )
        if creator_tool_endpoint != restore_tool_endpoint:
            raise PostgresBackupError(
                "creator and restore tools must use the same direct endpoint"
            )
        allowed_extensions = self._allowed_extensions()

        await inspect_pg_restore_archive(
            inspection.path / "resources" / "database.dump",
            pg_restore=self.settings.backup_pg_restore_path,
            expected_server_major=contract.server_major,
        )
        creator_engine = create_async_engine(creator_url, poolclass=NullPool)
        restore_engine = create_async_engine(restore_url, poolclass=NullPool)
        try:
            async with creator_engine.connect() as connection:
                creator_identity = await self._postgres_server_identity(connection)
                report = await preflight_database_contract(
                    connection,
                    contract,
                    creator_role=creator_info.username,
                    restore_role=restore_info.username,
                    runtime_role=runtime_info.username,
                    target_owner=target_owner,
                    allowed_extensions=allowed_extensions,
                )
                report.require_ok()
                await connection.rollback()
            async with self.engine.connect() as connection:
                runtime_identity = await self._postgres_server_identity(connection)
                if runtime_identity["role"] != runtime_info.username:
                    raise PostgresBackupError(
                        "runtime DSN login does not match current_user"
                    )
                if self._server_instance_key(
                    runtime_identity
                ) != self._server_instance_key(creator_identity):
                    raise PostgresBackupError(
                        "runtime and restore DSNs reached different PostgreSQL "
                        "server instances"
                    )
                await connection.rollback()
            async with restore_engine.connect() as connection:
                restore_identity = await self._postgres_server_identity(connection)
                if restore_identity["role"] != restore_info.username:
                    raise PostgresBackupError(
                        "restore DSN login does not match current_user"
                    )
                if self._server_instance_key(
                    restore_identity
                ) != self._server_instance_key(creator_identity):
                    raise PostgresBackupError(
                        "creator and restore DSNs reached different PostgreSQL "
                        "server instances"
                    )
                await connection.rollback()
        finally:
            await restore_engine.dispose()
            await creator_engine.dispose()

        self._require_direct_tool_endpoint(
            creator_hostaddr,
            creator_identity,
        )
        self._require_direct_tool_endpoint(
            restore_hostaddr,
            restore_identity,
        )

        binding = {
            "formatVersion": 1,
            "serverInstance": {
                "serverAddress": creator_identity["server_address"],
                "serverPort": creator_identity["server_port"],
                "postmasterStartedAt": creator_identity[
                    "postmaster_started_at"
                ],
                "serverVersionNum": creator_identity["server_version_num"],
            },
            "creator": {
                "configuredHost": creator_info.host,
                "configuredPort": creator_info.port,
                "toolHostAddress": creator_hostaddr,
                "role": creator_identity["role"],
                "database": creator_identity["database"],
            },
            "restore": {
                "configuredHost": restore_info.host,
                "configuredPort": restore_info.port,
                "toolHostAddress": restore_hostaddr,
                "role": restore_identity["role"],
                "database": restore_identity["database"],
            },
            "runtime": {
                "configuredHost": runtime_info.host,
                "configuredPort": runtime_info.port,
                "role": runtime_identity["role"],
                "database": runtime_identity["database"],
            },
            "dump": {
                "configuredHost": dump_info.host,
                "configuredPort": dump_info.port,
                "role": dump_info.username,
            },
            "targetOwner": target_owner,
            "allowedExtensions": [
                {"name": name, "version": version}
                for name, version in sorted(allowed_extensions.items())
            ],
        }
        fingerprint = hashlib.sha256(
            _DESTINATION_DOMAIN + canonical_json_bytes(binding)
        ).hexdigest()
        return _RestoreDestinationPreflight(
            creator_url=creator_url,
            restore_url=restore_url,
            creator_tool_url=creator_tool_url,
            restore_tool_url=restore_tool_url,
            target_owner=target_owner,
            creator_info=creator_info,
            restore_info=restore_info,
            runtime_info=runtime_info,
            dump_info=dump_info,
            creator_identity=creator_identity,
            restore_identity=restore_identity,
            runtime_identity=runtime_identity,
            creator_hostaddr=creator_hostaddr,
            restore_hostaddr=restore_hostaddr,
            fingerprint_sha256=fingerprint,
            warnings=tuple(report.warnings),
        )

    @staticmethod
    def _require_direct_tool_endpoint(
        hostaddr: str | None,
        identity: dict[str, Any],
    ) -> None:
        if hostaddr is None:
            return
        configured = ipaddress.ip_address(hostaddr)
        if configured.is_loopback:
            # Loopback is accepted for a local direct tunnel/NAT such as the
            # supported Docker test deployment. The hostname still resolves to
            # one address and both tool roles must share it.
            return
        observed_raw = str(identity.get("server_address", "") or "")
        try:
            observed = ipaddress.ip_address(observed_raw)
        except ValueError as exc:
            raise PostgresBackupError(
                "restore endpoint could not be proven direct"
            ) from exc
        if configured != observed:
            raise PostgresBackupError(
                "restore endpoint appears proxied or load-balanced; a direct "
                "PostgreSQL endpoint is required"
            )

    async def _verify_target_guard_connection(
        self,
        connection: Any,
        *,
        target_database: str,
        expected_restore_role: str,
        expected_server_identity: dict[str, Any],
        expected_marker: str,
        expected_backend_pid: int | None = None,
    ) -> dict[str, Any]:
        """Verify that the still-open restore guard pins the exact target."""
        target = validate_postgres_identifier(
            target_database,
            label="target database",
        )
        expected_restore = validate_postgres_identifier(
            expected_restore_role,
            label="restore role",
        )
        if bool(getattr(connection, "closed", False)) or bool(
            getattr(connection, "invalidated", False)
        ):
            raise PostgresBackupError("restore target guard connection was lost")
        try:
            restore_identity = await self._postgres_server_identity(connection)
            observed = await self._read_target_database_marker(connection)
        except PostgresBackupError:
            raise
        except Exception:
            raise PostgresBackupError(
                "restore target guard connection was lost or became unverifiable"
            ) from None
        if (
            restore_identity["role"] != expected_restore
            or restore_identity["database"] != target
        ):
            raise PostgresBackupError(
                "restore target guard reached an unexpected role or database"
            )
        if self._server_instance_key(
            restore_identity
        ) != self._server_instance_key(expected_server_identity):
            raise PostgresBackupError(
                "restore target guard reached a different PostgreSQL server instance"
            )
        if expected_backend_pid is not None and int(
            restore_identity["backend_pid"]
        ) != int(expected_backend_pid):
            raise PostgresBackupError(
                "restore target guard PostgreSQL session changed during restore"
            )
        if not secrets.compare_digest(observed, expected_marker):
            raise PostgresBackupError(
                "restore target marker changed while the guard was held"
            )
        return restore_identity

    async def _await_restore_with_guard(
        self,
        restore_task: asyncio.Task[_T],
        *,
        connection: Any,
        target_database: str,
        expected_restore_role: str,
        expected_server_identity: dict[str, Any],
        expected_marker: str,
        expected_backend_pid: int,
    ) -> _T:
        """Supervise pg_restore and stop it before surfacing guard loss."""
        try:
            while True:
                done, _ = await asyncio.wait(
                    (restore_task,),
                    timeout=_RESTORE_GUARD_POLL_SECONDS,
                )
                if restore_task in done:
                    return restore_task.result()
                try:
                    await asyncio.wait_for(
                        self._verify_target_guard_connection(
                            connection,
                            target_database=target_database,
                            expected_restore_role=expected_restore_role,
                            expected_server_identity=expected_server_identity,
                            expected_marker=expected_marker,
                            expected_backend_pid=expected_backend_pid,
                        ),
                        timeout=_RESTORE_GUARD_VERIFY_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    raise PostgresBackupError(
                        "restore target guard verification timed out"
                    ) from None
        except BaseException:
            await _cancel_task_quiescent(restore_task)
            raise

    @staticmethod
    async def _read_target_database_marker(connection: Any) -> str:
        await set_backup_control_search_path(connection)
        value = (
            await connection.execute(
                text(
                    "SELECT pg_catalog.shobj_description(d.oid, 'pg_database') "
                    "FROM pg_catalog.pg_database AS d "
                    "WHERE d.datname = pg_catalog.current_database()"
                )
            )
        ).scalar_one()
        return str(value or "")

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

    @staticmethod
    async def _validate_source_summary(
        connection: Any,
        inspection: AuthenticatedBackupInspection,
        collection_count: int,
        migration_count: int,
    ) -> None:
        source_summary = inspection.manifest.metadata.get("database_summary")
        if not isinstance(source_summary, dict):
            return
        if int(source_summary.get("collections", -1)) != collection_count:
            raise BackupIntegrityError(
                "restored collection count differs from the manifest"
            )
        if int(source_summary.get("migrations", -1)) != migration_count:
            raise BackupIntegrityError(
                "restored migration count differs from the manifest"
            )
        restored_superusers = int(
            (
                await connection.execute(
                    text('SELECT count(*) FROM public."_superusers"')
                )
            ).scalar_one()
        )
        if int(source_summary.get("superusers", -1)) != restored_superusers:
            raise BackupIntegrityError(
                "restored superuser count differs from the manifest"
            )

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
    def _manifest_jwt_secret_mode(
        inspection: BackupInspection | AuthenticatedBackupInspection,
    ) -> str:
        raw = inspection.manifest.metadata.get("jwt_secret")
        mode = raw.get("mode") if isinstance(raw, dict) else None
        resource_paths = {resource.path for resource in inspection.manifest.resources}
        has_resource = JWT_SECRET_RESOURCE in resource_paths
        if mode == "included_resource" and has_resource:
            return mode
        if mode == "external_required" and not has_resource:
            return mode
        raise BackupIntegrityError(
            "manifest JWT-secret metadata and signed resources are inconsistent"
        )

    @staticmethod
    def _require_plan_manifest(
        inspection: AuthenticatedBackupInspection,
        plan: StagingPlan,
    ) -> None:
        manifest_sha256 = hashlib.sha256(
            inspection.manifest.to_bytes()
        ).hexdigest()
        if manifest_sha256 != plan.manifest_sha256:
            raise BackupIntegrityError(
                "staging plan no longer matches the signed backup manifest"
            )

    def _restore_configuration(self) -> tuple[str, str, str]:
        creator = str(self.settings.backup_creator_database_url or "").strip()
        restore = str(self.settings.backup_restore_database_url or "").strip()
        owner = str(self.settings.backup_target_owner or "").strip()
        if not creator or not restore or not owner:
            raise BackupServiceError(
                409,
                "restore_roles_not_configured",
                "Restore staging requires separate creator/restore DSNs and a target owner.",
            )
        try:
            creator_info = sqlalchemy_url_to_libpq(creator)
            restore_info = sqlalchemy_url_to_libpq(restore)
        except PostgresBackupError as exc:
            raise BackupServiceError(
                409,
                "restore_database_url_invalid",
                "A configured restore PostgreSQL DSN is invalid.",
            ) from exc
        if creator_info.username == restore_info.username:
            raise BackupServiceError(
                409,
                "restore_roles_not_separate",
                "Creator and restore PostgreSQL login roles must be distinct.",
            )
        return creator, restore, owner

    def _dump_configuration(self) -> str:
        dump_url = str(self.settings.backup_dump_database_url or "").strip()
        if not dump_url:
            raise BackupServiceError(
                409,
                "dump_role_not_configured",
                "Backup creation requires a dedicated read-only pg_dump DSN.",
            )
        return dump_url

    def _allowed_extensions(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in getattr(self.settings, "backup_allowed_extensions", ()) or ():
            name, separator, version = str(item).partition("=")
            if not separator or not name or not version or name in result:
                raise BackupServiceError(
                    500,
                    "invalid_extension_allowlist",
                    "The backup extension allowlist must contain unique name=version entries.",
                )
            result[name] = version
        return result

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
                "The signed backup failed integrity verification.",
            )
        if isinstance(exc, StagingPlanError):
            return BackupServiceError(409, "staging_plan_invalid", str(exc))
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

    @staticmethod
    def _failure_code(exc: BaseException) -> str:
        if isinstance(exc, BackupServiceError):
            return exc.code
        if isinstance(exc, BackupIntegrityError):
            return "backup_integrity_failed"
        if isinstance(exc, PostgresBackupError):
            return "postgres_contract_failed"
        if isinstance(exc, BackupError):
            return "backup_store_failed"
        return "staging_validation_failed"
