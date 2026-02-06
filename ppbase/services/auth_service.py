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


def create_admin_token(admin_record: Any, settings: Any) -> str:
    """Create an admin JWT token.

    Args:
        admin_record: An ``AdminRecord`` ORM instance.
        settings: Application ``Settings`` instance.

    Returns:
        Encoded JWT string.
    """
    payload = {
        "id": admin_record.id,
        "type": "admin",
    }
    secret = admin_record.token_key + settings.get_jwt_secret()
    return create_token(payload, secret, settings.admin_token_duration)


def create_record_auth_token(
    record: Any, collection: Any, settings: Any
) -> str:
    """Create a record auth JWT token.

    Args:
        record: A dict-like record row with ``id`` and ``token_key``.
        collection: The ``CollectionRecord`` owning the record.
        settings: Application ``Settings`` instance.

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
    secret = token_key + settings.get_jwt_secret()
    return create_token(payload, secret, settings.record_token_duration)
