"""Models and canonical JSON rules for native backup sets."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypeAlias


BACKUP_FORMAT = "ppbase-backup-set"
BACKUP_FORMAT_VERSION = 1
DATABASE_DUMP_RESOURCE = "resources/database.dump"
JWT_SECRET_RESOURCE_PATH = "resources/secrets/jwt_secret"

_BACKUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CREATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_JSON_INTEGER_MIN = -(2**63)
_JSON_INTEGER_MAX = 2**63 - 1
_SENSITIVE_METADATA_KEY_FRAGMENTS = (
    "accesskey",
    "credential",
    "databaseurl",
    "dsn",
    "jwtsecret",
    "passfile",
    "passwd",
    "password",
    "pgpass",
    "privatekey",
    "secret",
    "tokenkey",
)

JsonScalar: TypeAlias = None | bool | int | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def _immutable_collection(*_args: Any, **_kwargs: Any) -> None:
    raise TypeError("signed manifest metadata is immutable")


class _FrozenDict(dict[str, JsonValue]):
    __setitem__ = _immutable_collection
    __delitem__ = _immutable_collection
    clear = _immutable_collection
    pop = _immutable_collection
    popitem = _immutable_collection
    setdefault = _immutable_collection
    update = _immutable_collection
    __ior__ = _immutable_collection


class _FrozenList(list[JsonValue]):
    __setitem__ = _immutable_collection
    __delitem__ = _immutable_collection
    append = _immutable_collection
    clear = _immutable_collection
    extend = _immutable_collection
    insert = _immutable_collection
    pop = _immutable_collection
    remove = _immutable_collection
    reverse = _immutable_collection
    sort = _immutable_collection
    __iadd__ = _immutable_collection
    __imul__ = _immutable_collection


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _FrozenDict(
            {key: _freeze_json(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return _FrozenList(_freeze_json(child) for child in value)
    return value


class BackupError(RuntimeError):
    """Base error raised by the native backup subsystem."""


class BackupManifestError(BackupError):
    """Raised when a manifest is malformed or is not canonical JSON."""


class BackupIntegrityError(BackupError):
    """Raised when a sealed backup fails signature or checksum validation."""


class BackupNotFoundError(BackupError):
    """Raised when a requested sealed backup set does not exist."""


class BackupAlreadyExistsError(BackupError):
    """Raised when publication would reuse an existing backup identifier."""


class BackupStateError(BackupError):
    """Raised when a backup builder is used in an invalid phase."""


class BackupUnsafeSourceError(BackupError):
    """Raised when local storage contains links or special files."""


def validate_backup_id(value: str) -> str:
    """Return a backup identifier that is safe as one path component."""
    if not isinstance(value, str) or not _BACKUP_ID_PATTERN.fullmatch(value):
        raise BackupManifestError(
            "backup_id must be 1-128 ASCII letters, digits, '.', '_' or '-' "
            "and must start with a letter or digit"
        )
    if value in {".", ".."}:
        raise BackupManifestError("backup_id cannot be a relative path segment")
    return value


def format_manifest_timestamp(value: datetime) -> str:
    """Format a timestamp in the single UTC representation accepted by v1."""
    if value.tzinfo is None:
        raise BackupManifestError("manifest timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime(_CREATED_AT_FORMAT)


def _validate_manifest_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise BackupManifestError("created_at must be a string")
    try:
        parsed = datetime.strptime(value, _CREATED_AT_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise BackupManifestError(
            "created_at must use canonical UTC form YYYY-MM-DDTHH:MM:SS.ffffffZ"
        ) from exc
    if format_manifest_timestamp(parsed) != value:
        raise BackupManifestError("created_at is not canonical")
    return value


def _normalize_json(value: Any, *, path: str = "$") -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not _JSON_INTEGER_MIN <= value <= _JSON_INTEGER_MAX:
            raise BackupManifestError(f"integer is outside signed 64-bit range at {path}")
        return value
    if isinstance(value, float):
        raise BackupManifestError(f"floating-point value is forbidden at {path}")
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        try:
            normalized.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise BackupManifestError(f"invalid UTF-8 string at {path}") from exc
        return normalized
    if isinstance(value, Mapping):
        normalized_object: dict[str, JsonValue] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise BackupManifestError(f"non-string object key at {path}")
            key = unicodedata.normalize("NFC", raw_key)
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise BackupManifestError(
                    f"invalid UTF-8 object key at {path}"
                ) from exc
            if key in normalized_object:
                raise BackupManifestError(
                    f"duplicate object key after NFC normalization at {path}.{key}"
                )
            normalized_object[key] = _normalize_json(
                raw_value,
                path=f"{path}.{key}",
            )
        return normalized_object
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray, memoryview),
    ):
        return [
            _normalize_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise BackupManifestError(
        f"unsupported JSON value {type(value).__name__} at {path}"
    )


def _reject_sensitive_metadata(value: JsonValue, *, path: str = "$.metadata") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            folded_key = "".join(
                character for character in key.casefold() if character.isalnum()
            )
            if folded_key == "jwtsecret" and isinstance(child, dict):
                if set(child) == {"mode"} and child["mode"] in {
                    "external_required",
                    "included_resource",
                }:
                    continue
            if any(
                fragment in folded_key
                for fragment in _SENSITIVE_METADATA_KEY_FRAGMENTS
            ):
                raise BackupManifestError(
                    f"sensitive field is forbidden in manifest metadata at {path}.{key}"
                )
            _reject_sensitive_metadata(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_metadata(child, path=f"{path}[{index}]")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the strict, NFC-normalized canonical JSON used for signatures."""
    normalized = _normalize_json(value)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BackupManifestError("value cannot be encoded as canonical JSON") from exc
    return encoded


