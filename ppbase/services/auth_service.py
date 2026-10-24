"""JWT and password authentication service."""

from __future__ import annotations

import copy
import secrets
import string
import time
from typing import Any

import bcrypt
import jwt

from ppbase.models.field_types import FieldDefinition, FieldType, validate_field_value
from ppbase.services.legacy_validation import go_string

# Alphabet for token key generation
_TOKEN_KEY_ALPHABET = string.ascii_letters + string.digits
AUTH_TOKEN_TYPE = "auth"
LEGACY_AUTH_TOKEN_TYPES = frozenset({AUTH_TOKEN_TYPE, "authRecord"})
DEFAULT_PASSWORD_MIN_LENGTH = 8
DEFAULT_PASSWORD_MAX_LENGTH = 71
DEFAULT_BCRYPT_COST = 10

_AUTH_SYSTEM_FIELD_DEFAULTS: dict[str, dict[str, Any]] = {
    "id": {
        "id": "text_id",
        "name": "id",
        "type": "text",
        "required": True,
        "system": True,
        "hidden": False,
        "presentable": False,
        "options": {
            "primaryKey": True,
            "min": 15,
            "max": 15,
            "pattern": "^[a-z0-9]+$",
            "autogeneratePattern": "[a-z0-9]{15}",
        },
    },
    "tokenKey": {
        "id": "token_key",
        "name": "tokenKey",
        "type": "text",
        "required": True,
        "system": True,
        "hidden": True,
        "presentable": False,
        "options": {
            "min": 30,
            "max": 60,
            "pattern": "",
            "autogeneratePattern": "[a-zA-Z0-9]{50}",
        },
    },
    "email": {
        "id": "email_field",
        "name": "email",
        "type": "email",
        "required": True,
        "system": True,
        "hidden": False,
        "presentable": False,
        "options": {},
    },
    "emailVisibility": {
        "id": "email_visibility",
        "name": "emailVisibility",
        "type": "bool",
        "required": False,
        "system": True,
        "hidden": False,
        "presentable": False,
        "options": {},
    },
    "verified": {
        "id": "verified",
        "name": "verified",
        "type": "bool",
        "required": False,
        "system": True,
        "hidden": False,
        "presentable": False,
        "options": {},
    },
}


