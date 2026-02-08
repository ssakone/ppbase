"""Admin CRUD and authentication service."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.core.id_generator import generate_id
from ppbase.db.system_tables import SuperuserRecord
from ppbase.services.auth_service import (
    create_admin_token,
    generate_token_key,
    hash_password,
    verify_password,
)


def _fmt_dt(dt: datetime | None) -> str:
    """Format datetime to PocketBase API format."""
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _admin_to_dict(admin: SuperuserRecord) -> dict[str, Any]:
    """Serialise an SuperuserRecord to the API response format."""
    return {
        "id": admin.id,
        "created": _fmt_dt(admin.created),
        "updated": _fmt_dt(admin.updated),
        "email": admin.email,
        "avatar": admin.avatar,
    }


async def list_admins(
    session: AsyncSession,
    page: int = 1,
    per_page: int = 30,
) -> dict[str, Any]:
    """Return a paginated list of admin records."""
    count_q = select(func.count()).select_from(SuperuserRecord)
    total_items = (await session.execute(count_q)).scalar() or 0
    total_pages = max(1, math.ceil(total_items / per_page))

    q = (
        select(SuperuserRecord)
        .order_by(SuperuserRecord.created.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    rows = (await session.execute(q)).scalars().all()
    return {
        "page": page,
        "perPage": per_page,
        "totalItems": total_items,
        "totalPages": total_pages,
        "items": [_admin_to_dict(r) for r in rows],
    }


async def get_admin(session: AsyncSession, admin_id: str) -> SuperuserRecord | None:
    """Fetch a single admin by ID."""
    return await session.get(SuperuserRecord, admin_id)


async def get_admin_by_email(session: AsyncSession, email: str) -> SuperuserRecord | None:
    """Fetch a single admin by email."""
    q = select(SuperuserRecord).where(SuperuserRecord.email == email)
    return (await session.execute(q)).scalars().first()


async def create_admin(
    session: AsyncSession,
    email: str,
    password: str,
    avatar: int = 0,
) -> SuperuserRecord:
    """Create a new admin record."""
    admin = SuperuserRecord(
        id=generate_id(),
        email=email,
        password_hash=hash_password(password),
        token_key=generate_token_key(),
        avatar=avatar,
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
    )
    session.add(admin)
    await session.flush()
    return admin


async def update_admin(
    session: AsyncSession,
    admin_id: str,
    data: dict[str, Any],
) -> SuperuserRecord | None:
    """Update an existing admin record."""
    admin = await session.get(SuperuserRecord, admin_id)
    if admin is None:
        return None

    if "email" in data:
        admin.email = data["email"]
    if "password" in data and data["password"]:
        admin.password_hash = hash_password(data["password"])
        admin.token_key = generate_token_key()
    if "avatar" in data:
        admin.avatar = data["avatar"]

    admin.updated = datetime.now(timezone.utc)
    await session.flush()
    return admin


async def delete_admin(session: AsyncSession, admin_id: str) -> bool:
    """Delete an admin. Returns False if it's the last admin."""
    count_q = select(func.count()).select_from(SuperuserRecord)
    total = (await session.execute(count_q)).scalar() or 0
    if total <= 1:
        return False

    admin = await session.get(SuperuserRecord, admin_id)
    if admin is None:
        return False

    await session.delete(admin)
    await session.flush()
    return True


async def auth_with_password(
    session: AsyncSession,
    email: str,
    password: str,
    settings: Any,
) -> dict[str, Any] | None:
    """Authenticate an admin with email + password.

    Returns:
        ``{"token": str, "admin": dict}`` on success, or ``None`` on failure.
    """
    admin = await get_admin_by_email(session, email)
    if admin is None:
        return None
    if not verify_password(password, admin.password_hash):
        return None

    token = create_admin_token(admin, settings)
    return {"token": token, "admin": _admin_to_dict(admin)}


async def auth_refresh(
    session: AsyncSession,
    admin_id: str,
    settings: Any,
) -> dict[str, Any] | None:
    """Refresh an admin token.

    Returns:
        ``{"token": str, "admin": dict}`` on success, or ``None`` if the admin is
        not found.
    """
    admin = await session.get(SuperuserRecord, admin_id)
    if admin is None:
        return None

    token = create_admin_token(admin, settings)
    return {"token": token, "admin": _admin_to_dict(admin)}
