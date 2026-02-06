"""PocketBase field types, definitions, and value validation.

Each collection stores its schema as a JSON array of field definitions.  This
module provides:

* :class:`FieldType` -- enum of all 14 supported field types.
* :class:`FieldDefinition` -- Pydantic model describing a single field.
* :func:`validate_field_value` -- validates and coerces a raw value against a
  field definition, returning the cleaned value or raising
  :class:`FieldValidationError`.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Field type enum
# ---------------------------------------------------------------------------


class FieldType(str, Enum):
    """All PocketBase field types."""

    TEXT = "text"
    EDITOR = "editor"
    NUMBER = "number"
    BOOL = "bool"
    EMAIL = "email"
    URL = "url"
    DATE = "date"
    AUTODATE = "autodate"
    SELECT = "select"
    FILE = "file"
    RELATION = "relation"
    JSON = "json"
    PASSWORD = "password"
    GEO_POINT = "geoPoint"


# ---------------------------------------------------------------------------
# Field definition
# ---------------------------------------------------------------------------


class FieldDefinition(BaseModel):
    """Schema definition for a single collection field."""

    id: str = ""
    name: str
    type: FieldType
    required: bool = False
    system: bool = False
    hidden: bool = False
    presentable: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation error
# ---------------------------------------------------------------------------


class FieldValidationError(Exception):
    """Raised when a value does not satisfy a field's constraints."""

    def __init__(self, field_name: str, code: str, message: str) -> None:
        self.field_name = field_name
        self.code = code
        self.message = message
        super().__init__(f"{field_name}: {message}")


# ---------------------------------------------------------------------------
# Per-type validators
# ---------------------------------------------------------------------------

# Lightweight email regex -- good enough for application-level gating.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# RFC-3986-ish URL pattern (scheme required).
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _validate_text(field: FieldDefinition, value: Any) -> str:
    val = str(value) if value is not None else ""
    opts = field.options
    min_len = opts.get("min")
    max_len = opts.get("max")
    pattern = opts.get("pattern")

    if field.required and not val:
        raise FieldValidationError(field.name, "validation_required", "Cannot be blank.")
    if not val:
        return val

    if min_len is not None and len(val) < int(min_len):
        raise FieldValidationError(
            field.name,
            "validation_min_text_constraint",
            f"Must be at least {min_len} character(s).",
        )
    if max_len is not None and len(val) > int(max_len):
        raise FieldValidationError(
            field.name,
            "validation_max_text_constraint",
            f"Must be at most {max_len} character(s).",
        )
    if pattern and not re.search(pattern, val):
        raise FieldValidationError(
            field.name,
            "validation_invalid_format",
            f"Must match pattern: {pattern}",
        )
    return val


def _validate_editor(field: FieldDefinition, value: Any) -> str:
    val = str(value) if value is not None else ""
    opts = field.options
    max_size = opts.get("maxSize")

    if field.required and not val:
        raise FieldValidationError(field.name, "validation_required", "Cannot be blank.")
    if not val:
        return val

    if max_size is not None and len(val.encode()) > int(max_size):
        raise FieldValidationError(
            field.name,
            "validation_max_size_constraint",
            f"Must be at most {max_size} byte(s).",
        )
    return val


def _validate_number(field: FieldDefinition, value: Any) -> int | float:
    try:
        val = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        raise FieldValidationError(
            field.name, "validation_not_a_number", "Must be a valid number."
        )
    if math.isnan(val) or math.isinf(val):
        raise FieldValidationError(
            field.name, "validation_not_a_number", "NaN and Inf are not allowed."
        )

    opts = field.options
    only_int = opts.get("onlyInt", False)

    if field.required and val == 0:
        raise FieldValidationError(field.name, "validation_required", "Cannot be blank.")

    if only_int and val != float(int(val)):
        raise FieldValidationError(
            field.name, "validation_only_int", "Must be an integer value."
        )

    min_val = opts.get("min")
    max_val = opts.get("max")
    if min_val is not None and val < float(min_val):
        raise FieldValidationError(
            field.name,
            "validation_min_number_constraint",
            f"Must be >= {min_val}.",
        )
    if max_val is not None and val > float(max_val):
        raise FieldValidationError(
            field.name,
            "validation_max_number_constraint",
            f"Must be <= {max_val}.",
        )

    return int(val) if only_int else val


