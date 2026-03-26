"""Business logic for auth collection user operations.

Handles login (auth-with-password), token refresh, email verification,
and password reset for collections of type ``auth``.
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.db.system_tables import CollectionRecord
from ppbase.models.field_types import _EMAIL_RE
from ppbase.services.auth_service import (
    create_email_change_token,
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

_OTP_ALPHABET = string.digits


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


def _is_password_auth_enabled(collection: CollectionRecord) -> bool:
    """Return whether password auth is enabled for the collection."""
    opts = collection.options or {}
    pa = opts.get("passwordAuth", {}) or {}
    return bool(pa.get("enabled", True))


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


def _otp_config(collection: CollectionRecord) -> tuple[int, int]:
    """Return ``(duration_seconds, length)`` for OTP auth."""
    opts = collection.options or {}
    otp = opts.get("otp", {}) or {}
    duration = int(otp.get("duration", 180) or 180)
    length = int(otp.get("length", 8) or 8)
    if duration < 1:
        duration = 180
    if length < 4:
        length = 8
    return duration, length


def _generate_otp_password(length: int) -> str:
    return "".join(secrets.choice(_OTP_ALPHABET) for _ in range(length))


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

    For _superusers collection, returns an admin token (type: "admin").
    For other auth collections, returns a record token (type: "authRecord").

    Returns ``{"token": ..., "record": ...}`` on success, or ``None`` on
    bad credentials.
    """
    from ppbase.models.record import build_record_response

    if not _is_password_auth_enabled(collection):
        return None

    fields = _get_identity_fields(collection)
    if not fields:
        fields = ["email"]

    if identity_field and identity_field not in fields:
        return None

    field = identity_field or fields[0]
    if not field.replace("_", "").isalnum():
        return None

    row = await _get_raw_record_by_field(engine, collection, field, identity)
    if row is None:
        return None

    pw_hash = row.get("password_hash", "")
    if not pw_hash or not verify_password(password, pw_hash):
        return None

    # _superusers collection uses admin tokens
    if collection.name == "_superusers":
        from ppbase.services.auth_service import create_admin_token
        from ppbase.db.system_tables import SuperuserRecord

        # Build a mock admin record object
        class MockAdminRecord:
            def __init__(self, row_dict):
                self.id = row_dict.get("id", "")
                self.token_key = row_dict.get("token_key", "")

        mock_admin = MockAdminRecord(row)
        token = create_admin_token(mock_admin, settings, superusers_collection=collection)
    else:
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
# OTP auth
# ---------------------------------------------------------------------------


async def request_otp(
    engine: AsyncEngine,
    collection: CollectionRecord,
    email: str,
    settings: Any,
) -> tuple[str, bool]:
    """Create an OTP request for an email and send/log the password.

    Returns:
        (otp_id, rate_limited)
    """
    normalized_email = email.strip()
    otp_id = ""

    row = await _get_raw_record_by_field(engine, collection, "email", normalized_email)
    if row is None:
        # Enumeration protection: always return an otpId.
        from ppbase.core.id_generator import generate_id

        return generate_id(), False

    duration, length = _otp_config(collection)
    now = datetime.now(timezone.utc)
    cooldown_start = now.timestamp() - 60

    # Basic flood control per record/email.
    rate_sql = (
        'SELECT 1 FROM "_otps" '
        'WHERE "collectionRef" = :collection_ref '
        'AND "recordRef" = :record_ref '
        'AND "sentTo" = :sent_to '
        'AND EXTRACT(EPOCH FROM "created") >= :cooldown_start '
        "LIMIT 1"
    )
    async with engine.connect() as conn:
        rate_hit = (
            await conn.execute(
                text(rate_sql),
                {
                    "collection_ref": collection.id,
                    "record_ref": row["id"],
                    "sent_to": normalized_email,
                    "cooldown_start": cooldown_start,
                },
            )
        ).first()
    if rate_hit is not None:
        from ppbase.core.id_generator import generate_id

        return generate_id(), True

    # Remove stale OTP rows for this record before inserting a new one.
    prune_sql = (
        'DELETE FROM "_otps" '
        'WHERE "collectionRef" = :collection_ref '
        'AND "recordRef" = :record_ref '
        'AND EXTRACT(EPOCH FROM "created") < :min_epoch'
    )
    min_epoch = now.timestamp() - duration
    async with engine.begin() as conn:
        await conn.execute(
            text(prune_sql),
            {
                "collection_ref": collection.id,
                "record_ref": row["id"],
                "min_epoch": min_epoch,
            },
        )

    from ppbase.core.id_generator import generate_id

    otp_id = generate_id()
    otp_password = _generate_otp_password(length)
    otp_hash = hash_password(otp_password)

    insert_sql = (
        'INSERT INTO "_otps" '
        '("id", "created", "updated", "collectionRef", "recordRef", "password", "sentTo") '
        'VALUES (:id, :created, :updated, :collection_ref, :record_ref, :password, :sent_to)'
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(insert_sql),
            {
                "id": otp_id,
                "created": now,
                "updated": now,
                "collection_ref": collection.id,
                "record_ref": row["id"],
                "password": otp_hash,
                "sent_to": normalized_email,
            },
        )

    from ppbase.services.mail_service import send_otp_email

    await send_otp_email(
        normalized_email,
        otp_id,
        otp_password,
        settings,
    )
    return otp_id, False


