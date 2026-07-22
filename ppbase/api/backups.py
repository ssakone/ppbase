"""Superuser API for native backup, transport, restore, and activation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response, StreamingResponse
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from ppbase.api.deps import get_optional_auth, get_session, require_admin
from ppbase.backup.activation import (
    ActivationError,
    BackupActivationStore,
    settle_failed_activation_restart,
)
from ppbase.backup.control import ControlPlaneRoot, ControlPlaneSafetyError
from ppbase.backup.service import (
    BACKUP_INSPECTION_DEFAULT_RESOURCE_LIMIT,
    BACKUP_INSPECTION_MAX_RESOURCE_LIMIT,
    BackupServiceError,
    NativeBackupService,
)
from ppbase.backup.transport import (
    PinnedBackupZip,
    validate_backup_transport_filename,
)
from ppbase.db.engine import get_engine
from ppbase.db.engine import get_async_session
from ppbase.services.async_utils import to_thread_quiescent
from ppbase.services.file_tokens import verify_file_token
from ppbase.services.process_control import can_self_restart, schedule_process_restart


logger = logging.getLogger(__name__)
_MAX_CONTROL_JSON_BYTES = 32 * 1024
_MAX_MULTIPART_FIELD_BYTES = 64 * 1024
_SAFE_DOWNLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.zip$")
_RESTORE_COORDINATOR_STATE = "native_backup_restore_coordinator"
_FAILED_SDK_CUTOVER_CONTEXTS: list[Any] = []


class _AppOwnedBackupRestoreCoordinator:
    """Own exactly one SDK restore task and all resources retained by it."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def active(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    async def start(
        self,
        request: Request,
        backup_id: str,
        *,
        actor_id: str | None,
    ) -> None:
        async with self._lock:
            if self._closing:
                raise BackupServiceError(
                    503,
                    "backup_restore_service_stopping",
                    "PPBase is stopping and cannot start another restore.",
                )
            if self.active:
                raise BackupServiceError(
                    409,
                    "backup_restore_in_progress",
                    "Another SDK backup restore is already in progress.",
                )

            service: NativeBackupService | None = None
            operation_context: Any = None
            operation_entered = False
            runner = None
            try:
                service = _service(request)
                operation_context = service.mutation_operation()
                operation_lease = operation_context.__enter__()
                operation_entered = True
                backup_id = await service.resolve_restore_reference(
                    backup_id,
                    operation_lease=operation_lease,
                )
                runner = self._run(
                    service,
                    operation_context,
                    operation_lease,
                    backup_id,
                    actor_id=actor_id,
                )
                task = asyncio.create_task(
                    runner,
                    name="ppbase-native-backup-sdk-restore",
                )
            except BaseException:
                if runner is not None:
                    runner.close()
                if operation_entered:
                    try:
                        operation_context.__exit__(None, None, None)
                    except Exception:
                        logger.exception(
                            "Failed to release an unstarted SDK restore lease"
                        )
                if service is not None:
                    service.close()
                raise

            self._task = task
            task.add_done_callback(self._task_done)

    async def _run(
        self,
        service: NativeBackupService,
        operation_context: Any,
        operation_lease: Any,
        backup_id: str,
        *,
        actor_id: str | None,
    ) -> None:
        operation_context_transferred = False
        try:
            prepared = await service.restore_local_backup(
                backup_id,
                actor_id=actor_id,
                operation_lease=operation_lease,
            )
            try:
                cutover_guard = _prepared_cutover_guard(prepared)
            except BaseException:
                try:
                    service.abandon_prepared_activation(
                        str(prepared.get("activationId", "")),
                        error_code="activation_guard_missing",
                    )
                except BaseException:
                    _FAILED_SDK_CUTOVER_CONTEXTS.append(operation_context)
                    operation_context_transferred = True
                    raise
                raise
            try:
                cutover_guard.retain_operation_context(operation_context)
            except BaseException:
                try:
                    service.abandon_prepared_activation(
                        str(prepared.get("activationId", "")),
                        error_code="activation_guard_transfer_failed",
                    )
                except BaseException:
                    _FAILED_SDK_CUTOVER_CONTEXTS.append(operation_context)
                    operation_context_transferred = True
                    raise
                await cutover_guard.close()
                raise
            operation_context_transferred = True
            await _schedule_backup_activation(service, prepared)
        except asyncio.CancelledError:
            raise
        except BackupServiceError as exc:
            logger.error("SDK native backup restore failed safely: %s", exc.code)
        except Exception:
            logger.exception("SDK native backup restore failed unexpectedly")
        finally:
            if not operation_context_transferred:
                try:
                    operation_context.__exit__(None, None, None)
                except Exception:
                    logger.exception(
                        "Failed to release the SDK restore operation lease"
                    )
            service.close()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        if self._task is task:
            self._task = None

    async def wait_idle(self) -> None:
        task = self._task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
            task = self._task
            if task is not None and not task.done():
                task.cancel()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass


def _restore_coordinator(app: Any) -> _AppOwnedBackupRestoreCoordinator:
    coordinator = getattr(app.state, _RESTORE_COORDINATOR_STATE, None)
    if coordinator is None:
        coordinator = _AppOwnedBackupRestoreCoordinator()
        setattr(app.state, _RESTORE_COORDINATOR_STATE, coordinator)
    if not isinstance(coordinator, _AppOwnedBackupRestoreCoordinator):
        raise BackupServiceError(
            500,
            "backup_restore_coordinator_invalid",
            "The application backup restore coordinator is invalid.",
        )
    return coordinator


@asynccontextmanager
async def _backup_router_lifespan(app: Any):
    try:
        yield
    finally:
        coordinator = getattr(app.state, _RESTORE_COORDINATOR_STATE, None)
        if isinstance(coordinator, _AppOwnedBackupRestoreCoordinator):
            await coordinator.close()


router = APIRouter(
    prefix="/backups",
    tags=["backups"],
    lifespan=_backup_router_lifespan,
)
staging_router = APIRouter(prefix="/backup-staging", tags=["backup-staging"])
activation_router = APIRouter(
    prefix="/backup-activations",
    tags=["backup-activations"],
)


class _PinnedBackupStreamingResponse(StreamingResponse):
    """Release the pinned ZIP even when ASGI streaming is cancelled."""

    def __init__(self, pinned: PinnedBackupZip, *args: Any, **kwargs: Any) -> None:
        self._pinned = pinned
        super().__init__(*args, **kwargs)

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        try:
            await super().__call__(*args, **kwargs)
        finally:
            self._pinned.close()


class StagingPlanCreateBody(BaseModel):
    jwt_secret_mode: str = Field(alias="jwtSecretMode")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class BackupCreateBody(BaseModel):
    name: str | None = None

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        try:
            return validate_backup_transport_filename(value)
        except ValueError as exc:
            raise ValueError(
                "name must be a safe .zip basename without path components"
            ) from exc


class StagingExecuteBody(BaseModel):
    plan_hash: str = Field(alias="planHash", min_length=64, max_length=64)