def _validate_bool(field: FieldDefinition, value: Any) -> bool:
    val = bool(value) if value is not None else False
    if field.required and not val:
        raise FieldValidationError(
            field.name, "validation_required", "Must be true."
        )
    return val


def _validate_email(field: FieldDefinition, value: Any) -> str:
    val = str(value).strip() if value is not None else ""

    if field.required and not val:
        raise FieldValidationError(field.name, "validation_required", "Cannot be blank.")
    if not val:
        return val

    if not _EMAIL_RE.match(val):
        raise FieldValidationError(
            field.name, "validation_invalid_email", "Must be a valid email address."
        )

    domain = val.rsplit("@", 1)[-1].lower()
    opts = field.options
    only_domains: list[str] = opts.get("onlyDomains", []) or []
    except_domains: list[str] = opts.get("exceptDomains", []) or []

    if only_domains and domain not in [d.lower() for d in only_domains]:
        raise FieldValidationError(
            field.name,
            "validation_invalid_email_domain",
            f"Email domain must be one of: {', '.join(only_domains)}.",
        )
    if except_domains and domain in [d.lower() for d in except_domains]:
        raise FieldValidationError(
            field.name,
            "validation_invalid_email_domain",
            f"Email domain is not allowed.",
        )
    return val


def _validate_url(field: FieldDefinition, value: Any) -> str:
    val = str(value).strip() if value is not None else ""

    if field.required and not val:
        raise FieldValidationError(field.name, "validation_required", "Cannot be blank.")
    if not val:
        return val

    if not _URL_RE.match(val):
        raise FieldValidationError(
            field.name, "validation_invalid_url", "Must be a valid URL."
        )

    # Extract the host portion for domain checks.
    try:
        from urllib.parse import urlparse

        host = urlparse(val).hostname or ""
    except Exception:
        host = ""

    opts = field.options
    only_domains: list[str] = opts.get("onlyDomains", []) or []
    except_domains: list[str] = opts.get("exceptDomains", []) or []

    if only_domains and host.lower() not in [d.lower() for d in only_domains]:
        raise FieldValidationError(
            field.name,
            "validation_invalid_url_domain",
            f"URL domain must be one of: {', '.join(only_domains)}.",
        )
    if except_domains and host.lower() in [d.lower() for d in except_domains]:
        raise FieldValidationError(
            field.name,
            "validation_invalid_url_domain",
            "URL domain is not allowed.",
        )
    return val