async def auth_with_otp(
    engine: AsyncEngine,
    collection: CollectionRecord,
    otp_id: str,
    otp_password: str,
    settings: Any,
) -> dict[str, Any] | None:
    """Authenticate a user using ``otp_id`` and OTP password."""
    from ppbase.models.record import build_record_response

    duration, _ = _otp_config(collection)
    now_epoch = datetime.now(timezone.utc).timestamp()

    sql = (
        'SELECT * FROM "_otps" '
        'WHERE "id" = :otp_id AND "collectionRef" = :collection_ref '
        "LIMIT 1"
    )
    async with engine.connect() as conn:
        result = await conn.execute(
            text(sql),
            {
                "otp_id": otp_id,
                "collection_ref": collection.id,
            },
        )
        otp_row = result.mappings().first()

    if otp_row is None:
        return None

    created = otp_row.get("created")
    if created is None or (now_epoch - created.timestamp()) > duration:
        async with engine.begin() as conn:
            await conn.execute(
                text('DELETE FROM "_otps" WHERE "id" = :otp_id'),
                {"otp_id": otp_id},
            )
        return None

    otp_hash = otp_row.get("password", "")
    if not otp_hash or not verify_password(otp_password, otp_hash):
        return None

    record_id = otp_row.get("recordRef", "")
    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        return None

    # OTP is single-use.
    async with engine.begin() as conn:
        await conn.execute(
            text('DELETE FROM "_otps" WHERE "id" = :otp_id'),
            {"otp_id": otp_id},
        )

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

    For _superusers collection, returns an admin token.
    For other auth collections, returns a record token.

    ``token_payload`` is the decoded JWT.  We re-fetch the record to verify
    it still exists and issue a fresh token.

    Returns ``{"token": ..., "record": ...}`` or ``None``.
    """
    from ppbase.models.record import build_record_response

    # Impersonation tokens are intentionally non-refreshable.
    if token_payload.get("refreshable") is False:
        return None

    record_id = token_payload.get("id")
    if not record_id:
        return None

    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        return None

    # _superusers collection uses admin tokens
    if collection.name == "_superusers":
        from ppbase.services.auth_service import create_admin_token
        from ppbase.db.system_tables import SuperuserRecord

        class MockAdminRecord:
            def __init__(self, row_dict):
                self.id = row_dict.get("id", "")
                self.token_key = row_dict.get("token_key", "")

        mock_admin = MockAdminRecord(row)
        token = create_admin_token(mock_admin, settings, superusers_collection=collection)
    else:
        token = create_record_auth_token(row, collection, settings)

    record = build_record_response(
        row,
        collection.id,
        collection.name,
        collection.schema or [],
        is_auth_collection=True,
    )

    return {"token": token, "record": record}


async def impersonate_auth_record(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    settings: Any,
    *,
    duration: int | None = None,
) -> dict[str, Any] | None:
    """Generate a non-refreshable auth token for an existing auth record.

    For _superusers collection, returns an admin token.
    """
    from ppbase.models.record import build_record_response

    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        return None

    # _superusers collection uses admin tokens
    if collection.name == "_superusers":
        from ppbase.services.auth_service import create_admin_token

        class MockAdminRecord:
            def __init__(self, row_dict):
                self.id = row_dict.get("id", "")
                self.token_key = row_dict.get("token_key", "")

        mock_admin = MockAdminRecord(row)
        token = create_admin_token(
            mock_admin,
            settings,
            superusers_collection=collection,
        )
        # Admin tokens don't support refreshable flag in the same way
        # but we can still make it non-refreshable by setting it in payload
        import jwt as _jwt
        unverified = _jwt.decode(token, options={"verify_signature": False})
        unverified["refreshable"] = False
        # Re-sign with same secret
        secret = mock_admin.token_key
        from ppbase.services.auth_service import get_collection_token_config
        auth_secret, _ = get_collection_token_config(collection, 'authToken')
        secret += auth_secret
        from ppbase.services.auth_service import create_token
        token = create_token(unverified, secret, duration if duration else 1209600)
    else:
        token = create_record_auth_token(
            row,
            collection,
            settings,
            refreshable=False,
            duration_seconds=duration,
        )

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

    For _superusers collection, also accepts admin tokens (type: "admin").

    Returns the decoded payload on success, or ``None`` on failure.
    """
    import jwt as _jwt

    try:
        unverified = _jwt.decode(
            token_str, options={"verify_signature": False}
        )
    except _jwt.InvalidTokenError:
        return None

    token_type = unverified.get("type")

    # For _superusers, accept both admin and authRecord tokens
    if collection.name == "_superusers":
        if token_type not in ("admin", "authRecord"):
            return None
    else:
        # For other collections, only accept authRecord tokens
        if token_type != "authRecord":
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


