"""Admin API routes.

Endpoints:
    POST   /api/admins/init               (public, first-run only)
    POST   /api/admins/auth-with-password
    POST   /api/admins/auth-refresh
    POST   /api/admins/request-password-reset   (PocketBase 0.22)
    POST   /api/admins/confirm-password-reset   (PocketBase 0.22)
    GET    /api/admins
    POST   /api/admins
    GET    /api/admins/{id}
    PATCH  /api/admins/{id}
    DELETE /api/admins/{id}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import (
    ADMIN_AUTH_REQUIRED_MESSAGE,
    get_session,
    get_settings,
    require_admin,
)
from ppbase.db.engine import get_async_session
from ppbase.models.field_types import _EMAIL_RE
from ppbase.services import admin_service

logger = logging.getLogger(__name__)
router = APIRouter()

# PocketBase 0.22.34 validation wording, measured on the official binary.
_BLANK = {"code": "validation_required", "message": "Cannot be blank."}
_ADMIN_PASSWORD_MIN, _ADMIN_PASSWORD_MAX = 10, 72  # forms/admin_password_reset_confirm.go:45


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class AuthWithPasswordBody(BaseModel):
    identity: str
    password: str


class AdminCreateBody(BaseModel):
    email: str
    password: str
    avatar: int = 0


class AdminUpdateBody(BaseModel):
    email: str | None = None
    password: str | None = None
    avatar: int | None = None


class ChangePasswordBody(BaseModel):
    password: str
    passwordConfirm: str


class InitBody(BaseModel):
    email: str
    password: str
    passwordConfirm: str
    setupToken: str | None = None


# ---------------------------------------------------------------------------
# Init endpoint (public, first-run only)
# ---------------------------------------------------------------------------


@router.post("/init")
async def init_admin(
    body: InitBody,
    session: AsyncSession = Depends(get_session),
    settings: Any = Depends(get_settings),
):
    """Create the first admin account.

    This endpoint is public and only works when no admins exist yet.
    Requires a valid setupToken (from the one-time URL printed at startup).
    Once an admin has been created, subsequent calls return 400.
    """
    count = await admin_service.count_admins(session)
    if count > 0:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "An admin already exists. Use the login endpoint instead.",
                "data": {},
            },
        )

    if body.password != body.passwordConfirm:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "Password and confirmation do not match.",
                "data": {
                    "passwordConfirm": {
                        "code": "validation_values_mismatch",
                        "message": "Values don't match.",
                    }
                },
            },
        )

    from ppbase.models.field_types import FieldValidationError
    from ppbase.services.auth_service import validate_password_value
    from ppbase.services.admin_service import _get_superusers_collection

    superusers_collection = await _get_superusers_collection(session)
    try:
        validate_password_value(superusers_collection, body.password)
    except FieldValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "An error occurred while validating the submitted data.",
                "data": {
                    "password": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
            },
        )

    from ppbase.services.admin_service import _admin_to_dict, _get_superusers_collection
    from ppbase.services.auth_service import create_admin_token
    from ppbase.services.setup_service import consume_setup_token

    if not await consume_setup_token(session, body.setupToken):
        raise HTTPException(
            status_code=403,
            detail={
                "status": 403,
                "message": "Invalid or expired setup token. Use the URL printed in the server console.",
                "data": {},
            },
        )

    admin = await admin_service.create_admin(session, body.email, body.password)
    await session.commit()

    su_coll = await _get_superusers_collection(session)
    token = create_admin_token(admin, settings, superusers_collection=su_coll)
    return {"token": token, "admin": _admin_to_dict(admin)}


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@router.post("/auth-with-password")
async def auth_with_password(
    body: AuthWithPasswordBody,
    session: AsyncSession = Depends(get_session),
    settings: Any = Depends(get_settings),
):
    """Authenticate an admin with email + password."""
    result = await admin_service.auth_with_password(
        session, body.identity, body.password, settings
    )
    if result is None:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "Failed to authenticate.",
                "data": {
                    "identity": {
                        "code": "validation_invalid_credentials",
                        "message": "Invalid login credentials.",
                    }
                },
            },
        )
    return result


@router.post("/auth-refresh")
async def auth_refresh(
    admin_auth: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    settings: Any = Depends(get_settings),
):
    """Refresh an admin auth token."""
    result = await admin_service.auth_refresh(
        session, admin_auth["id"], settings
    )
    if result is None:
        raise HTTPException(
            status_code=401,
            detail={
                "status": 401,
                "message": ADMIN_AUTH_REQUIRED_MESSAGE,
                "data": {},
            },
        )
    return result


async def _json_object_or_error(request: Request) -> dict[str, Any]:
    """Body of a PocketBase 0.22 form: a JSON object, empty meaning ``{}``."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise _bad_request("An error occurred while loading the submitted data.")
    return body


