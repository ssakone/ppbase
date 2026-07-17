"""Superuser API for native local backup and restore staging/validation."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, require_admin
from ppbase.backup.service import BackupServiceError, NativeBackupService
from ppbase.db.engine import get_engine


router = APIRouter(prefix="/backups", tags=["backups"])
staging_router = APIRouter(prefix="/backup-staging", tags=["backup-staging"])
_MAX_CONTROL_JSON_BYTES = 32 * 1024


class StagingPlanCreateBody(BaseModel):
    jwt_secret_mode: str = Field(alias="jwtSecretMode")

    model_config = {"populate_by_name": True, "extra": "forbid"}


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


async def _read_control_json(request: Request) -> Any:
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
        return _service(request).get_identity()
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_local_backup(
    request: Request,
    admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    """Create and seal one local backup synchronously."""
    try:
        return await _service(request).create_local_backup(
            actor_id=_actor_id(admin),
        )
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@router.get("")
async def list_local_backups(
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> list[dict[str, Any]]:
    try:
        return await _service(request).list_local_backups()
    except BackupServiceError as exc:
        _raise_backup_error(exc)


@router.get("/{backup_id}")
async def inspect_local_backup(
    backup_id: str,
    request: Request,
    _admin: dict[str, Any] = Depends(_require_backup_admin),
) -> dict[str, Any]:
    try:
        return await _service(request).inspect_local_backup(backup_id)
    except BackupServiceError as exc:
        _raise_backup_error(exc)


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
        return await _service(request).create_staging_plan(
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
        return _service(request).inspect_staging_plan(plan_id)
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
        return await _service(request).execute_staging_plan(
            plan_id,
            expected_plan_hash=body.plan_hash,
        )
    except BackupServiceError as exc:
        _raise_backup_error(exc)