def _validate_date(field: FieldDefinition, value: Any) -> str:
    """Validate a date/datetime value.

    Accepts ISO-8601 / RFC-3339 strings or ``datetime`` objects and returns
    a normalised string in ``YYYY-MM-DD HH:MM:SS.fffZ`` format.
    """
    if value is None or value == "":
        if field.required:
            raise FieldValidationError(
                field.name, "validation_required", "Cannot be blank."
            )
        return ""

    if isinstance(value, datetime):
        dt = value
    else:
        val_str = str(value).strip()
        if not val_str:
            if field.required:
                raise FieldValidationError(
                    field.name, "validation_required", "Cannot be blank."
                )
            return ""
        try:
            dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise FieldValidationError(
                field.name,
                "validation_invalid_date",
                "Must be a valid datetime string.",
            )

    opts = field.options
    min_date = opts.get("min")
    max_date = opts.get("max")

    if min_date:
        min_dt = datetime.fromisoformat(str(min_date).replace("Z", "+00:00"))
        if dt < min_dt:
            raise FieldValidationError(
                field.name,
                "validation_min_date_constraint",
                f"Must be >= {min_date}.",
            )
    if max_date:
        max_dt = datetime.fromisoformat(str(max_date).replace("Z", "+00:00"))
        if dt > max_dt:
            raise FieldValidationError(
                field.name,
                "validation_max_date_constraint",
                f"Must be <= {max_date}.",
            )

    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _validate_autodate(field: FieldDefinition, value: Any) -> str:
    """Autodate fields are set automatically; validation is a no-op."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
    if value is None or value == "":
        return ""
    return str(value)


def _validate_select(field: FieldDefinition, value: Any) -> str | list[str]:
    opts = field.options
    allowed: list[str] = opts.get("values", []) or []
    max_select: int = opts.get("maxSelect", 1) or 1
    is_multi = max_select > 1

    if is_multi:
        if value is None:
            val_list: list[str] = []
        elif isinstance(value, list):
            val_list = [str(v) for v in value]
        else:
            val_list = [str(value)]

        if field.required and not val_list:
            raise FieldValidationError(
                field.name, "validation_required", "Cannot be blank."
            )

        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for v in val_list:
            if v not in seen:
                seen.add(v)
                unique.append(v)
        val_list = unique

        if len(val_list) > max_select:
            raise FieldValidationError(
                field.name,
                "validation_max_select_constraint",
                f"Must select at most {max_select} value(s).",
            )
        for v in val_list:
            if allowed and v not in allowed:
                raise FieldValidationError(
                    field.name,
                    "validation_invalid_select_value",
                    f"'{v}' is not an allowed value.",
                )
        return val_list
    else:
        val = str(value) if value is not None else ""
        if field.required and not val:
            raise FieldValidationError(
                field.name, "validation_required", "Cannot be blank."
            )
        if val and allowed and val not in allowed:
            raise FieldValidationError(
                field.name,
                "validation_invalid_select_value",
                f"'{val}' is not an allowed value.",
            )
        return val


def _validate_file(field: FieldDefinition, value: Any) -> str | list[str]:
    """Validate file field (filename strings).

    Actual file upload handling is done at the service layer.  Here we only
    validate the stored filename references.
    """
    opts = field.options
    max_select: int = opts.get("maxSelect", 1) or 1
    is_multi = max_select > 1

    if is_multi:
        if value is None:
            val_list: list[str] = []
        elif isinstance(value, list):
            val_list = [str(v) for v in value if v]
        else:
            val_list = [str(value)] if value else []

        if field.required and not val_list:
            raise FieldValidationError(
                field.name, "validation_required", "Cannot be blank."
            )
        if len(val_list) > max_select:
            raise FieldValidationError(
                field.name,
                "validation_max_select_constraint",
                f"Must select at most {max_select} file(s).",
            )
        return val_list
    else:
        val = str(value) if value is not None else ""
        if field.required and not val:
            raise FieldValidationError(
                field.name, "validation_required", "Cannot be blank."
            )
        return val


def _validate_relation(field: FieldDefinition, value: Any) -> str | list[str]:
    """Validate relation references (record IDs).

    Existence checks against the target collection happen at the service layer.
    """
    opts = field.options
    max_select: int = opts.get("maxSelect", 1) or 1
    is_multi = max_select > 1

    if is_multi:
        if value is None:
            val_list: list[str] = []
        elif isinstance(value, list):
            val_list = [str(v) for v in value if v]
        else:
            val_list = [str(value)] if value else []

        if field.required and not val_list:
            raise FieldValidationError(
                field.name, "validation_required", "Cannot be blank."
            )
        if len(val_list) > max_select:
            raise FieldValidationError(
                field.name,
                "validation_max_select_constraint",
                f"Must select at most {max_select} relation(s).",
            )
        return val_list
    else:
        val = str(value) if value is not None else ""
        if field.required and not val:
            raise FieldValidationError(
                field.name, "validation_required", "Cannot be blank."
            )
        return val


def _validate_json(field: FieldDefinition, value: Any) -> Any:
    """Validate a JSON field value.

    The value can be any JSON-serialisable object (dict, list, str, int, bool,
    None).  The only constraint is an optional ``maxSize``.
    """
    import json as _json

    if field.required and value is None:
        raise FieldValidationError(field.name, "validation_required", "Cannot be blank.")

    if value is None:
        return None

    opts = field.options
    max_size = opts.get("maxSize")
    if max_size is not None:
        encoded = _json.dumps(value, separators=(",", ":"))
        if len(encoded.encode()) > int(max_size):
            raise FieldValidationError(
                field.name,
                "validation_max_size_constraint",
                f"Must be at most {max_size} byte(s).",
            )
    return value


def _validate_password(field: FieldDefinition, value: Any) -> str:
    """Validate a plain-text password against field constraints.

    Hashing is performed by the auth service, not here.
    """
    val = str(value) if value is not None else ""
    opts = field.options
    min_len = opts.get("min", 8)
    max_len = opts.get("max", 71)  # bcrypt limit
    pattern = opts.get("pattern")

    if field.required and not val:
        raise FieldValidationError(field.name, "validation_required", "Cannot be blank.")
    if not val:
        return val

    if min_len is not None and len(val) < int(min_len):
        raise FieldValidationError(
            field.name,
            "validation_min_text_constraint",
            f"Must be at least {min_len} character(s).",
        )
    if max_len is not None and len(val) > int(max_len):
        raise FieldValidationError(
            field.name,
            "validation_max_text_constraint",
            f"Must be at most {max_len} character(s).",
        )
    if pattern and not re.search(pattern, val):
        raise FieldValidationError(
            field.name,
            "validation_invalid_format",
            f"Must match pattern: {pattern}",
        )
    return val


def _validate_geo_point(field: FieldDefinition, value: Any) -> dict[str, float]:
    """Validate a GeoPoint value (``{"lon": float, "lat": float}``)."""
    default: dict[str, float] = {"lon": 0.0, "lat": 0.0}

    if value is None:
        if field.required:
            raise FieldValidationError(
                field.name, "validation_required", "Cannot be blank."
            )
        return default

    if not isinstance(value, dict):
        raise FieldValidationError(
            field.name,
            "validation_invalid_geo_point",
            'Must be an object with "lon" and "lat" keys.',
        )

    try:
        lon = float(value.get("lon", 0))
        lat = float(value.get("lat", 0))
    except (TypeError, ValueError):
        raise FieldValidationError(
            field.name,
            "validation_invalid_geo_point",
            '"lon" and "lat" must be numbers.',
        )

    if not (-180 <= lon <= 180):
        raise FieldValidationError(
            field.name,
            "validation_invalid_geo_point",
            '"lon" must be between -180 and 180.',
        )
    if not (-90 <= lat <= 90):
        raise FieldValidationError(
            field.name,
            "validation_invalid_geo_point",
            '"lat" must be between -90 and 90.',
        )

    if field.required and lon == 0 and lat == 0:
        raise FieldValidationError(
            field.name,
            "validation_required",
            "A non-zero coordinate is required.",
        )

    return {"lon": lon, "lat": lat}


# ---------------------------------------------------------------------------
# Validator dispatch
# ---------------------------------------------------------------------------

_VALIDATORS: dict[FieldType, Any] = {
    FieldType.TEXT: _validate_text,
    FieldType.EDITOR: _validate_editor,
    FieldType.NUMBER: _validate_number,
    FieldType.BOOL: _validate_bool,
    FieldType.EMAIL: _validate_email,
    FieldType.URL: _validate_url,
    FieldType.DATE: _validate_date,
    FieldType.AUTODATE: _validate_autodate,
    FieldType.SELECT: _validate_select,
    FieldType.FILE: _validate_file,
    FieldType.RELATION: _validate_relation,
    FieldType.JSON: _validate_json,
    FieldType.PASSWORD: _validate_password,
    FieldType.GEO_POINT: _validate_geo_point,
}


def validate_field_value(field_def: FieldDefinition, value: Any) -> Any:
    """Validate and coerce *value* according to *field_def*.

    Args:
        field_def: The field schema definition.
        value: Raw value to validate.

    Returns:
        The cleaned / normalised value.

    Raises:
        FieldValidationError: If the value violates the field's constraints.
    """
    validator = _VALIDATORS.get(field_def.type)
    if validator is None:
        raise FieldValidationError(
            field_def.name,
            "validation_unknown_field_type",
            f"Unknown field type: {field_def.type}",
        )
    return validator(field_def, value)