def _bad_request(message: str, data: dict[str, Any] | None = None) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"status": 400, "message": message, "data": data or {}},
    )


def _string_fields(body: dict[str, Any], *names: str) -> dict[str, str]:
    """Go binds JSON strings only: any other value is a loading error."""
    values: dict[str, str] = {}
    for name in names:
        value = body.get(name)
        if value is None:
            values[name] = ""
        elif isinstance(value, str):
            values[name] = value
        else:
            raise _bad_request("An error occurred while loading the submitted data.")
    return values


async def _send_admin_password_reset(email: str, settings: Any) -> None:
    """Background part of the reset request: errors are never shown to the client."""
    try:
        async for session in get_async_session():
            await admin_service.request_password_reset(session, email, settings)
    except Exception as exc:  # pragma: no cover - logged, like PocketBase's FireAndForget
        logger.error("Failed to send admin password reset request: %s", type(exc).__name__)


@router.post("/request-password-reset", status_code=204)
async def request_password_reset(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Any = Depends(get_settings),
):
    """PocketBase 0.22 admin password reset request (apis/admin.go:21, :111-154).

    Answers 204 for any valid e-mail, known or not, before anything is sent
    (protection against admin enumeration); the link is sent in the background,
    built on the stored application URL only (services/app_url.py), never on
    the request headers.
    """
    body = await _json_object_or_error(request)
    email = _string_fields(body, "email")["email"]
    if not email:
        raise _bad_request("An error occurred while validating the form.", {"email": _BLANK})
    if len(email) > 255 or not _EMAIL_RE.match(email):
        raise _bad_request(
            "An error occurred while validating the form.",
            {"email": {"code": "validation_is_email", "message": "Must be a valid email address."}},
        )

    background_tasks.add_task(_send_admin_password_reset, email, settings)
    return Response(status_code=204)


