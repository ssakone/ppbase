"""Application orchestration for local backup and restore staging only."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import stat
import socket
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import secrets
from typing import Any, Callable, TypeVar

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ppbase import __version__
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
    BackupNotFoundError,
    canonical_json_bytes,
)
from ppbase.backup.plans import StagingPlan, StagingPlanError, StagingPlanStore
from ppbase.backup.postgres import (
    DatabaseContract,
    LibpqConnectionInfo,
    PostgresBackupError,
    create_target_database,
    detect_postgres_versions,
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
    BackupSealCancelledError,
    BackupSealGate,
    LocalBackupStore,
)
from ppbase.backup.validation import (
    generate_clone_jwt_secret,
    rotate_clone_database_secrets,
    validate_staged_database,
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
from ppbase.services.write_barrier import backup_write_barrier


_DESTINATION_DOMAIN = b"PPBASE-RESTORE-DESTINATION-V1\0"
_FILE_REFERENCE_INVENTORY_DOMAIN = b"PPBASE-LOCAL-FILE-REFERENCES-V1\0"
_FILE_REFERENCE_INVENTORY_KEY = "local_file_reference_inventory"
_FILE_REFERENCE_INVENTORY_VERSION = 1
_RESTORE_GUARD_POLL_SECONDS = 0.1
_RESTORE_GUARD_VERIFY_TIMEOUT_SECONDS = 2.0
_T = TypeVar("_T")


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
    creator_identity: dict[str, Any]
    restore_identity: dict[str, Any]
    creator_hostaddr: str | None
    restore_hostaddr: str | None
    fingerprint_sha256: str
    warnings: tuple[str, ...]


async def _to_thread_quiescent(
    function: Callable[..., _T],
    /,
    *args: Any,
    cancel_cleanup: Callable[[_T], None] | None = None,
    **kwargs: Any,
) -> _T:
    """Wait for a blocking worker to stop before propagating cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
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
                cancel_cleanup(completed)
        raise


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
        sealing_prevented = seal_gate.cancel()
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
            self.plans = StagingPlanStore(self.control_root, self.staging_root)
            self.identity = self._load_identity()
            self.store = LocalBackupStore(
                self.backup_root,
                identity=self.identity,
            )
        except BackupServiceError:
            self.close()
            raise
        except StagingPlanError as exc:
            self.close()
            raise BackupServiceError(
                500,
                "backup_control_invalid",
                "The native backup control plane is missing or unsafe.",
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
        plans = getattr(self, "plans", None)
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
                    if plans is not None:
                        plans.close()
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

    async def create_local_backup(
        self,
        *,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_control_identity_attached()
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
        if not str(getattr(self.settings, "jwt_secret", "") or ""):
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
        preflight_warnings: list[str] = []
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
                    prepared = await _to_thread_quiescent(builder.prepare)
                    await _to_thread_quiescent(
                        _require_copied_file_references,
                        source_file_references,
                        prepared,
                        storage_config,
                    )
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
        ):  # pragma: no cover
            raise BackupServiceError(
                500,
                "backup_creation_failed",
                "Backup preparation did not produce its signed metadata.",
            )
        metadata = {
            "ppbase_version": __version__,
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
                    if local_secret_path is not None
                    else "external_required"
                )
            },
            "created_by": actor_id,
            "preflight_warnings": preflight_warnings,
        }
        try:
            inspection = await _finalize_backup_atomically(
                self.store.finalize_set,
                prepared,
                seal_gate=BackupSealGate(),
                metadata=metadata,
                identity_guard=self._require_control_identity_attached,
            )
        except BackupError as exc:
            raise self._map_error(exc, operation="seal") from exc
        return self._inspection_dict(inspection)

    async def list_local_backups(self) -> list[dict[str, Any]]:
        self._require_control_identity_attached()
        try:
            summaries = await _to_thread_quiescent(self.store.list_sets)
            self._require_control_identity_attached()
            return [
                {
                    "id": item.backup_id,
                    "createdAt": item.created_at,
                    "signerFingerprintSha256": item.signer_fingerprint_sha256,
                    "resourceCount": item.resource_count,
                    "totalSize": item.total_size,
                    "status": (
                        "sealed"
                        if item.integrity_status == "valid"
                        else "invalid"
                    ),
                    "integrityStatus": item.integrity_status,
                    "errorCode": item.error_code,
                }
                for item in summaries
            ]
        except BackupError as exc:
            raise self._map_error(exc, operation="list") from exc

    async def inspect_local_backup(self, backup_id: str) -> dict[str, Any]:
        self._require_control_identity_attached()
        try:
            inspection = await asyncio.to_thread(
                self.store.inspect_set,
                backup_id,
                expected_public_key=self.identity.public_key_bytes,
                verify_resources=True,
            )
            self._require_control_identity_attached()
        except BackupError as exc:
            raise self._map_error(exc, operation="inspect") from exc
        return self._inspection_dict(inspection)

    async def create_staging_plan(
        self,
        backup_id: str,
        *,
        jwt_secret_mode: str,
        actor_id: str | None,
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
                pre_commit_guard=self._require_control_identity_attached,
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

    async def execute_staging_plan(
        self,
        plan_id: str,
        *,
        expected_plan_hash: str,
    ) -> dict[str, Any]:
        self._require_control_identity_attached()
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
            self._require_control_identity_attached()
            return self.plans.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
                data=result,
                pre_commit_guard=self._require_control_identity_attached,
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
            self.staging_root,
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
            contract=contract,
            expected_server_identity=destination.creator_identity,
            psql=self.settings.backup_psql_path,
            passfile_factory=staging_target.temporary_file,
        )
        target_restore_url = replace_sqlalchemy_database(
            restore_url,
            plan.target_database,
        )
        target_restore_tool_url = replace_sqlalchemy_database(
            destination.restore_tool_url,
            plan.target_database,
        )
        target_marker = created_database.marker_comment
        target_engine = create_async_engine(target_restore_url, poolclass=NullPool)
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
                    await connection.execute(
                        text(
                            f'COMMENT ON DATABASE "{plan.target_database}" IS NULL'
                        )
                    )
        finally:
            await target_engine.dispose()

        await _to_thread_quiescent(staging_target.verify_attached)
        return {
            "targetDatabase": plan.target_database,
            "targetDataDir": plan.target_data_dir,
            "validation": validation.to_dict(),
            "jwtSecretMode": plan.jwt_secret_mode,
            "cloneRotation": clone_rotation,
            "activationPerformed": False,
        }

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

    def _validate_roots(self) -> None:
        active_data_dir = Path(self.settings.data_dir).expanduser().resolve(
            strict=False
        )
        named_roots = {
            "backup_root": self.backup_root,
            "backup_control_dir": self.control_dir,
            "backup_staging_root": self.staging_root,
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
        self.staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging_info = self.staging_root.lstat()
        if (
            not stat.S_ISDIR(staging_info.st_mode)
            or self.staging_root.is_symlink()
        ):
            raise BackupServiceError(
                409,
                "unsafe_staging_root",
                "backup_staging_root must be a private local directory.",
            )
        os.chmod(self.staging_root, 0o700, follow_symlinks=False)

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
                    "pg_catalog.pg_backend_pid() AS backend_pid"
                )
            )
        ).mappings().one()
        return {
            "role": str(row["role"]),
            "database": str(row["database"]),
            "server_address": str(row["server_address"]),
            "server_port": int(row["server_port"]),
            "postmaster_started_at": str(row["postmaster_started_at"]),
            "server_version_num": str(row["server_version_num"]),
            "backend_pid": int(row["backend_pid"]),
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
                    target_owner=target_owner,
                    allowed_extensions=allowed_extensions,
                )
                report.require_ok()
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
            creator_identity=creator_identity,
            restore_identity=restore_identity,
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
            inspection = await asyncio.to_thread(
                self.store.authenticate_set,
                backup_id,
                approved_public_key=self.identity.public_key_bytes,
            )
            self._require_control_identity_attached()
            return inspection
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

    def _inspection_dict(self, inspection: BackupInspection) -> dict[str, Any]:
        manifest = inspection.manifest
        signer = base64.urlsafe_b64encode(
            inspection.signer_public_key
        ).decode("ascii").rstrip("=")
        return {
            "id": manifest.backup_id,
            "createdAt": manifest.created_at,
            "status": "sealed",
            "authenticated": True,
            "trustStatus": "trusted_local",
            "signerFingerprintSha256": manifest.signer_fingerprint_sha256,
            "signerPublicKey": signer,
            "resourcesVerified": inspection.resources_verified,
            "resourceCount": len(manifest.resources),
            "totalSize": manifest.total_size,
            "metadata": dict(manifest.metadata),
            "resources": [
                {
                    "path": resource.path,
                    "size": resource.size,
                    "sha256": resource.sha256,
                }
                for resource in manifest.resources
            ],
        }

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