def _reject_float(value: str) -> Any:
    raise BackupManifestError(f"floating-point JSON number is forbidden: {value}")


def _reject_constant(value: str) -> Any:
    raise BackupManifestError(f"non-finite JSON number is forbidden: {value}")


def _parse_integer(value: str) -> int:
    digits = value.removeprefix("-")
    if len(digits) > 19:
        raise BackupManifestError("JSON integer is outside signed 64-bit range")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise BackupManifestError("JSON integer is invalid") from exc
    if not _JSON_INTEGER_MIN <= parsed <= _JSON_INTEGER_MAX:
        raise BackupManifestError("JSON integer is outside signed 64-bit range")
    return parsed


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupManifestError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def parse_canonical_json(payload: bytes) -> JsonValue:
    """Decode JSON and reject every byte representation that is not canonical."""
    if not isinstance(payload, bytes):
        raise BackupManifestError("canonical JSON payload must be bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackupManifestError("manifest is not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_float=_reject_float,
            parse_int=_parse_integer,
            parse_constant=_reject_constant,
        )
    except BackupManifestError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BackupManifestError("manifest is not valid JSON") from exc

    normalized = _normalize_json(value)
    if canonical_json_bytes(normalized) != payload:
        raise BackupManifestError("manifest JSON is not in canonical form")
    return normalized


def _validate_resource_path(value: Any) -> str:
    if not isinstance(value, str):
        raise BackupManifestError("resource path must be a string")
    if value != unicodedata.normalize("NFC", value):
        raise BackupManifestError("resource path must be NFC-normalized")
    if "\\" in value or any(unicodedata.category(char) == "Cc" for char in value):
        raise BackupManifestError("resource path contains unsafe characters")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "resources"
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise BackupManifestError("resource path must stay below resources/")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BackupManifestError("resource path is not valid UTF-8") from exc
    return value


