"""PocketBase-compatible short-lived tokens for protected downloads."""

from __future__ import annotations

from typing import Any

import jwt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.db.system_tables import CollectionRecord, SuperuserRecord
from ppbase.services.auth_service import create_token, get_collection_token_config


async def get_superusers_collection(
    session: AsyncSession,
) -> CollectionRecord | None:
    """Return the collection that owns superuser token configuration."""
    statement = select(CollectionRecord).where(
        CollectionRecord.name == "_superusers"
    )
    return (await session.execute(statement)).scalars().first()


async def _get_auth_record_token_key(
    session: AsyncSession,
    collection: CollectionRecord,
    record_id: str,
) -> str | None:
    statement = text(
        f'SELECT "token_key" FROM "{collection.name}" '
        'WHERE "id" = :rid LIMIT 1'
    )
    result = await session.execute(statement, {"rid": record_id})
    row = result.mappings().first()
    if row is None:
        return None
    token_key = row.get("token_key")
    return str(token_key) if token_key is not None else None


async def create_file_token_for_auth(
    session: AsyncSession,
    auth_payload: dict[str, Any],
) -> str:
    """Create a file-purpose token for one verified auth payload."""
    auth_type = str(auth_payload.get("type", "") or "")
    auth_id = str(auth_payload.get("id", "") or "")
    if not auth_type or not auth_id:
        raise ValueError("Invalid auth payload.")

    if auth_type == "admin":
        admin = await session.get(SuperuserRecord, auth_id)
        if admin is None:
            raise ValueError("Missing superuser.")

        superusers_collection = await get_superusers_collection(session)
        if superusers_collection is None:
            raise ValueError("Missing _superusers collection.")

        file_secret, file_duration = get_collection_token_config(
            superusers_collection,
            "fileToken",
        )
        payload = {
            "id": admin.id,
            "type": "admin",
            "for": "file",
        }
        return create_token(
            payload,
            str(admin.token_key) + file_secret,
            file_duration,
        )

    if auth_type == "authRecord":
        collection_id = str(auth_payload.get("collectionId", "") or "")
        if not collection_id:
            raise ValueError("Missing collectionId.")

        auth_collection = await session.get(CollectionRecord, collection_id)
        if auth_collection is None:
            raise ValueError("Missing auth collection.")

        token_key = await _get_auth_record_token_key(
            session,
            auth_collection,
            auth_id,
        )
        if not token_key:
            raise ValueError("Missing auth record.")

        file_secret, file_duration = get_collection_token_config(
            auth_collection,
            "fileToken",
        )
        payload = {
            "id": auth_id,
            "type": "authRecord",
            "collectionId": auth_collection.id,
            "for": "file",
        }
        return create_token(
            payload,
            token_key + file_secret,
            file_duration,
        )

    raise ValueError("Unsupported auth token type.")


async def verify_file_token(
    session: AsyncSession,
    file_token: str,
) -> dict[str, Any] | None:
    """Verify a file-purpose token and return its current auth identity."""
    try:
        unverified = jwt.decode(file_token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None

    if unverified.get("for") != "file":
        return None

    token_type = str(unverified.get("type", "") or "")
    token_id = str(unverified.get("id", "") or "")
    if not token_type or not token_id:
        return None

    if token_type == "admin":
        admin = await session.get(SuperuserRecord, token_id)
        if admin is None:
            return None

        superusers_collection = await get_superusers_collection(session)
        if superusers_collection is None:
            return None

        file_secret, _ = get_collection_token_config(
            superusers_collection,
            "fileToken",
        )
        try:
            jwt.decode(
                file_token,
                str(admin.token_key) + file_secret,
                algorithms=["HS256"],
            )
        except jwt.InvalidTokenError:
            return None
        return {
            "id": admin.id,
            "email": admin.email,
            "type": "admin",
        }

    if token_type == "authRecord":
        collection_id = str(unverified.get("collectionId", "") or "")
        if not collection_id:
            return None

        auth_collection = await session.get(CollectionRecord, collection_id)
        if auth_collection is None:
            return None

        token_key = await _get_auth_record_token_key(
            session,
            auth_collection,
            token_id,
        )
        if not token_key:
            return None

        file_secret, _ = get_collection_token_config(
            auth_collection,
            "fileToken",
        )
        try:
            jwt.decode(
                file_token,
                token_key + file_secret,
                algorithms=["HS256"],
            )
        except jwt.InvalidTokenError:
            return None
        return {
            "id": token_id,
            "type": "authRecord",
            "collectionId": auth_collection.id,
            "collectionName": auth_collection.name,
        }

    return None
