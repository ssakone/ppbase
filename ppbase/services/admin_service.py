"""Admin CRUD and authentication service."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import jwt

from ppbase.core.id_generator import generate_id
from ppbase.db.system_tables import ParamRecord, SuperuserRecord
from ppbase.services.auth_service import (
    create_admin_token,
    create_token,
    generate_token_key,
    get_collection_token_config,
    get_password_cost,
    hash_password,
    verify_password,
)

# PocketBase 0.22.34 forms/admin_password_reset_request.go:35 (2 minutes).
ADMIN_PASSWORD_RESET_RESEND_SECONDS = 120


async def _get_superusers_collection(session: AsyncSession):
    """Fetch the _superusers CollectionRecord."""
    from ppbase.db.system_tables import CollectionRecord
    stmt = select(CollectionRecord).where(CollectionRecord.name == "_superusers")
    return (await session.execute(stmt)).scalars().first()


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


async def count_admins(session: AsyncSession) -> int:
    """Return the total number of admin (superuser) records."""
    count_q = select(func.count()).select_from(SuperuserRecord)
    return (await session.execute(count_q)).scalar() or 0


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
    superusers_collection = await _get_superusers_collection(session)
    admin = SuperuserRecord(
        id=generate_id(),
        email=email,
        password_hash=hash_password(
            password,
            cost=get_password_cost(superusers_collection),
        ),
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
        admin.token_key = generate_token_key()
    if "password" in data and data["password"]:
        await set_admin_password(session, admin, data["password"])
    if "avatar" in data:
        admin.avatar = data["avatar"]

    admin.updated = datetime.now(timezone.utc)
    await session.flush()
    return admin


async def set_admin_password(
    session: AsyncSession,
    admin: SuperuserRecord,
    password: str,
) -> None:
    """Set a new password like PocketBase 0.22.34 ``Admin.SetPassword``.

    The token key is renewed (previous sessions and reset links are revoked)
    and the password reset throttle is cleared (models/admin.go:42-61).
    """
    superusers_collection = await _get_superusers_collection(session)
    admin.password_hash = hash_password(
        password,
        cost=get_password_cost(superusers_collection),
    )
    admin.token_key = generate_token_key()
    admin.last_reset_sent_at = None


def create_admin_password_reset_token(
    admin: SuperuserRecord,
    superusers_collection: Any,
) -> str:
    """PocketBase 0.22.34 ``tokens.NewAdminResetPasswordToken`` (tokens/admin.go:19-26).

    Claims ``id``, ``type=admin`` and ``email``, signed with the admin token key
    plus the reset secret (``adminPasswordResetToken`` in 0.22, the
    ``_superusers`` ``passwordResetToken`` option in PPBase).
    """
    secret, duration = get_collection_token_config(
        superusers_collection, "passwordResetToken"
    )
    return create_token(
        {"id": admin.id, "type": "admin", "email": admin.email},
        admin.token_key + secret,
        duration,
    )


async def find_admin_by_password_reset_token(
    session: AsyncSession,
    token: str,
) -> SuperuserRecord | None:
    """PocketBase 0.22.34 ``FindAdminByToken`` with the reset secret.

    Only the signature (admin token key plus reset secret) and the expiry are
    checked, never the type claim (daos/admin.go:52-78,
    forms/admin_password_reset_confirm.go:50-62).
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None
    admin_id = unverified.get("id")
    if not isinstance(admin_id, str) or not admin_id:
        return None
    admin = await session.get(SuperuserRecord, admin_id)
    if admin is None:
        return None
    superusers_collection = await _get_superusers_collection(session)
    if superusers_collection is None:
        return None
    secret, _ = get_collection_token_config(superusers_collection, "passwordResetToken")
    try:
        jwt.decode(token, admin.token_key + secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    return admin


async def request_password_reset(
    session: AsyncSession,
    email: str,
    settings: Any,
) -> bool:
    """E-mail a password reset link to the admin with exactly ``email``.

    PocketBase 0.22.34 forms/admin_password_reset_request.go:63-89: unknown
    e-mail, or a link sent less than 120 seconds ago, sends nothing; the send
    time is stored only once the message is accepted.  The link is built on
    the stored ``meta.appURL`` only (mails/admin.go:22-26), never on request
    headers.  Returns whether a message was sent.
    """
    from ppbase.services.app_url import app_url_from_settings
    from ppbase.services.mail_service import send_admin_password_reset_email

    admin = await get_admin_by_email(session, email)
    if admin is None:
        return False
    last_sent = admin.last_reset_sent_at
    if last_sent is not None:
        elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds()
        if elapsed < ADMIN_PASSWORD_RESET_RESEND_SECONDS:
            return False
    superusers_collection = await _get_superusers_collection(session)
    if superusers_collection is None:
        return False

    app_name = ""
    settings_row = (
        await session.execute(select(ParamRecord.value).where(ParamRecord.key == "settings"))
    ).scalar_one_or_none()
    if isinstance(settings_row, dict) and isinstance(settings_row.get("meta"), dict):
        app_name = str(settings_row["meta"].get("appName", "") or "")

    token = create_admin_password_reset_token(admin, superusers_collection)
    sent = await send_admin_password_reset_email(
        admin.email,
        token,
        settings,
        base_url=app_url_from_settings(settings_row),
        app_name=app_name,
    )
    if not sent:
        await session.rollback()
        return False
    admin.last_reset_sent_at = datetime.now(timezone.utc)
    await session.commit()
    return True


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

    su_coll = await _get_superusers_collection(session)
    token = create_admin_token(admin, settings, superusers_collection=su_coll)
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

    su_coll = await _get_superusers_collection(session)
    token = create_admin_token(admin, settings, superusers_collection=su_coll)
    return {"token": token, "admin": _admin_to_dict(admin)}