def normalize_auth_password_schema(
    schema: Any,
    options: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Materialize legacy ``minPasswordLength`` into the auth password field.

    PocketBase's pre-v0.23 migration converted the legacy option into the
    system ``password`` field's ``min`` value.  The option is not a runtime
    setting in current PocketBase, so it is consumed once and removed.
    Existing password field definitions always win over the legacy option.
    """
    normalized_schema = copy.deepcopy(schema) if isinstance(schema, list) else []
    normalized_options = copy.deepcopy(options) if isinstance(options, dict) else {}

    legacy_value = normalized_options.pop("minPasswordLength", None)
    password_field_index: int | None = None
    for index, raw in enumerate(normalized_schema):
        if not isinstance(raw, dict):
            continue
        if raw.get("name") == "password" and raw.get("type") == FieldType.PASSWORD:
            password_field_index = index
            break

    if password_field_index is not None:
        normalized_schema[password_field_index] = _normalise_schema_field(
            normalized_schema[password_field_index]
        )
    else:
        try:
            minimum = int(
                legacy_value if legacy_value is not None else DEFAULT_PASSWORD_MIN_LENGTH
            )
        except (TypeError, ValueError):
            minimum = DEFAULT_PASSWORD_MIN_LENGTH
        if minimum < 1 or minimum > DEFAULT_PASSWORD_MAX_LENGTH:
            minimum = DEFAULT_PASSWORD_MIN_LENGTH

        normalized_schema.append(
            {
                "id": "password_field",
                "name": "password",
                "type": "password",
                "required": True,
                "system": True,
                "hidden": True,
                "presentable": False,
                "options": {
                    "min": minimum,
                    "max": 0,
                    "cost": DEFAULT_BCRYPT_COST,
                    "pattern": "",
                },
            }
        )

    normalized_schema = _normalize_auth_system_field_properties(normalized_schema)

    return normalized_schema, normalized_options


def _normalise_field_options(definition: dict[str, Any]) -> dict[str, Any]:
    """Normalise flat PocketBase field options into PPBase's nested shape."""
    core_keys = {
        "id",
        "name",
        "type",
        "required",
        "system",
        "hidden",
        "presentable",
        "options",
    }
    extra = {key: value for key, value in definition.items() if key not in core_keys}
    result = {key: value for key, value in definition.items() if key in core_keys}
    existing = result.pop("options", None)
    if not isinstance(existing, dict):
        existing = {}
    if extra or "options" in definition:
        result["options"] = {**extra, **existing}

    return result


def _normalise_schema_field(definition: dict[str, Any]) -> dict[str, Any]:
    """Normalise and enforce PocketBase's auth password field properties."""
    result = _normalise_field_options(definition)

    # PocketBase's initPasswordField() enforces these system properties on an
    # existing password field while leaving Min/Max/Pattern/Cost untouched.
    # This is deliberately separate from the legacy minPasswordLength import:
    # once a password field exists, that option must never override it.
    result["name"] = "password"
    result["type"] = FieldType.PASSWORD
    result["required"] = True
    result["system"] = True
    result["hidden"] = True
    result["presentable"] = False
    return result


def _normalize_auth_system_field_properties(
    schema: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reimpose PocketBase's immutable auth system-field properties."""
    normalized = copy.deepcopy(schema)

    for index, raw in enumerate(normalized):
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        defaults = _AUTH_SYSTEM_FIELD_DEFAULTS.get(str(name))
        if defaults is None:
            continue

        if raw.get("type") != defaults["type"]:
            normalized[index] = copy.deepcopy(defaults)
            continue

        field = _normalise_field_options(raw)
        options = dict(field.get("options") or {})
        field["system"] = True

        if name == "id":
            field["required"] = True
            field["hidden"] = False
            options["primaryKey"] = True
            if not options.get("pattern"):
                options["pattern"] = "^[a-z0-9]+$"
        elif name == "tokenKey":
            field["required"] = True
            field["hidden"] = True
        elif name == "email":
            field["hidden"] = False

        field["options"] = options
        normalized[index] = field

    return normalized


def get_password_field(collection: Any | None = None) -> FieldDefinition:
    """Return the effective password field definition for an auth collection.

    PocketBase stores password constraints on the system ``password`` field.
    Older PPBase collections don't persist that system field in ``schema``;
    those collections use the same default constraints as PocketBase.
    """
    schema = getattr(collection, "schema", None) if collection is not None else None
    if isinstance(collection, dict):
        schema = collection.get("schema")

    if isinstance(schema, list):
        for raw in schema:
            if not isinstance(raw, dict):
                continue
            # Match the actual PocketBase lookup semantics: resolve the
            # password field by its original name and type first.  Do not
            # normalize an arbitrary earlier business field before deciding
            # whether it is the system password field.
            if raw.get("name") != "password" or raw.get("type") != FieldType.PASSWORD:
                continue
            return FieldDefinition(**_normalise_schema_field(raw))

    return FieldDefinition(
        name="password",
        type=FieldType.PASSWORD,
        required=True,
        system=True,
        hidden=True,
        options={
            "min": DEFAULT_PASSWORD_MIN_LENGTH,
            "max": DEFAULT_PASSWORD_MAX_LENGTH,
        },
    )


def validate_password_value(collection: Any | None, password: Any) -> str:
    """Validate and return a plain password using the collection field rules."""
    return validate_field_value(get_password_field(collection), password)


def get_password_cost(collection: Any | None) -> int:
    """Return the configured bcrypt cost, falling back to bcrypt's default."""
    options = get_password_field(collection).options or {}
    try:
        cost = int(options.get("cost", DEFAULT_BCRYPT_COST))
    except (TypeError, ValueError):
        cost = DEFAULT_BCRYPT_COST
    if cost < 4 or cost > 31:
        return DEFAULT_BCRYPT_COST
    return cost


def hash_password(password: str, *, cost: int | None = None) -> str:
    """Hash a plaintext password using bcrypt.

    The password is hashed as the Go string PocketBase would hash: a lone
    surrogate escape decoded by Python's ``json`` becomes U+FFFD, as with Go's
    JSON decoder (it could not be encoded to UTF-8 otherwise).  bcrypt refuses
    more than 72 bytes (``ValueError``): callers validate the length first.
    """
    pwd_bytes = go_string(password).encode("utf-8")
    salt = bcrypt.gensalt(rounds=cost or DEFAULT_BCRYPT_COST)
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash (Go string, as above)."""
    try:
        return bcrypt.checkpw(go_string(plain).encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def generate_token_key(length: int = 50) -> str:
    """Generate a random token key used for JWT invalidation."""
    return "".join(secrets.choice(_TOKEN_KEY_ALPHABET) for _ in range(length))


def generate_default_auth_options(*, is_superusers: bool = False) -> dict:
    """Generate default PocketBase-compatible auth options with per-collection secrets."""
    return {
        "authAlert": {
            "enabled": True,
            "emailTemplate": {
                "subject": "Login from a new location",
                "body": "We noticed a login to your {APP_NAME} account from a new location: {ALERT_INFO}",
            },
        },
        "authToken": {
            "secret": generate_token_key(50),
            "duration": 86400 if is_superusers else 432000,  # 1 day / 5 days
        },
        "passwordResetToken": {
            "secret": generate_token_key(50),
            "duration": 1800,  # 30 min
        },
        "verificationToken": {
            "secret": generate_token_key(50),
            "duration": 86400,  # 1 day
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
            "duration": 600,
        },
        "otp": {
            "enabled": False,
            "duration": 180,
            "length": 8,
        },
        "authRule": "",
        "manageRule": None,
        "verificationTemplate": {
            "subject": "Verify your {APP_NAME} email",
            "body": "Click on the button below to verify your email address: {TOKEN}",
        },
        "resetPasswordTemplate": {
            "subject": "Reset your {APP_NAME} password",
            "body": "Click on the button below to reset your password: {TOKEN}",
        },
        "confirmEmailChangeTemplate": {
            "subject": "Confirm your {APP_NAME} new email address",
            "body": "Click on the button below to confirm your new email address: {TOKEN}",
        },
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
        'authToken': 432000,        # 5 days
        'passwordResetToken': 1800,  # 30 min
        'verificationToken': 86400, # 1 day
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
    record: Any,
    collection: Any,
    settings: Any = None,
    *,
    refreshable: bool = True,
    duration_seconds: int | None = None,
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
        "type": AUTH_TOKEN_TYPE,
        "collectionId": collection_id,
        "refreshable": bool(refreshable),
    }
    auth_secret, auth_duration = get_collection_token_config(collection, 'authToken')
    secret = token_key + auth_secret
    duration = int(duration_seconds) if duration_seconds is not None else auth_duration
    if duration < 1:
        duration = auth_duration
    return create_token(payload, secret, duration)


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


# PocketBase 0.22 signs every record purpose token (verification, password
# reset, email change, file) with ``type=authRecord``: only the purpose secret
# tells them apart (v0.22.34 tokens/record.go:30-95, tokens/tokens.go:4-7), and
# its confirmations check the signature, never the type
# (forms/record_verification_confirm.go:58-60,
# forms/record_password_reset_confirm.go:59-62,
# forms/record_email_change_confirm.go:103-106).  PPBase issues explicit
# purpose types like PocketBase >= 0.23 and also accepts the 0.22 type, so the
# links e-mailed by PocketBase 0.22 before a migration keep working.
LEGACY_RECORD_TOKEN_TYPE = "authRecord"


def is_purpose_token_type(token_type: Any, expected_type: str) -> bool:
    """Return whether ``token_type`` is the purpose type or the 0.22 type."""
    return token_type in (expected_type, LEGACY_RECORD_TOKEN_TYPE)


def verify_purpose_token(
    token_str: str,
    secret: str,
    expected_type: str,
    *,
    accept_legacy_record_type: bool = False,
) -> dict[str, Any] | None:
    """Decode a purpose-specific JWT and check its ``type`` claim.

    ``secret`` must be the record ``token_key`` plus the purpose secret.  With
    ``accept_legacy_record_type`` a PocketBase 0.22 ``authRecord`` token signed
    with that same key is accepted too.

    Returns the decoded payload dict on success, or ``None`` on failure.
    """
    try:
        payload = verify_token(token_str, secret)
    except jwt.InvalidTokenError:
        return None
    token_type = payload.get("type")
    if accept_legacy_record_type:
        if not is_purpose_token_type(token_type, expected_type):
            return None
    elif token_type != expected_type:
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


def create_email_change_token(
    record_id: str,
    collection_id: str,
    email: str,
    new_email: str,
    secret: str,
    duration: int,
) -> str:
    """Create a JWT for email change confirmation."""
    payload = {
        "id": record_id,
        "collectionId": collection_id,
        "email": email,
        "newEmail": new_email,
        "type": "emailChange",
    }
    return create_token(payload, secret, duration)