class StagingActivateBody(StagingExecuteBody):
    activation_id: str | None = Field(
        default=None,
        alias="activationId",
        pattern=r"^[a-f0-9]{32}$",
    )
    resume_token: str | None = Field(
        default=None,
        alias="resumeToken",
        min_length=32,
        max_length=512,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @model_validator(mode="after")
    def require_complete_resume_credentials(self) -> "StagingActivateBody":
        if (self.activation_id is None) != (self.resume_token is None):
            raise ValueError(
                "activationId and resumeToken must either both be supplied or "
                "both be omitted"
            )
        return self

    model_config = {"populate_by_name": True, "extra": "forbid"}


class BackupTrustApproveBody(BaseModel):
    fingerprint_sha256: str = Field(
        alias="fingerprintSha256",
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}


def _backup_readiness(
    settings: Any,
    *,
    warnings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return non-secret deployment readiness for Dashboard guidance."""
    storage_backend = str(
        getattr(settings, "storage_backend", "local") or "local"
    ).strip().lower()
    create_missing: list[str] = []
    if not str(
        getattr(settings, "backup_dump_database_url", "") or ""
    ).strip():
        create_missing.append("PPBASE_BACKUP_DUMP_DATABASE_URL")
    if storage_backend != "local":
        create_missing.append("local business-file storage backend")

    restore_missing: list[str] = []
    for attribute, label in (
        (
            "backup_creator_database_url",
            "PPBASE_BACKUP_CREATOR_DATABASE_URL",
        ),
        (
            "backup_restore_database_url",
            "PPBASE_BACKUP_RESTORE_DATABASE_URL",
        ),
        ("backup_target_owner", "PPBASE_BACKUP_TARGET_OWNER"),
    ):
        if not str(getattr(settings, attribute, "") or "").strip():
            restore_missing.append(label)

    restart_ready = can_self_restart()
    tool_missing = [
        label
        for attribute, label in (
            ("backup_pg_dump_path", "pg_dump"),
            ("backup_pg_restore_path", "pg_restore"),
            ("backup_psql_path", "psql"),
        )
        if shutil.which(str(getattr(settings, attribute, "") or "")) is None
    ]
    storage_missing = [] if storage_backend == "local" else [
        "local business-file storage backend"
    ]
    staging_value = str(getattr(settings, "backup_staging_root", "") or "")
    target_value = str(
        getattr(settings, "backup_target_root", "") or f"{staging_value}_targets"
    )
    control_missing: list[str] = []
    for value, label in (
        (getattr(settings, "backup_control_dir", ""), "PPBASE_BACKUP_CONTROL_DIR"),
        (staging_value, "PPBASE_BACKUP_STAGING_ROOT"),
        (target_value, "PPBASE_BACKUP_TARGET_ROOT"),
    ):
        path = Path(str(value)).expanduser()
        if path.exists() and (not path.is_dir() or path.is_symlink()):
            control_missing.append(f"safe directory: {label}")
    return {
        "create": {
            "configured": not create_missing,
            "missing": create_missing,
        },
        "restore": {
            "configured": not restore_missing,
            "missing": restore_missing,
        },
        "activation": {
            "configured": restart_ready,
            "missing": [] if restart_ready else ["PPBASE_RESTART_CMD"],
        },
        "postgresqlTools": {
            "configured": not tool_missing,
            "missing": tool_missing,
        },
        "storage": {
            "configured": not storage_missing,
            "missing": storage_missing,
        },
        "controlPlane": {
            "configured": not control_missing,
            "missing": control_missing,
        },
        "storageBackend": storage_backend,
        "warnings": list(warnings or []),
        "onboarding": {
            "recommended": "production",
            "productionCommand": "ppbase backup provision --plan",
            "localCommand": "ppbase backup provision --plan --local",
            "doctorCommand": "ppbase backup doctor",
        },
    }


async def _runtime_readiness_warnings(
    session: AsyncSession,
) -> list[dict[str, str]]:
    """Report non-blocking security compatibility for the live runtime role."""
    is_superuser = bool(
        await session.scalar(
            text(
                "SELECT runtime_role.rolsuper "
                "FROM pg_catalog.pg_roles AS runtime_role "
                "WHERE runtime_role.rolname = SESSION_USER"
            )
        )
    )
    if not is_superuser:
        return []
    return [
        {
            "code": "legacy_runtime_superuser",
            "name": "runtime_role",
            "detail": "PostgreSQL superuser runtime",
        }
    ]


async def _require_backup_admin(
    admin: dict[str, Any] = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Authenticate first, then return the read connection to a size-1 pool."""
    await session.rollback()
    return admin


async def _authorize_backup_read(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Select tokenized download or ordinary superuser inspection auth."""
    if "token" in request.query_params:
        token = str(request.query_params.get("token", "") or "").strip()
        token_auth = await verify_file_token(session, token) if token else None
        is_superuser = token_auth is not None and (
            token_auth.get("type") == "admin"
            or (
                token_auth.get("type") == "authRecord"
                and token_auth.get("collectionName") == "_superusers"
            )
        )
        if not is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "status": status.HTTP_403_FORBIDDEN,
                    "message": "Insufficient permissions to access the resource.",
                    "data": {},
                },
            )
        await session.rollback()
        return {"mode": "download", "auth": token_auth}

    auth = await get_optional_auth(request, session)
    admin = await require_admin(auth)
    await session.rollback()
    return {"mode": "inspect", "auth": admin}


def _service(request: Request) -> NativeBackupService:
    return NativeBackupService(get_engine(), request.app.state.settings)


def _actor_id(admin: dict[str, Any]) -> str | None:
    value = admin.get("id")
    return str(value) if value else None


def _raise_backup_error(exc: BackupServiceError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={
            "status": exc.status_code,
            "message": exc.message,
            "data": {"code": exc.code, **exc.data},
        },
    ) from exc


async def _require_activation_admin(request: Request) -> dict[str, Any]:
    """Authenticate an admin only after a scoped resume token was rejected."""
    async for session in get_async_session():
        auth = await get_optional_auth(request, session)
        admin = await require_admin(auth)
        await session.rollback()
        return admin
    raise HTTPException(status_code=503, detail="Database session unavailable")


def _prepared_cutover_guard(prepared: dict[str, Any]) -> Any:
    guard = getattr(prepared, "cutover_guard", None)
    if (
        guard is None
        or not callable(getattr(guard, "retain_operation_context", None))
        or not callable(getattr(guard, "close", None))
        or not callable(getattr(guard, "close_from_restart_thread", None))
        or not callable(getattr(guard, "verify_from_restart_thread", None))
        or getattr(guard, "restart_reservation", None) is None
    ):
        raise BackupServiceError(
            500,
            "backup_activation_guard_missing",
            "The accepted activation did not retain its cutover guards.",
        )
    return guard


async def _schedule_backup_activation(
    service: NativeBackupService,
    prepared: dict[str, Any],
) -> None:
    activation_id = str(prepared.get("activationId", ""))
    cutover_guard = _prepared_cutover_guard(prepared)
    control_root = getattr(service, "control_root", None)
    control_dir = getattr(control_root, "path", None)

    def persist_exec_failure(_exc: BaseException) -> None:
        if control_dir is None:
            logger.critical(
                "Accepted backup activation restart failed without a durable control path"
            )
            return
        try:
            settle_failed_activation_restart(
                control_dir,
                activation_id,
                error_code="restart_exec_failed",
            )
        except Exception:
            logger.exception(
                "Failed to persist the backup activation restart failure"
            )
            # Fail closed: releasing the guards without a durable rollback
            # would reopen mutations while the target remains selected.
            raise
        try:
            cutover_guard.close_from_restart_thread()
        except Exception:
            logger.exception(
                "Failed to release cutover guards after durable exec rollback"
            )
            raise

    async def abandon_and_release(*, error_code: str) -> None:
        # The journal transition is the commit point for reopening writes.
        # If it cannot be persisted, the self-retained guard stays fail-closed.
        service.abandon_prepared_activation(
            activation_id,
            error_code=error_code,
        )
        await cutover_guard.close()

    try:
        command, env_overrides = service.get_activation_restart_spec(
            activation_id
        )
        scheduled = schedule_process_restart(
            "Activating a validated native backup restore target",
            delay_seconds=1.0,
            command=command,
            env_overrides=env_overrides,
            on_failure=persist_exec_failure,
            reservation=cutover_guard.restart_reservation,
            before_exec=cutover_guard.verify_from_restart_thread,
        )
    except BackupServiceError:
        await abandon_and_release(error_code="restart_schedule_failed")
        raise
    except Exception as exc:
        await abandon_and_release(error_code="restart_schedule_failed")
        raise BackupServiceError(
            500,
            "backup_activation_restart_failed",
            "The activation was cleared because restart scheduling failed.",
        ) from exc
    if scheduled:
        return
    await abandon_and_release(error_code="restart_schedule_rejected")
    raise BackupServiceError(
        409,
        "backup_activation_restart_rejected",
        "The activation was cleared because PPBase could not schedule its restart.",
    )


def _upload_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "status": status_code,
            "message": message,
            "data": {"code": code},
        },
    )


class _BackupMultipartLimit(MultiPartException):
    pass


def _parse_content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise _upload_error(
            400,
            "invalid_content_length",
            "The request Content-Length is invalid.",
        ) from exc
    if value < 0:
        raise _upload_error(
            400,
            "invalid_content_length",
            "The request Content-Length is invalid.",
        )
    return value


def _prepare_uploaded_file(handle: Any) -> None:
    handle.flush()
    descriptor = handle.fileno()
    os.fchmod(descriptor, 0o600)
    os.fsync(descriptor)
    handle.seek(0)


async def _close_upload_form(form: FormData) -> None:
    """Close multipart spools without replacing the operation's outcome."""
    try:
        await form.close()
    except Exception:
        # A sealed import is already durable, while a rejected import must keep
        # its original error. Multipart cleanup is therefore always best effort.
        pass


async def _read_backup_upload(
    request: Request,
) -> tuple[FormData, UploadFile]:
    settings = request.app.state.settings
    max_upload = int(settings.backup_max_upload_bytes)
    overhead = int(settings.backup_multipart_overhead_bytes)
    if max_upload <= 0 or overhead <= 0:
        raise _upload_error(
            500,
            "backup_transport_limits_invalid",
            "The native backup upload limits are invalid.",
        )
    max_body = max_upload + overhead
    content_length = _parse_content_length(request)
    if content_length is not None and content_length > max_body:
        raise _upload_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "backup_upload_too_large",
            "The uploaded backup exceeds the configured size limit.",
        )
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        raise _upload_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "backup_upload_multipart_required",
            "The backup upload must be multipart/form-data with a file field.",
        )

    total = 0

    async def counted_stream():
        nonlocal total
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_body:
                raise _BackupMultipartLimit(
                    "The uploaded backup exceeds the configured size limit."
                )
            yield chunk

    parser = MultiPartParser(
        request.headers,
        counted_stream(),
        max_files=1,
        max_fields=0,
        max_part_size=_MAX_MULTIPART_FIELD_BYTES,
    )
    try:
        form = await parser.parse()
    except _BackupMultipartLimit as exc:
        raise _upload_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "backup_upload_too_large",
            str(exc),
        ) from exc
    except MultiPartException as exc:
        raise _upload_error(
            400,
            "backup_upload_invalid_multipart",
            "The backup multipart body is invalid.",
        ) from exc

    items = form.multi_items()
    if (
        len(items) != 1
        or items[0][0] != "file"
        or not isinstance(items[0][1], UploadFile)
    ):
        await _close_upload_form(form)
        raise _upload_error(
            400,
            "backup_upload_file_required",
            "Exactly one multipart file field named 'file' is required.",
        )
    upload = items[0][1]
    if upload.size is None or upload.size <= 0:
        await _close_upload_form(form)
        raise _upload_error(
            400,
            "backup_upload_empty",
            "The uploaded backup ZIP is empty.",
        )
    if upload.size > max_upload:
        await _close_upload_form(form)
        raise _upload_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "backup_upload_too_large",
            "The uploaded backup exceeds the configured size limit.",
        )
    try:
        await to_thread_quiescent(
            _prepare_uploaded_file,
            upload.file,
        )
    except asyncio.CancelledError:
        await _close_upload_form(form)
        raise
    except (OSError, ValueError) as exc:
        await _close_upload_form(form)
        raise _upload_error(
            400,
            "backup_upload_spool_failed",
            "The uploaded backup could not be prepared safely.",
        ) from exc
    return form, upload


