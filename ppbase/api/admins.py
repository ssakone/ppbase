"""Admin API routes.

Endpoints:
    POST   /api/admins/init               (public, first-run only)
    POST   /api/admins/auth-with-password
    POST   /api/admins/auth-refresh
    GET    /api/admins
    POST   /api/admins
    GET    /api/admins/{id}
    PATCH  /api/admins/{id}
    DELETE /api/admins/{id}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, get_settings, require_admin
from ppbase.services import admin_service

router = APIRouter()


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

    if len(body.password) < 8:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "Password must be at least 8 characters.",
                "data": {
                    "password": {
                        "code": "validation_length_out_of_range",
                        "message": "The length must be at least 8 characters.",
                    }
                },
            },
        )

    from ppbase.services.admin_service import _admin_to_dict, _get_superusers_collection
    from ppbase.services.auth_service import create_admin_token

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
                "message": "The request requires admin authorization token to be set.",
                "data": {},
            },
        )
    return result


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
    from ppbase.services.admin_service import _admin_to_dict

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
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Delete an admin (cannot delete the last one)."""
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

    if len(body.password) < 8:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "Password must be at least 8 characters.",
                "data": {
                    "password": {
                        "code": "validation_length_out_of_range",
                        "message": "The length must be at least 8 characters.",
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
