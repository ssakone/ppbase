"""JWT and password authentication service."""

from __future__ import annotations

import secrets
import string
import time
from typing import Any

import bcrypt
import jwt

# Alphabet for token key generation
_TOKEN_KEY_ALPHABET = string.ascii_letters + string.digits


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    pwd_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def generate_token_key(length: int = 50) -> str:
    """Generate a random token key used for JWT invalidation."""
    return "".join(secrets.choice(_TOKEN_KEY_ALPHABET) for _ in range(length))


def generate_default_auth_options(*, is_superusers: bool = False) -> dict:
    """Generate default PocketBase-compatible auth options with per-collection secrets."""
    return {
        "authToken": {
            "secret": generate_token_key(50),
            "duration": 86400 if is_superusers else 604800,  # 1 day / 7 days
        },
        "passwordResetToken": {
            "secret": generate_token_key(50),
            "duration": 1800,  # 30 min
        },
        "verificationToken": {
            "secret": generate_token_key(50),
            "duration": 259200,  # 3 days
        },
        "emailChangeToken": {
            "secret": generate_token_key(50),
            "duration": 1800,
        },
        "fileToken": {
            "secret": generate_token_key(50),
            "duration": 180,  # 3 min
        },
        "passwordAuth": {
            "enabled": True,
            "identityFields": ["email"],
        },
        "oauth2": {
            "enabled": False,
            "providers": [],
            "mappedFields": {},
        },
        "mfa": {
            "enabled": False,
            "duration": 1800,
        },
        "otp": {
            "enabled": False,
            "duration": 180,
            "length": 8,
        },
        "authRule": "",
        "manageRule": None,
    }


def get_collection_token_config(collection, token_type: str) -> tuple[str, int]:
    """Extract (secret, duration) from collection.options for a token type.

    Args:
        collection: A CollectionRecord or dict-like with ``options``.
        token_type: One of 'authToken', 'passwordResetToken', 'verificationToken',
                    'emailChangeToken', 'fileToken'.

    Returns:
        (secret, duration) tuple. Falls back to empty string / defaults if missing.
    """
    opts = getattr(collection, 'options', None) or {}
    if isinstance(collection, dict):
        opts = collection.get('options', {}) or {}

    token_config = opts.get(token_type, {})
    secret = token_config.get('secret', '')

    # Default durations by token type
    default_durations = {
        'authToken': 604800,        # 7 days
        'passwordResetToken': 1800,  # 30 min
        'verificationToken': 259200, # 3 days
        'emailChangeToken': 1800,
        'fileToken': 180,
    }
    duration = token_config.get('duration', default_durations.get(token_type, 604800))

    return secret, duration


def create_token(payload: dict[str, Any], secret: str, duration: int) -> str:
    """Create a signed JWT token (HS256).

    Args:
        payload: Base claims (id, type, collectionId, etc.).
        secret: Signing secret.
        duration: Token lifetime in seconds.

    Returns:
        Encoded JWT string.
    """
    now = int(time.time())
    claims = {**payload, "iat": now, "exp": now + duration}
    return jwt.encode(claims, secret, algorithm="HS256")


def verify_token(token: str, secret: str) -> dict[str, Any]:
    """Decode and validate a JWT token.

    Returns:
        The decoded payload dict.

    Raises:
        jwt.InvalidTokenError: If the token is invalid or expired.
    """
    return jwt.decode(token, secret, algorithms=["HS256"])


def create_admin_token(
    admin_record: Any, settings: Any = None, *, superusers_collection=None
) -> str:
    """Create an admin JWT token.

    Args:
        admin_record: An ``AdminRecord`` ORM instance.
        settings: Application ``Settings`` instance (fallback).
        superusers_collection: The ``_superusers`` CollectionRecord for
            per-collection token secrets.

    Returns:
        Encoded JWT string.
    """
    payload = {
        "id": admin_record.id,
        "type": "admin",
    }
    if superusers_collection is not None:
        auth_secret, auth_duration = get_collection_token_config(
            superusers_collection, 'authToken'
        )
        secret = admin_record.token_key + auth_secret
        return create_token(payload, secret, auth_duration)
    # Fallback for backward compat (e.g. during bootstrap before collection exists)
    secret = admin_record.token_key + (settings.get_jwt_secret() if settings else '')
    duration = settings.admin_token_duration if settings else 1209600
    return create_token(payload, secret, duration)


def create_record_auth_token(
    record: Any, collection: Any, settings: Any = None
) -> str:
    """Create a record auth JWT token.

    Args:
        record: A dict-like record row with ``id`` and ``token_key``.
        collection: The ``CollectionRecord`` owning the record.
        settings: Unused, kept for backward compatibility.

    Returns:
        Encoded JWT string.
    """
    record_id = record["id"] if isinstance(record, dict) else record.id
    token_key = record["token_key"] if isinstance(record, dict) else record.token_key
    collection_id = collection.id if hasattr(collection, "id") else collection["id"]

    payload = {
        "id": record_id,
        "type": "authRecord",
        "collectionId": collection_id,
    }
    auth_secret, auth_duration = get_collection_token_config(collection, 'authToken')
    secret = token_key + auth_secret
    return create_token(payload, secret, auth_duration)


# ---------------------------------------------------------------------------
# Purpose-specific tokens (verification, password reset)
# ---------------------------------------------------------------------------


def create_verification_token(
    record_id: str,
    collection_id: str,
    email: str,
    secret: str,
    duration: int,
) -> str:
    """Create a JWT for email verification."""
    payload = {
        "id": record_id,
        "collectionId": collection_id,
        "email": email,
        "type": "verification",
    }
    return create_token(payload, secret, duration)


def verify_purpose_token(
    token_str: str,
    secret: str,
    expected_type: str,
) -> dict[str, Any] | None:
    """Decode a purpose-specific JWT and check its ``type`` claim.

    Returns the decoded payload dict on success, or ``None`` on failure.
    """
    try:
        payload = verify_token(token_str, secret)
    except jwt.InvalidTokenError:
        return None
    if payload.get("type") != expected_type:
        return None
    return payload


def create_password_reset_token(
    record_id: str,
    collection_id: str,
    email: str,
    secret: str,
    duration: int,
) -> str:
    """Create a JWT for password reset."""
    payload = {
        "id": record_id,
        "collectionId": collection_id,
        "email": email,
        "type": "passwordReset",
    }
    return create_token(payload, secret, duration)
