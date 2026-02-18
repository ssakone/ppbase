"""Setup token for first-admin creation (PocketBase-style one-time URL)."""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.core.id_generator import generate_id
from ppbase.db.system_tables import ParamRecord

_SETUP_TOKEN_KEY = "_setup_token"


def _generate_setup_token() -> str:
    """Generate a cryptographically secure one-time setup token."""
    return secrets.token_urlsafe(32)


async def get_or_create_setup_token(session: AsyncSession) -> str:
    """Return the setup token, creating one if none exists. Call only when no admins."""
    q = select(ParamRecord).where(ParamRecord.key == _SETUP_TOKEN_KEY)
    row = (await session.execute(q)).scalars().first()
    if row is not None and isinstance(row.value, dict):
        token = row.value.get("token")
        if isinstance(token, str) and token:
            return token

    token = _generate_setup_token()
    row = ParamRecord(
        id=generate_id(),
        key=_SETUP_TOKEN_KEY,
        value={"token": token},
    )
    session.add(row)
    await session.flush()
    return token


async def validate_setup_token(session: AsyncSession, token: str | None) -> bool:
    """Return True if the token is valid and setup is allowed (no admins + matching token)."""
    if not token or not token.strip():
        return False

    from ppbase.services.admin_service import count_admins

    if await count_admins(session) > 0:
        return False

    q = select(ParamRecord).where(ParamRecord.key == _SETUP_TOKEN_KEY)
    row = (await session.execute(q)).scalars().first()
    if row is None or not isinstance(row.value, dict):
        return False

    stored = row.value.get("token")
    return isinstance(stored, str) and secrets.compare_digest(stored, token.strip())


async def consume_setup_token(session: AsyncSession, token: str | None) -> bool:
    """Validate token, delete it, and return True. Return False if invalid."""
    if not await validate_setup_token(session, token):
        return False

    q = select(ParamRecord).where(ParamRecord.key == _SETUP_TOKEN_KEY)
    row = (await session.execute(q)).scalars().first()
    if row is not None:
        await session.delete(row)
        await session.flush()
    return True
