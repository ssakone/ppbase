"""Validation helpers for storage path components.

Storage identifiers are logical PocketBase/PPBase values, but the local
backend also uses them as individual filesystem path components.  Keep the
validation in one place so local paths and S3 object keys enforce the same
rules without silently normalizing attacker-controlled input.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit


MAX_STORAGE_IDENTIFIER_LENGTH = 15
MAX_STORAGE_FILENAME_BYTES = 255

_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_PATH_SEPARATORS = frozenset({"/", "\\"})


class StorageSafetyError(ValueError):
    """Raised when an unsafe storage identifier or filename is supplied."""


def _has_control_character(value: str) -> bool:
    return any(
        unicodedata.category(char) in {"Cc", "Cs"}
        for char in value
    )


def _validate_single_component(
    value: Any,
    *,
    label: str,
    max_length: int | None = None,
    max_utf8_bytes: int | None = None,
    reject_windows_drive_prefix: bool = False,
) -> str:
    """Validate a single, non-normalized filesystem/object-key component."""
    if not isinstance(value, str):
        raise StorageSafetyError(f"{label} must be a string.")
    if not value or value != value.strip():
        raise StorageSafetyError(f"{label} must be a non-empty trimmed value.")
    if value in {".", ".."}:
        raise StorageSafetyError(f"{label} cannot be a relative path segment.")
    if any(separator in value for separator in _PATH_SEPARATORS):
        raise StorageSafetyError(f"{label} cannot contain path separators.")
    if reject_windows_drive_prefix and _WINDOWS_DRIVE_PREFIX.match(value):
        raise StorageSafetyError(f"{label} cannot contain a drive prefix.")
    if _has_control_character(value):
        raise StorageSafetyError(f"{label} cannot contain control characters.")
    if max_length is not None and len(value) > max_length:
        raise StorageSafetyError(
            f"{label} must contain at most {max_length} characters."
        )
    if max_utf8_bytes is not None:
        try:
            encoded_value = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise StorageSafetyError(f"{label} must be valid UTF-8 text.") from exc
        if len(encoded_value) > max_utf8_bytes:
            raise StorageSafetyError(
                f"{label} must contain at most {max_utf8_bytes} UTF-8 bytes."
            )
    return value


def validate_collection_id(value: Any) -> str:
    """Return a safe PPBase/PocketBase collection ID path component."""
    return _validate_single_component(
        value,
        label="collection_id",
        max_length=MAX_STORAGE_IDENTIFIER_LENGTH,
    )


def validate_record_id(value: Any) -> str:
    """Return a safe PPBase/PocketBase record ID path component.

    PPBase intentionally supports safe custom IDs such as ``ab:cd'ef12`` in
    addition to PocketBase's generated lowercase alphanumeric IDs.
    """
    return _validate_single_component(
        value,
        label="record_id",
        max_length=MAX_STORAGE_IDENTIFIER_LENGTH,
    )


def validate_file_reference(value: Any) -> str:
    """Return a safe stored filename reference.

    File references are basenames only.  They are never interpreted as paths
    and must fit within the common local-filesystem component limit.
    """
    return _validate_single_component(
        value,
        label="filename",
        max_utf8_bytes=MAX_STORAGE_FILENAME_BYTES,
        reject_windows_drive_prefix=True,
    )


def is_remote_file_reference(value: Any) -> bool:
    """Return whether ``value`` is a legacy HTTP(S) file-field reference.

    PPBase's OAuth field mapping historically stores provider avatar URLs in a
    file field.  They remain valid record values for compatibility, but every
    storage backend still rejects them as object names or local paths.
    """
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
