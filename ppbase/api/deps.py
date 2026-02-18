"""FastAPI dependency injection utilities."""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.db.engine import get_engine, get_async_session
from ppbase.db.system_tables import SuperuserRecord, CollectionRecord


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def get_settings(request: Request) -> Any:
    """Retrieve the application ``Settings`` from ``app.state``."""
    return request.app.state.settings


# ---------------------------------------------------------------------------
# Database session
# ---------------------------------------------------------------------------


async def get_session():
    """Yield an ``AsyncSession`` for the request lifetime."""
    async for s in get_async_session():
        yield s


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


async def get_optional_auth(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any] | None:
    """Extract and decode a JWT from the Authorization header.

    PocketBase sends tokens without a ``Bearer`` prefix, but we accept both
    ``TOKEN`` and ``Bearer TOKEN`` for flexibility.

    Returns:
        The decoded JWT payload dict, or ``None`` if no valid token is present.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return None

    token = auth_header
    if token.lower().startswith("bearer "):
        token = token[7:]
    token = token.strip()
    if not token:
        return None

    settings = request.app.state.settings
    secret = settings.get_jwt_secret()

    # Try to decode without full verification first to get claims
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None

    token_type = unverified.get("type")
    token_id = unverified.get("id")
    if not token_id:
        return None

    # For admin tokens, look up the admin to build the full secret
    if token_type == "admin":
        admin = await session.get(SuperuserRecord, token_id)
        if admin is None:
            return None
        # Look up _superusers collection for per-collection secret
        su_stmt = select(CollectionRecord).where(CollectionRecord.name == "_superusers")
        su_coll = (await session.execute(su_stmt)).scalars().first()
        if su_coll is not None:
            from ppbase.services.auth_service import get_collection_token_config
            auth_secret, _ = get_collection_token_config(su_coll, 'authToken')
            full_secret = admin.token_key + auth_secret
        else:
            full_secret = admin.token_key + secret  # fallback
        try:
            payload = jwt.decode(token, full_secret, algorithms=["HS256"])
            return payload
        except jwt.InvalidTokenError:
            return None

    # For auth record tokens, do full verification with the record's token_key
    if token_type == "authRecord":
        collection_id = unverified.get("collectionId")
        if not collection_id:
            return None

        # Look up the auth collection's record to get token_key
        from sqlalchemy import text as _text
        from ppbase.db.engine import get_engine

        engine = get_engine()
        # First resolve the collection to get its table name
        stmt = select(CollectionRecord).where(
            CollectionRecord.id == collection_id
        )
        coll = (await session.execute(stmt)).scalars().first()
        if coll is None:
            # Token claims a collection that doesn't exist - reject explicitly.
            # PocketBase returns 404 when the referenced collection is missing.
            raise HTTPException(
                status_code=404,
                detail={
                    "status": 404,
                    "message": f"Missing collection with id \"{collection_id}\".",
                    "data": {},
                },
            )

        table_name = coll.name
        sql = _text(
            f'SELECT "token_key" FROM "{table_name}" WHERE "id" = :rid LIMIT 1'
        )
        async with engine.connect() as conn:
            result = await conn.execute(sql, {"rid": token_id})
            row = result.mappings().first()

        if row is None:
            return None

        record_token_key = row["token_key"]
        from ppbase.services.auth_service import get_collection_token_config
        auth_secret, _ = get_collection_token_config(coll, 'authToken')
        full_secret = record_token_key + auth_secret
        try:
            payload = jwt.decode(token, full_secret, algorithms=["HS256"])
            return payload
        except jwt.InvalidTokenError:
            return None

    return None


async def require_admin(
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> dict[str, Any]:
    """Require that the request has a valid admin token.

    Raises:
        HTTPException(401): If no admin auth is present.
    """
    if auth is None or auth.get("type") != "admin":
        raise HTTPException(
            status_code=401,
            detail={
                "status": 401,
                "message": "The request requires admin authorization token to be set.",
                "data": {},
            },
        )
    return auth


async def require_auth(
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> dict[str, Any]:
    """Require that the request has any valid auth token."""
    if auth is None:
        raise HTTPException(
            status_code=401,
            detail={
                "status": 401,
                "message": "The request requires authentication.",
                "data": {},
            },
        )
    return auth


async def require_record_auth(
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> dict[str, Any]:
    """Require that the request has an auth-record token."""
    if auth is None:
        raise HTTPException(
            status_code=401,
            detail={
                "status": 401,
                "message": "The request requires authentication.",
                "data": {},
            },
        )
    if auth.get("type") != "authRecord":
        raise HTTPException(
            status_code=403,
            detail={
                "status": 403,
                "message": "The authorized auth model is not allowed to perform this action.",
                "data": {},
            },
        )
    return auth


# ---------------------------------------------------------------------------
# Collection resolver
# ---------------------------------------------------------------------------


async def resolve_collection(
    session: AsyncSession,
    collection_id_or_name: str,
) -> CollectionRecord:
    """Lookup a collection by ID or name.

    Raises:
        HTTPException(404): If the collection is not found.
    """
    # Try by ID first
    record = await session.get(CollectionRecord, collection_id_or_name)
    if record is not None:
        return record

    # Try by name
    q = select(CollectionRecord).where(
        CollectionRecord.name == collection_id_or_name
    )
    record = (await session.execute(q)).scalars().first()
    if record is not None:
        return record

    raise HTTPException(
        status_code=404,
        detail={
            "status": 404,
            "message": f"Missing collection with id or name \"{collection_id_or_name}\".",
            "data": {},
        },
    )