# ---------------------------------------------------------------------------
# Email change
# ---------------------------------------------------------------------------


def _invalid_email_change_token_error() -> dict[str, Any]:
    return {
        "token": {
            "code": "validation_invalid_token",
            "message": "Invalid or expired token.",
        }
    }


async def request_email_change(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    new_email: str,
    settings: Any,
    base_url: str = "",
) -> tuple[bool, dict[str, Any] | None]:
    """Send an email change confirmation token to ``new_email``."""
    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        return False, {
            "auth": {
                "code": "validation_invalid_token",
                "message": "Invalid or expired token.",
            }
        }

    normalized_email = (new_email or "").strip()
    if not normalized_email:
        return False, {
            "newEmail": {
                "code": "validation_required",
                "message": "Cannot be blank.",
            }
        }

    if not _EMAIL_RE.match(normalized_email):
        return False, {
            "newEmail": {
                "code": "validation_invalid_email",
                "message": "Must be a valid email address.",
            }
        }

    dup = await _get_raw_record_by_field(engine, collection, "email", normalized_email)
    if dup is not None and dup.get("id") != record_id:
        return False, {
            "newEmail": {
                "code": "validation_not_unique",
                "message": "The email is already in use.",
            }
        }

    token_key = row.get("token_key", "")
    secret, duration = get_collection_token_config(collection, 'emailChangeToken')
    token = create_email_change_token(
        row["id"],
        collection.id,
        row.get("email", ""),
        normalized_email,
        token_key + secret,
        duration,
    )

    from ppbase.services.mail_service import send_confirm_email_change_email

    await send_confirm_email_change_email(
        normalized_email,
        token,
        settings,
        base_url=base_url,
    )
    return True, None


async def confirm_email_change(
    engine: AsyncEngine,
    collection: CollectionRecord,
    token_str: str,
    password: str,
    settings: Any,
) -> tuple[bool, dict[str, Any] | None]:
    """Confirm an email change token and update the auth record email."""
    import jwt as _jwt

    try:
        unverified = _jwt.decode(token_str, options={"verify_signature": False})
    except _jwt.InvalidTokenError:
        return False, _invalid_email_change_token_error()

    if unverified.get("type") != "emailChange":
        return False, _invalid_email_change_token_error()

    record_id = unverified.get("id")
    if not record_id:
        return False, _invalid_email_change_token_error()

    row = await _get_raw_record_by_id(engine, collection, record_id)
    if row is None:
        return False, _invalid_email_change_token_error()

    token_key_val = row.get("token_key", "")
    secret, _ = get_collection_token_config(collection, 'emailChangeToken')
    payload = verify_purpose_token(token_str, token_key_val + secret, "emailChange")
    if payload is None:
        return False, _invalid_email_change_token_error()

    # Token is bound to the current record email. If it changed meanwhile,
    # reject the token as stale.
    if payload.get("email") != row.get("email"):
        return False, _invalid_email_change_token_error()

    new_email = str(payload.get("newEmail", "")).strip()
    if not new_email or not _EMAIL_RE.match(new_email):
        return False, _invalid_email_change_token_error()

    pw_hash = row.get("password_hash", "")
    if not pw_hash or not verify_password(password, pw_hash):
        return False, {
            "password": {
                "code": "validation_invalid_credentials",
                "message": "Invalid login credentials.",
            }
        }

    dup = await _get_raw_record_by_field(engine, collection, "email", new_email)
    if dup is not None and dup.get("id") != record_id:
        return False, {
            "newEmail": {
                "code": "validation_not_unique",
                "message": "The email is already in use.",
            }
        }

    await _update_record_columns(
        engine,
        collection,
        record_id,
        {
            "email": new_email,
            "verified": True,
            "token_key": generate_token_key(),
        },
    )
    return True, None
