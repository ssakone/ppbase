"""Business logic for auth collection user operations.

Handles login (auth-with-password), token refresh, email verification,
and password reset for collections of type ``auth``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.db.system_tables import CollectionRecord
from ppbase.services.auth_service import (
    create_password_reset_token,
    create_record_auth_token,
    create_verification_token,
    generate_token_key,
    get_collection_token_config,
    hash_password,
    verify_password,
    verify_purpose_token,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_name(collection: CollectionRecord) -> str:
    return collection.name


def _get_identity_fields(collection: CollectionRecord) -> list[str]:
    """Return the identity fields for an auth collection.

    PocketBase stores this in ``collection.options["passwordAuth"]["identityFields"]``.
    Defaults to ``["email"]``.
    """
    opts = collection.options or {}
    pa = opts.get("passwordAuth", {})
    return pa.get("identityFields", ["email"])


async def _get_raw_record_by_id(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
) -> dict[str, Any] | None:
    """Fetch a raw row (including password_hash, token_key) by ID."""
    table = _table_name(collection)
    sql = f'SELECT * FROM "{table}" WHERE "id" = :id LIMIT 1'
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), {"id": record_id})
        row = result.mappings().first()
    return dict(row) if row else None


async def _get_raw_record_by_field(
    engine: AsyncEngine,
    collection: CollectionRecord,
    field: str,
    value: str,
) -> dict[str, Any] | None:
    """Fetch a raw row by an arbitrary column (e.g. email)."""
    table = _table_name(collection)
    sql = f'SELECT * FROM "{table}" WHERE "{field}" = :val LIMIT 1'
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), {"val": value})
        row = result.mappings().first()
    return dict(row) if row else None


async def _update_record_columns(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    updates: dict[str, Any],
) -> None:
    """Update specific columns on a record."""
    table = _table_name(collection)
    set_clause = ", ".join(f'"{k}" = :{k}' for k in updates)
    sql = f'UPDATE "{table}" SET {set_clause} WHERE "id" = :_rid'
    params = {**updates, "_rid": record_id}
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


# ---------------------------------------------------------------------------
# Auth-with-password
# ---------------------------------------------------------------------------


async def generate_record_auth_token(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    settings: Any,
) -> str:
    """Generate an auth token for a record by ID.

    Args:
        engine: Database engine
        collection: Auth collection
        record_id: Record ID
        settings: App settings

    Returns:
        JWT token string
    """
    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        raise ValueError(f"Record not found: {record_id}")

    return create_record_auth_token(row, collection, settings)


async def auth_with_password(
    engine: AsyncEngine,
    collection: CollectionRecord,
    identity: str,
    password: str,
    settings: Any,
    *,
    identity_field: str | None = None,
) -> dict[str, Any] | None:
    """Authenticate a user by identity + password.

    Returns ``{"token": ..., "record": ...}`` on success, or ``None`` on
    bad credentials.
    """
    from ppbase.models.record import build_record_response

    fields = _get_identity_fields(collection)
    field = identity_field or fields[0]

    row = await _get_raw_record_by_field(engine, collection, field, identity)
    if row is None:
        return None

    pw_hash = row.get("password_hash", "")
    if not pw_hash or not verify_password(password, pw_hash):
        return None

    token = create_record_auth_token(row, collection, settings)

    record = build_record_response(
        row,
        collection.id,
        collection.name,
        collection.schema or [],
        is_auth_collection=True,
    )

    return {"token": token, "record": record}


# ---------------------------------------------------------------------------
# Auth-refresh
# ---------------------------------------------------------------------------


async def auth_refresh(
    engine: AsyncEngine,
    collection: CollectionRecord,
    token_payload: dict[str, Any],
    settings: Any,
) -> dict[str, Any] | None:
    """Refresh an auth token.

    ``token_payload`` is the decoded JWT.  We re-fetch the record to verify
    it still exists and issue a fresh token.

    Returns ``{"token": ..., "record": ...}`` or ``None``.
    """
    from ppbase.models.record import build_record_response

    record_id = token_payload.get("id")
    if not record_id:
        return None

    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        return None

    token = create_record_auth_token(row, collection, settings)

    record = build_record_response(
        row,
        collection.id,
        collection.name,
        collection.schema or [],
        is_auth_collection=True,
    )

    return {"token": token, "record": record}


# ---------------------------------------------------------------------------
# Verify record auth token (full JWT verification with token_key)
# ---------------------------------------------------------------------------


async def verify_record_auth_token(
    engine: AsyncEngine,
    collection: CollectionRecord,
    token_str: str,
    settings: Any,
) -> dict[str, Any] | None:
    """Fully verify a record auth JWT.

    Returns the decoded payload on success, or ``None`` on failure.
    """
    import jwt as _jwt

    try:
        unverified = _jwt.decode(
            token_str, options={"verify_signature": False}
        )
    except _jwt.InvalidTokenError:
        return None

    if unverified.get("type") != "authRecord":
        return None
    if unverified.get("collectionId") != collection.id:
        return None

    record_id = unverified.get("id")
    if not record_id:
        return None

    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        return None

    token_key = row.get("token_key", "")
    auth_secret, _ = get_collection_token_config(collection, 'authToken')
    full_secret = token_key + auth_secret

    try:
        payload = _jwt.decode(token_str, full_secret, algorithms=["HS256"])
    except _jwt.InvalidTokenError:
        return None

    return payload


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


async def request_verification(
    engine: AsyncEngine,
    collection: CollectionRecord,
    email: str,
    settings: Any,
    base_url: str = "",
) -> bool:
    """Send (or log) a verification email.

    Always returns True to avoid email enumeration.
    """
    row = await _get_raw_record_by_field(engine, collection, "email", email)
    if row is None:
        return True  # no enumeration

    if row.get("verified", False):
        return True  # already verified

    token_key = row.get("token_key", "")
    ver_secret, ver_duration = get_collection_token_config(collection, 'verificationToken')
    secret = token_key + ver_secret
    duration = ver_duration

    token = create_verification_token(
        row["id"], collection.id, email, secret, duration
    )

    # Send email or log
    from ppbase.services.mail_service import send_verification_email

    await send_verification_email(email, token, settings, base_url=base_url)
    return True


async def confirm_verification(
    engine: AsyncEngine,
    collection: CollectionRecord,
    token_str: str,
    settings: Any,
) -> bool:
    """Confirm a verification token → set ``verified = true``.

    Returns True on success, False on invalid token.
    """
    # We need to try every possible record in the collection to find the
    # one whose token_key + jwt_secret signed this token.
    import jwt as _jwt

    try:
        unverified = _jwt.decode(
            token_str, options={"verify_signature": False}
        )
    except _jwt.InvalidTokenError:
        return False

    if unverified.get("type") != "verification":
        return False

    record_id = unverified.get("id")
    if not record_id:
        return False

    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        return False

    token_key = row.get("token_key", "")
    ver_secret, _ = get_collection_token_config(collection, 'verificationToken')
    secret = token_key + ver_secret
    payload = verify_purpose_token(token_str, secret, "verification")
    if payload is None:
        return False

    # Check the email in the token matches the record's current email
    if payload.get("email") != row.get("email"):
        return False

    await _update_record_columns(engine, collection, record_id, {"verified": True})
    return True


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


async def request_password_reset(
    engine: AsyncEngine,
    collection: CollectionRecord,
    email: str,
    settings: Any,
    base_url: str = "",
) -> bool:
    """Send (or log) a password reset email.

    Always returns True to avoid email enumeration.
    """
    row = await _get_raw_record_by_field(engine, collection, "email", email)
    if row is None:
        return True  # no enumeration

    token_key = row.get("token_key", "")
    reset_secret, reset_duration = get_collection_token_config(collection, 'passwordResetToken')
    secret = token_key + reset_secret
    duration = reset_duration

    token = create_password_reset_token(
        row["id"], collection.id, email, secret, duration
    )

    from ppbase.services.mail_service import send_password_reset_email

    await send_password_reset_email(email, token, settings, base_url=base_url)
    return True


async def confirm_password_reset(
    engine: AsyncEngine,
    collection: CollectionRecord,
    token_str: str,
    password: str,
    password_confirm: str,
    settings: Any,
) -> tuple[bool, dict[str, Any] | None]:
    """Confirm a password reset token → update password.

    Returns ``(success, errors_dict_or_none)``.
    """
    if password != password_confirm:
        return False, {
            "passwordConfirm": {
                "code": "validation_values_mismatch",
                "message": "Values don't match.",
            }
        }

    if len(password) < 8:
        return False, {
            "password": {
                "code": "validation_length_out_of_range",
                "message": "The length must be between 8 and 72.",
            }
        }

    import jwt as _jwt

    try:
        unverified = _jwt.decode(
            token_str, options={"verify_signature": False}
        )
    except _jwt.InvalidTokenError:
        return False, {
            "token": {
                "code": "validation_invalid_token",
                "message": "Invalid or expired token.",
            }
        }

    if unverified.get("type") != "passwordReset":
        return False, {
            "token": {
                "code": "validation_invalid_token",
                "message": "Invalid or expired token.",
            }
        }

    record_id = unverified.get("id")
    if not record_id:
        return False, {
            "token": {
                "code": "validation_invalid_token",
                "message": "Invalid or expired token.",
            }
        }

    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        return False, {
            "token": {
                "code": "validation_invalid_token",
                "message": "Invalid or expired token.",
            }
        }

    token_key_val = row.get("token_key", "")
    reset_secret, _ = get_collection_token_config(collection, 'passwordResetToken')
    secret = token_key_val + reset_secret
    payload = verify_purpose_token(token_str, secret, "passwordReset")
    if payload is None:
        return False, {
            "token": {
                "code": "validation_invalid_token",
                "message": "Invalid or expired token.",
            }
        }

    # Check the email in the token matches the record
    if payload.get("email") != row.get("email"):
        return False, {
            "token": {
                "code": "validation_invalid_token",
                "message": "Invalid or expired token.",
            }
        }

    new_hash = hash_password(password)
    new_token_key = generate_token_key()

    await _update_record_columns(
        engine, collection, record_id,
        {"password_hash": new_hash, "token_key": new_token_key},
    )
    return True, None