@router.post("/confirm-password-reset", status_code=204)
async def confirm_password_reset(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """PocketBase 0.22 admin password reset confirmation (apis/admin.go:22, :156-186).

    The token is checked like ``forms/admin_password_reset_confirm.go:41-62``:
    signature with the admin token key plus the reset secret, never the type.
    On success the token key is renewed, revoking every session and link.
    """
    body = await _json_object_or_error(request)
    values = _string_fields(body, "token", "password", "passwordConfirm")
    token, password, confirm = values["token"], values["password"], values["passwordConfirm"]

    errors: dict[str, Any] = {}
    admin = None
    if not token:
        errors["token"] = _BLANK
    else:
        admin = await admin_service.find_admin_by_password_reset_token(session, token)
        if admin is None:
            errors["token"] = {
                "code": "validation_invalid_token",
                "message": "Invalid or expired token.",
            }
    if not password:
        errors["password"] = _BLANK
    elif not _ADMIN_PASSWORD_MIN <= len(password) <= _ADMIN_PASSWORD_MAX:
        errors["password"] = {
            "code": "validation_length_out_of_range",
            "message": (
                f"The length must be between {_ADMIN_PASSWORD_MIN} and {_ADMIN_PASSWORD_MAX}."
            ),
        }
    if not confirm:
        errors["passwordConfirm"] = _BLANK
    elif confirm != password:
        errors["passwordConfirm"] = {
            "code": "validation_values_mismatch",
            "message": "Values don't match.",
        }
    if errors:
        raise _bad_request("Something went wrong while processing your request.", errors)

    await admin_service.set_admin_password(session, admin, password)
    admin.updated = datetime.now(timezone.utc)
    await session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_admins(
    page: int = 1,
    perPage: int = 30,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """List all admins (admin auth required)."""
    return await admin_service.list_admins(session, page, perPage)


@router.post("")
async def create_admin(
    body: AdminCreateBody,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Create a new admin (admin auth required)."""
    from ppbase.models.field_types import FieldValidationError
    from ppbase.services.admin_service import _admin_to_dict, _get_superusers_collection
    from ppbase.services.auth_service import validate_password_value

    try:
        validate_password_value(
            await _get_superusers_collection(session),
            body.password,
        )
    except FieldValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "An error occurred while validating the submitted data.",
                "data": {"password": {"code": exc.code, "message": exc.message}},
            },
        )

    admin = await admin_service.create_admin(
        session, body.email, body.password, body.avatar
    )
    await session.commit()
    return _admin_to_dict(admin)


@router.get("/{admin_id}")
async def view_admin(
    admin_id: str,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """View a single admin."""
    from ppbase.services.admin_service import _admin_to_dict

    admin = await admin_service.get_admin(session, admin_id)
    if admin is None:
        raise HTTPException(
            status_code=404,
            detail={
                "status": 404,
                "message": "The requested resource wasn't found.",
                "data": {},
            },
        )
    return _admin_to_dict(admin)


@router.patch("/{admin_id}")
async def update_admin(
    admin_id: str,
    body: AdminUpdateBody,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Update an admin."""
    from ppbase.services.admin_service import _admin_to_dict

    data = body.model_dump(exclude_none=True)
    admin = await admin_service.update_admin(session, admin_id, data)
    if admin is None:
        raise HTTPException(
            status_code=404,
            detail={
                "status": 404,
                "message": "The requested resource wasn't found.",
                "data": {},
            },
        )
    await session.commit()
    return _admin_to_dict(admin)


@router.delete("/{admin_id}", status_code=204)
async def delete_admin(
    admin_id: str,
    request: Request,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Delete an admin (cannot delete the last one)."""
    superusers_collection = await admin_service._get_superusers_collection(session)
    deleted = await admin_service.delete_admin(session, admin_id)
    if not deleted:
        # Either not found or last admin
        admin = await admin_service.get_admin(session, admin_id)
        if admin is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "status": 404,
                    "message": "The requested resource wasn't found.",
                    "data": {},
                },
            )
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "You cannot delete the last admin.",
                "data": {},
            },
        )
    await session.commit()

    # Realtime clients of the deleted admin become guests, as in PocketBase
    # (v0.22.34 apis/realtime.go:269-280, unregisterClientsByAuthModel).
    manager = getattr(request.app.state, "subscription_manager", None)
    if manager is not None and superusers_collection is not None:
        manager.unset_auth_record(str(superusers_collection.id), admin_id)


@router.post("/{admin_id}/change-password")
async def change_password(
    admin_id: str,
    body: ChangePasswordBody,
    admin_auth: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Change an admin's password.

    The caller must provide matching password and passwordConfirm values.
    After the password is changed, the admin's token_key is rotated, which
    invalidates all existing sessions.
    """
    if body.password != body.passwordConfirm:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "Password and confirmation do not match.",
                "data": {
                    "passwordConfirm": {
                        "code": "validation_values_mismatch",
                        "message": "Values don't match.",
                    }
                },
            },
        )

    from ppbase.models.field_types import FieldValidationError
    from ppbase.services.auth_service import validate_password_value
    try:
        validate_password_value(
            await admin_service._get_superusers_collection(session),
            body.password,
        )
    except FieldValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "An error occurred while validating the submitted data.",
                "data": {
                    "password": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
            },
        )

    admin = await admin_service.update_admin(
        session, admin_id, {"password": body.password}
    )
    if admin is None:
        raise HTTPException(
            status_code=404,
            detail={
                "status": 404,
                "message": "The requested resource wasn't found.",
                "data": {},
            },
        )
    await session.commit()
    return {"message": "Password changed successfully."}
