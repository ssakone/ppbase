"""Superuser API for native local backup and restore staging/validation."""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from ppbase.api.deps import get_optional_auth, get_session, require_admin
from ppbase.backup.service import BackupServiceError, NativeBackupService
from ppbase.backup.transport import (
    PinnedBackupZip,
    validate_backup_transport_filename,
)
from ppbase.db.engine import get_engine
from ppbase.services.async_utils import to_thread_quiescent
from ppbase.services.file_tokens import verify_file_token


router = APIRouter(prefix="/backups", tags=["backups"])
staging_router = APIRouter(prefix="/backup-staging", tags=["backup-staging"])
_MAX_CONTROL_JSON_BYTES = 32 * 1024
_MAX_MULTIPART_FIELD_BYTES = 64 * 1024
_SAFE_DOWNLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.zip$")


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

    model_config = {"populate_by_name": True, "extra": "forbid"}


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


@router.get("/{backup_id}")
async def inspect_or_download_local_backup(
    backup_id: str,
    request: Request,
    access: dict[str, Any] = Depends(_authorize_backup_read),
) -> Any:
    try:
        with _service(request) as service:
            if access.get("mode") != "download":
                return await service.inspect_local_backup(backup_id)
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