@dataclass(frozen=True, slots=True)
class BackupResource:
    """One immutable file covered by the manifest checksum list."""

    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validate_resource_path(self.path))
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise BackupManifestError("resource size must be a non-negative integer")
        if not isinstance(self.sha256, str) or not _SHA256_PATTERN.fullmatch(
            self.sha256
        ):
            raise BackupManifestError(
                "resource sha256 must be 64 lowercase hexadecimal characters"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "BackupResource":
        if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "size"}:
            raise BackupManifestError("resource has unknown or missing fields")
        return cls(
            path=value["path"],
            sha256=value["sha256"],
            size=value["size"],
        )


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Versioned signed description of a canonical local backup set."""

    backup_id: str
    created_at: str
    signer_fingerprint_sha256: str
    resources: tuple[BackupResource, ...]
    metadata: Mapping[str, JsonValue]
    format: str = BACKUP_FORMAT
    format_version: int = BACKUP_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "backup_id", validate_backup_id(self.backup_id))
        object.__setattr__(
            self,
            "created_at",
            _validate_manifest_timestamp(self.created_at),
        )
        if self.format != BACKUP_FORMAT:
            raise BackupManifestError(f"unsupported backup format: {self.format}")
        if (
            isinstance(self.format_version, bool)
            or self.format_version != BACKUP_FORMAT_VERSION
        ):
            raise BackupManifestError(
                f"unsupported backup format version: {self.format_version}"
            )
        if not isinstance(self.signer_fingerprint_sha256, str) or not (
            _SHA256_PATTERN.fullmatch(self.signer_fingerprint_sha256)
        ):
            raise BackupManifestError("signer fingerprint must be a SHA-256 hex value")

        resources = tuple(self.resources)
        if any(not isinstance(resource, BackupResource) for resource in resources):
            raise BackupManifestError("manifest resources must be resource objects")
        paths = [resource.path for resource in resources]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise BackupManifestError(
                "manifest resources must have unique paths in ascending order"
            )
        if DATABASE_DUMP_RESOURCE not in paths:
            raise BackupManifestError("manifest is missing resources/database.dump")
        for path in paths:
            if path == DATABASE_DUMP_RESOURCE or path == JWT_SECRET_RESOURCE_PATH:
                continue
            if path.startswith("resources/files/"):
                continue
            raise BackupManifestError(f"unsupported v1 backup resource: {path}")
        object.__setattr__(self, "resources", resources)

        normalized_metadata = _normalize_json(self.metadata, path="$.metadata")
        if not isinstance(normalized_metadata, dict):
            raise BackupManifestError("manifest metadata must be an object")
        _reject_sensitive_metadata(normalized_metadata)
        object.__setattr__(self, "metadata", _freeze_json(normalized_metadata))

    @property
    def total_size(self) -> int:
        return sum(resource.size for resource in self.resources)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "format": self.format,
            "format_version": self.format_version,
            "metadata": dict(self.metadata),
            "resources": [resource.to_dict() for resource in self.resources],
            "signing": {
                "algorithm": "Ed25519",
                "fingerprint_sha256": self.signer_fingerprint_sha256,
            },
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> "BackupManifest":
        value = parse_canonical_json(payload)
        if not isinstance(value, Mapping):
            raise BackupManifestError("manifest root must be a JSON object")
        expected_fields = {
            "backup_id",
            "created_at",
            "format",
            "format_version",
            "metadata",
            "resources",
            "signing",
        }
        if set(value) != expected_fields:
            raise BackupManifestError("manifest has unknown or missing fields")

        signing = value["signing"]
        if not isinstance(signing, Mapping) or set(signing) != {
            "algorithm",
            "fingerprint_sha256",
        }:
            raise BackupManifestError("manifest signing descriptor is invalid")
        if signing["algorithm"] != "Ed25519":
            raise BackupManifestError("manifest signing algorithm must be Ed25519")

        raw_resources = value["resources"]
        if not isinstance(raw_resources, list):
            raise BackupManifestError("manifest resources must be an array")
        resources = tuple(
            BackupResource.from_dict(resource) for resource in raw_resources
        )
        return cls(
            backup_id=value["backup_id"],
            created_at=value["created_at"],
            format=value["format"],
            format_version=value["format_version"],
            metadata=value["metadata"],
            resources=resources,
            signer_fingerprint_sha256=signing["fingerprint_sha256"],
        )


@dataclass(frozen=True, slots=True)
class BackupSetSummary:
    """Listing projection that isolates invalid sealed sets."""

    backup_id: str
    created_at: str | None
    signer_fingerprint_sha256: str | None
    resource_count: int | None
    total_size: int | None
    integrity_status: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class BackupInspection:
    """A sealed set whose signature and, optionally, resources were verified."""

    path: Path
    manifest: BackupManifest
    signer_public_key: bytes
    resources_verified: bool