def _content_disposition(filename: str) -> str:
    try:
        exact_name = validate_backup_transport_filename(filename)
    except ValueError:
        exact_name = "ppbase_backup.zip"
    safe_name = (
        exact_name
        if _SAFE_DOWNLOAD_NAME_RE.fullmatch(exact_name)
        else "ppbase_backup.zip"
    )
    encoded = quote(exact_name, safe="")
    return f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{encoded}'


async def _read_control_json(
    request: Request,
    *,
    allow_empty: bool = False,
) -> Any:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if content_length < 0 or content_length > _MAX_CONTROL_JSON_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Backup control body is too large.",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_CONTROL_JSON_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Backup control body is too large.",
            )
        body.extend(chunk)
    if not body and allow_empty:
        return {}
    if not body:
        raise HTTPException(status_code=400, detail="A JSON body is required.")

    def reject_non_integer_number(value: str) -> Any:
        raise ValueError(f"Non-integer JSON number is forbidden: {value}")

    try:
        return json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            parse_float=reject_non_integer_number,
            parse_constant=reject_non_integer_number,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 JSON body.") from exc


def _validate_body(model: type[BaseModel], payload: Any) -> BaseModel:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(), body=payload) from exc


@router.get("/identity")
async def get_backup_identity(
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    try:
        with _service(request) as service:
            return service.get_identity()
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@router.get("/readiness")
async def get_backup_readiness(
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        warnings = await _runtime_readiness_warnings(session)
    finally:
        await session.rollback()
    return _backup_readiness(
        request.app.state.settings,
        warnings=warnings,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_local_backup(
    request: Request,
    admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    """Create and seal one local backup synchronously."""
    payload = await _read_control_json(request, allow_empty=True)
    if payload is None:
        payload = {}
    body = _validate_body(BackupCreateBody, payload)
    assert isinstance(body, BackupCreateBody)
    try:
        with _service(request) as service:
            return await service.create_local_backup(
                actor_id=_actor_id(admin),
                transport_filename=body.name,
            )
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_local_backup(
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    form: FormData | None = None
    try:
        with _service(request) as service:
            with service.mutation_operation() as operation_lease:
                form, upload = await _read_backup_upload(request)
                return await service.upload_local_backup(
                    upload.file,
                    operation_lease=operation_lease,
                )
    except BackupServiceError as exc:
        _raise_backup_error(exc)
    finally:
        if form is not None:
            await _close_upload_form(form)


@router.get("")
async def list_local_backups(
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> list[dict[str, Any]]:
    try:
        with _service(request) as service:
            return await service.list_local_backups()
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@router.get("/trusted-signers")
async def list_trusted_backup_signers(
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> list[dict[str, Any]]:
    try:
        with _service(request) as service:
            return service.list_trusted_signers()
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@router.post("/{backup_id}/trust")
async def approve_backup_signer(
    backup_id: str,
    request: Request,
    admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    payload = await _read_control_json(request)
    body = _validate_body(BackupTrustApproveBody, payload)
    if not isinstance(body, BackupTrustApproveBody):  # pragma: no cover
        raise RuntimeError("Invalid backup trust approval model")
    try:
        with _service(request) as service:
            return await service.approve_backup_signer(
                backup_id,
                expected_fingerprint_sha256=body.fingerprint_sha256,
                actor_id=_actor_id(admin),
            )
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@router.delete(
    "/trusted-signers/{fingerprint_sha256}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_backup_signer(
    fingerprint_sha256: str,
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> Response:
    try:
        with _service(request) as service:
            service.revoke_backup_signer(fingerprint_sha256)
    except BackupServiceError as exc:
        _raise_backup_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{backup_id}")
async def inspect_or_download_local_backup(
    backup_id: str,
    request: Request,
    access: dict[str, Any] = Depends(_authorize_backup_read),
    resource_offset: Annotated[
        int,
        Query(alias="resourceOffset", ge=0),
    ] = 0,
    resource_limit: Annotated[
        int,
        Query(
            alias="resourceLimit",
            ge=1,
            le=BACKUP_INSPECTION_MAX_RESOURCE_LIMIT,
        ),
    ] = BACKUP_INSPECTION_DEFAULT_RESOURCE_LIMIT,
) -> Any:
    try:
        with _service(request) as service:
            if access.get("mode") != "download":
                return await service.inspect_local_backup(
                    backup_id,
                    resource_offset=resource_offset,
                    resource_limit=resource_limit,
                )
            pinned = await service.materialize_local_backup_zip(backup_id)
    except BackupServiceError as exc:
        _raise_backup_error(exc)
    try:
        return _PinnedBackupStreamingResponse(
            pinned,
            pinned.iter_bytes(
                int(request.app.state.settings.backup_transport_chunk_size)
            ),
            media_type="application/zip",
            headers={
                "Content-Length": str(pinned.size),
                "Content-Disposition": _content_disposition(pinned.filename),
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(pinned.close),
        )
    except BaseException:
        pinned.close()
        raise


@router.delete("/{backup_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_local_backup(
    backup_id: str,
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> Response:
    try:
        with _service(request) as service:
            await service.delete_local_backup(backup_id)
    except BackupServiceError as exc:
        _raise_backup_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{backup_id}/staging-plans", status_code=status.HTTP_201_CREATED)
async def create_staging_plan(
    backup_id: str,
    request: Request,
    admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    payload = await _read_control_json(request)
    body = _validate_body(StagingPlanCreateBody, payload)
    if not isinstance(body, StagingPlanCreateBody):  # pragma: no cover
        raise RuntimeError("Invalid staging plan model")
    try:
        with _service(request) as service:
            return await service.create_staging_plan(
                backup_id,
                jwt_secret_mode=body.jwt_secret_mode,
                actor_id=_actor_id(admin),
            )
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@router.post("/{backup_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore_local_backup_sdk_compatible(
    backup_id: str,
    request: Request,
    admin: dict[str, Any] = Depends(_require_backup_admin),
) -> Response:
    """Accept a PocketBase SDK restore and run it as an app-owned task."""
    try:
        await _restore_coordinator(request.app).start(
            request,
            backup_id,
            actor_id=_actor_id(admin),
        )
    except BackupServiceError as exc:
        _raise_backup_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@staging_router.get("/{plan_id}")
async def inspect_staging_plan(
    plan_id: str,
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    try:
        with _service(request) as service:
            return service.inspect_staging_plan(plan_id)
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@staging_router.post(
    "/{plan_id}/abandon",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def abandon_staging_plan(
    plan_id: str,
    request: Request,
    admin: dict[str, Any] = Depends(_require_backup_admin),
) -> Response:
    payload = await _read_control_json(request)
    body = _validate_body(StagingExecuteBody, payload)
    if not isinstance(body, StagingExecuteBody):  # pragma: no cover
        raise RuntimeError("Invalid staging abandonment model")
    try:
        with _service(request) as service:
            await service.abandon_staging_plan(
                plan_id,
                expected_plan_hash=body.plan_hash,
                actor_id=_actor_id(admin),
            )
    except BackupServiceError as exc:
        _raise_backup_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@staging_router.post("/{plan_id}/execute")
async def execute_staging_plan(
    plan_id: str,
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    payload = await _read_control_json(request)
    body = _validate_body(StagingExecuteBody, payload)
    if not isinstance(body, StagingExecuteBody):  # pragma: no cover
        raise RuntimeError("Invalid staging execute model")
    try:
        with _service(request) as service:
            return await service.execute_staging_plan(
                plan_id,
                expected_plan_hash=body.plan_hash,
            )
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@staging_router.post("/{plan_id}/activate", status_code=status.HTTP_202_ACCEPTED)
async def activate_staging_plan(
    plan_id: str,
    request: Request,
    admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    payload = await _read_control_json(request)
    body = _validate_body(StagingActivateBody, payload)
    if not isinstance(body, StagingActivateBody):  # pragma: no cover
        raise RuntimeError("Invalid staging activation model")
    try:
        with _service(request) as service:
            prepared = await service.activate_staging_plan(
                plan_id,
                expected_plan_hash=body.plan_hash,
                actor_id=_actor_id(admin),
                activation_id=body.activation_id,
                resume_token=body.resume_token,
            )
            await _schedule_backup_activation(service, prepared)
            return prepared
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@activation_router.get("/{activation_id}")
async def inspect_backup_activation(
    activation_id: str,
    request: Request,
) -> dict[str, Any]:
    """Poll restart/rollback with either admin auth or a scoped resume token."""
    resume_token = str(
        request.headers.get("X-PPBase-Activation-Token", "") or ""
    ).strip()
    if len(resume_token) > 512:
        resume_token = ""
    control_root: ControlPlaneRoot | None = None
    store: BackupActivationStore | None = None
    try:
        control_path = Path(
            str(request.app.state.settings.backup_control_dir)
        ).expanduser()
        if not control_path.is_absolute():
            control_path = Path(os.path.abspath(os.fspath(control_path)))
        control_root = ControlPlaneRoot.open(control_path, create_missing=False)
        store = BackupActivationStore(control_root)
        state = (
            store.authenticate_state(activation_id, resume_token)
            if resume_token
            else None
        )
        if state is None:
            await _require_activation_admin(request)
            state = store.inspect(activation_id)
        return store.public_payload(state)
    except ActivationError as exc:
        control_detached = "detached or substituted" in str(exc)
        if control_root is not None and not control_detached:
            try:
                control_root.verify_attached()
            except ControlPlaneSafetyError:
                control_detached = True
        if control_detached:
            _raise_backup_error(
                BackupServiceError(
                    500,
                    "backup_control_invalid",
                    "The native backup activation control plane is missing or unsafe.",
                )
            )
        _raise_backup_error(
            BackupServiceError(
                404,
                "backup_activation_not_found",
                "The requested backup activation was not found or is invalid.",
            )
        )
    except (ControlPlaneSafetyError, OSError) as exc:
        _raise_backup_error(
            BackupServiceError(
                500,
                "backup_control_invalid",
                "The native backup activation control plane is missing or unsafe.",
            )
        )
    finally:
        if store is not None:
            store.close()
        if control_root is not None:
            control_root.close()
