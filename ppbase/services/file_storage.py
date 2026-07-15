"""File storage service with local and S3-compatible backends."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import string
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, formatdate
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from ppbase.config import Settings
from ppbase.core.storage_safety import (
    StorageSafetyError,
    is_remote_file_reference,
    validate_collection_id,
    validate_file_reference,
    validate_record_id,
)

_settings: Settings | None = None
_runtime_storage_overrides: dict[str, Any] | None = None
_runtime_lock = Lock()
_s3_client_cache: Any | None = None
_s3_client_cache_key: tuple[Any, ...] | None = None
_legacy_storage_id_cache: dict[
    tuple[int, int, str],
    tuple[str, int, int],
] = {}
_exact_storage_id_cache: OrderedDict[
    tuple[int, int, str],
    tuple[int, int, int, int, int],
] = OrderedDict()
_storage_write_tracker: ContextVar[set[tuple[str, str, str]] | None] = ContextVar(
    "ppbase_storage_write_tracker",
    default=None,
)
_storage_delete_tracker: ContextVar[
    list[tuple[str, str, str, tuple[str, ...]]] | None
] = ContextVar(
    "ppbase_storage_delete_tracker",
    default=None,
)
_storage_config_snapshot: ContextVar[Any] = ContextVar(
    "ppbase_storage_config_snapshot",
    default=None,
)

_SAFE_STEM_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
_SAFE_EXTENSION_PATTERN = re.compile(r"[^A-Za-z0-9]+")
_POCKETBASE_STORAGE_ID_PATTERN = re.compile(r"^[_a-z0-9]+$")
_LOCAL_ID_ESCAPE_PREFIX = "__ppbase_storage_id_v1__"
_ALPHANUM = string.ascii_letters + string.digits
_MAX_SAFE_STEM_LENGTH = 180
_MAX_SAFE_EXTENSION_LENGTH = 16
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_EXACT_STORAGE_ID_CACHE_LIMIT = 8192


@dataclass(frozen=True)
class _OpenedRecordDirectory:
    """Directory descriptors anchored below the configured storage root."""

    collection_fd: int
    record_fd: int
    collection_name: str
    record_name: str


@dataclass
class StorageFileStream:
    """Opened storage object suitable for bounded streaming responses."""

    stream: Any
    content_length: int | None
    backend: str
    config: _StorageConfig | None
    storage_path: Path | None = None
    object_key: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    def close(self) -> None:
        close = getattr(self.stream, "close", None)
        if callable(close):
            close()


class StorageObjectWriteError(OSError):
    """Report proven and ambiguous object outcomes from a failed S3 write."""

    def __init__(
        self,
        message: str,
        *,
        created_filenames: tuple[str, ...] = (),
        ambiguous_filenames: tuple[str, ...] = (),
        cleanup_failed_filenames: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.created_filenames = created_filenames
        self.ambiguous_filenames = ambiguous_filenames
        self.cleanup_failed_filenames = cleanup_failed_filenames


@dataclass(frozen=True)
class _StorageConfig:
    data_dir: str
    backend: str
    s3_endpoint: str
    s3_bucket: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    s3_force_path_style: bool


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_storage_settings(settings: Settings | None) -> None:
    """Bind storage helpers to the active app settings instance."""
    global _settings
    _settings = settings


def _s3_has_required_credentials(values: dict[str, Any]) -> bool:
    return bool(
        str(values.get("s3_bucket", "") or "").strip()
        and str(values.get("s3_access_key", "") or "").strip()
        and str(values.get("s3_secret_key", "") or "").strip()
    )


def _resolve_storage_config() -> _StorageConfig:
    snapshot = _storage_config_snapshot.get()
    if isinstance(snapshot, _StorageConfig):
        return snapshot
    settings = _get_settings()
    values: dict[str, Any] = {
        "data_dir": str(getattr(settings, "data_dir", "./pb_data")),
        "storage_backend": str(getattr(settings, "storage_backend", "local") or "local"),
        "s3_endpoint": str(getattr(settings, "s3_endpoint", "") or "").strip(),
        "s3_bucket": str(getattr(settings, "s3_bucket", "") or "").strip(),
        "s3_region": str(getattr(settings, "s3_region", "") or "").strip(),
        "s3_access_key": str(getattr(settings, "s3_access_key", "") or "").strip(),
        "s3_secret_key": str(getattr(settings, "s3_secret_key", "") or "").strip(),
        "s3_force_path_style": bool(getattr(settings, "s3_force_path_style", False)),
    }

    with _runtime_lock:
        if isinstance(_runtime_storage_overrides, dict):
            values.update(_runtime_storage_overrides)

    backend = str(values.get("storage_backend", "local") or "local").strip().lower()
    if backend not in {"local", "s3"}:
        backend = "local"
    if backend == "s3" and not _s3_has_required_credentials(values):
        backend = "local"

    return _StorageConfig(
        data_dir=str(values.get("data_dir", getattr(settings, "data_dir", "./pb_data"))),
        backend=backend,
        s3_endpoint=str(values.get("s3_endpoint", "") or "").strip(),
        s3_bucket=str(values.get("s3_bucket", "") or "").strip(),
        s3_region=str(values.get("s3_region", "") or "").strip(),
        s3_access_key=str(values.get("s3_access_key", "") or "").strip(),
        s3_secret_key=str(values.get("s3_secret_key", "") or "").strip(),
        s3_force_path_style=bool(values.get("s3_force_path_style", False)),
    )


@contextmanager
def pin_storage_config() -> Iterator[_StorageConfig]:
    """Keep one immutable backend/config selection for a logical operation."""
    config = _resolve_storage_config()
    token = _storage_config_snapshot.set(config)
    try:
        yield config
    finally:
        _storage_config_snapshot.reset(token)


def _clear_s3_client_cache() -> None:
    global _s3_client_cache
    global _s3_client_cache_key
    with _runtime_lock:
        _s3_client_cache = None
        _s3_client_cache_key = None


def clear_runtime_storage_overrides() -> None:
    """Clear runtime storage overrides (fallback to environment settings)."""
    global _runtime_storage_overrides
    with _runtime_lock:
        _runtime_storage_overrides = None
    _clear_s3_client_cache()


def configure_storage_runtime_from_settings_payload(
    settings_value: dict[str, Any] | None,
) -> None:
    """Configure runtime storage backend overrides from settings payload."""
    global _runtime_storage_overrides

    overrides: dict[str, Any] | None = None
    if isinstance(settings_value, dict):
        raw_s3 = settings_value.get("s3")
        if isinstance(raw_s3, dict):
            endpoint = str(raw_s3.get("endpoint", "") or "").strip()
            bucket = str(raw_s3.get("bucket", "") or "").strip()
            region = str(raw_s3.get("region", "") or "").strip()
            access_key = str(raw_s3.get("accessKey", "") or "").strip()
            secret_key = str(raw_s3.get("secret", "") or "").strip()
            enabled_raw = raw_s3.get("enabled")
            enabled = bool(enabled_raw) if enabled_raw is not None else False
            has_any_value = any([endpoint, bucket, region, access_key, secret_key, enabled])

            if has_any_value:
                use_s3 = enabled or bool(bucket and access_key and secret_key)
                overrides = {
                    "storage_backend": "s3" if use_s3 else "local",
                    "s3_endpoint": endpoint,
                    "s3_bucket": bucket,
                    "s3_region": region,
                    "s3_access_key": access_key,
                    "s3_secret_key": secret_key,
                    "s3_force_path_style": bool(raw_s3.get("forcePathStyle", False)),
                }

    with _runtime_lock:
        _runtime_storage_overrides = overrides
    _clear_s3_client_cache()


def get_storage_backend() -> str:
    """Return active storage backend name."""
    return _resolve_storage_config().backend


@contextmanager
def capture_storage_writes(
    targets: set[tuple[str, str, str]],
) -> Iterator[None]:
    """Capture exact successful file writes in the current async context."""
    token = _storage_write_tracker.set(targets)
    try:
        yield
    finally:
        _storage_write_tracker.reset(token)


def _track_storage_writes(
    collection_id: str,
    record_id: str,
    filenames: list[str],
) -> None:
    tracker = _storage_write_tracker.get()
    if tracker is None:
        return
    tracker.update(
        (collection_id, record_id, filename)
        for filename in filenames
    )


@contextmanager
def defer_storage_deletes(
    actions: list[tuple[str, str, str, tuple[str, ...]]],
) -> Iterator[None]:
    """Defer destructive storage changes until a DB transaction commits."""
    token = _storage_delete_tracker.set(actions)
    try:
        yield
    finally:
        _storage_delete_tracker.reset(token)


def flush_deferred_storage_deletes(
    actions: list[tuple[str, str, str, tuple[str, ...]]],
    *,
    preserved_files: set[tuple[str, str, str]],
) -> int:
    """Apply committed deletions without removing final DB references."""
    failures = 0
    for action, collection_id, record_id, filenames in actions:
        try:
            if action == "files":
                deletable = [
                    filename
                    for filename in filenames
                    if (collection_id, record_id, filename)
                    not in preserved_files
                ]
                delete_files(collection_id, record_id, deletable)
            elif action == "all":
                preserved_names = {
                    filename
                    for preserved_collection, preserved_record, filename
                    in preserved_files
                    if preserved_collection == collection_id
                    and preserved_record == record_id
                }
                delete_all_files_except(
                    collection_id,
                    record_id,
                    preserved_names,
                )
            else:  # pragma: no cover - internal invariant
                failures += 1
        except (OSError, StorageSafetyError):
            failures += 1
    return failures


def _get_storage_root(config: _StorageConfig) -> Path:
    """Return the trusted local storage root, rejecting a symlinked root."""
    data_root = Path(config.data_dir).expanduser().resolve(strict=False)
    storage_root = data_root / "storage"
    if storage_root.is_symlink():
        raise StorageSafetyError("The configured storage root cannot be a symlink.")

    resolved_root = storage_root.resolve(strict=False)
    try:
        resolved_root.relative_to(data_root)
    except ValueError as exc:
        raise StorageSafetyError(
            "The configured storage root escapes the data directory."
        ) from exc
    return resolved_root


def _ensure_no_symlink_components(root: Path, candidate: Path) -> None:
    """Reject symlinks at every component below an already trusted root."""
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise StorageSafetyError("Storage path escapes the configured root.") from exc

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise StorageSafetyError("Symlinks are not allowed in storage paths.")


def _confine_storage_path(root: Path, candidate: Path) -> Path:
    """Resolve and confine a candidate path below ``root``."""
    _ensure_no_symlink_components(root, candidate)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise StorageSafetyError("Storage path escapes the configured root.") from exc
    return resolved


def _require_secure_local_storage_support() -> None:
    """Fail closed when secure descriptor-relative I/O is unavailable."""
    required_dir_fd_functions = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir)
    if (
        not getattr(os, "O_DIRECTORY", 0)
        or not getattr(os, "O_NOFOLLOW", 0)
        or any(function not in os.supports_dir_fd for function in required_dir_fd_functions)
        or os.scandir not in os.supports_fd
    ):
        raise StorageSafetyError(
            "Secure local storage requires directory-descriptor support."
        )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _raise_for_unsafe_directory_error(exc: OSError) -> None:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise StorageSafetyError(
            "Symlinks and non-directory components are not allowed in storage paths."
        ) from exc


def _local_storage_id_name(value: str) -> str:
    """Project custom IDs injectively while retaining PocketBase's layout."""
    if _POCKETBASE_STORAGE_ID_PATTERN.fullmatch(value):
        return value
    return _LOCAL_ID_ESCAPE_PREFIX + value.encode("utf-8").hex()


def _directory_entry_stat_at(
    directory_fd: int,
    name: str,
) -> os.stat_result | None:
    """Return lstat data only for an exact directory-entry spelling."""
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if entry.name != name:
                continue
            try:
                return entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                return None
    return None


def _open_directory_fast_at(
    parent_fd: int,
    name: str,
    *,
    create: bool,
    mode: int = _DIRECTORY_MODE,
) -> int | None:
    """Open an unambiguous internal component without enumerating siblings."""
    flags = _directory_open_flags()
    for _attempt in range(3):
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(name, mode=mode, dir_fd=parent_fd)
            except FileExistsError:
                continue
        except OSError as exc:
            _raise_for_unsafe_directory_error(exc)
            raise
    raise StorageSafetyError("Storage directory changed repeatedly while opening it.")


def _directory_change_token(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_mtime_ns, value.st_ctime_ns, value.st_size)


def _cache_exact_storage_directory(
    key: tuple[int, int, str],
    child_stat: os.stat_result,
    parent_stat: os.stat_result,
) -> None:
    with _runtime_lock:
        _exact_storage_id_cache[key] = (
            child_stat.st_dev,
            child_stat.st_ino,
            *_directory_change_token(parent_stat),
        )
        _exact_storage_id_cache.move_to_end(key)
        while len(_exact_storage_id_cache) > _EXACT_STORAGE_ID_CACHE_LIMIT:
            _exact_storage_id_cache.popitem(last=False)


def _open_exact_storage_directory_at(
    parent_fd: int,
    name: str,
    *,
    create: bool,
) -> int | None:
    """Open an exact ID spelling with a parent-change-aware positive cache."""
    parent_before = os.fstat(parent_fd)
    cache_key = (parent_before.st_dev, parent_before.st_ino, name)
    with _runtime_lock:
        cached = _exact_storage_id_cache.get(cache_key)

    if cached is not None:
        expected_dev, expected_ino, mtime_ns, ctime_ns, parent_size = cached
        if _directory_change_token(parent_before) == (
            mtime_ns,
            ctime_ns,
            parent_size,
        ):
            descriptor = _open_directory_fast_at(
                parent_fd,
                name,
                create=False,
            )
            if descriptor is not None:
                opened_stat = os.fstat(descriptor)
                parent_after = os.fstat(parent_fd)
                if (
                    (opened_stat.st_dev, opened_stat.st_ino)
                    == (expected_dev, expected_ino)
                    and _directory_change_token(parent_after)
                    == (mtime_ns, ctime_ns, parent_size)
                ):
                    with _runtime_lock:
                        if cache_key in _exact_storage_id_cache:
                            _exact_storage_id_cache.move_to_end(cache_key)
                    return descriptor
                os.close(descriptor)
        with _runtime_lock:
            _exact_storage_id_cache.pop(cache_key, None)

    descriptor = _open_directory_at(parent_fd, name, create=create)
    if descriptor is None:
        return None
    _cache_exact_storage_directory(
        cache_key,
        os.fstat(descriptor),
        os.fstat(parent_fd),
    )
    return descriptor


def _open_storage_id_directory_at(
    parent_fd: int,
    logical_id: str,
    *,
    create: bool,
) -> tuple[int, str] | None:
    """Open an encoded ID directory, scanning only for exact legacy fallback."""
    physical_name = _local_storage_id_name(logical_id)
    physical_fd = _open_exact_storage_directory_at(
        parent_fd,
        physical_name,
        create=False,
    )
    if physical_fd is not None:
        return physical_fd, physical_name

    parent_stat = os.fstat(parent_fd)
    cache_key = (parent_stat.st_dev, parent_stat.st_ino, logical_id)
    with _runtime_lock:
        cached_legacy = _legacy_storage_id_cache.get(cache_key)

    if cached_legacy is not None:
        legacy_name, expected_dev, expected_ino = cached_legacy
        legacy_fd = _open_exact_storage_directory_at(
            parent_fd,
            legacy_name,
            create=False,
        )
        if legacy_fd is not None:
            opened_stat = os.fstat(legacy_fd)
            if (opened_stat.st_dev, opened_stat.st_ino) == (
                expected_dev,
                expected_ino,
            ):
                return legacy_fd, legacy_name
            os.close(legacy_fd)
        with _runtime_lock:
            _legacy_storage_id_cache.pop(cache_key, None)
    legacy_stat = _directory_entry_stat_at(parent_fd, logical_id)
    if legacy_stat is not None:
        with _runtime_lock:
            _legacy_storage_id_cache[cache_key] = (
                logical_id,
                legacy_stat.st_dev,
                legacy_stat.st_ino,
            )
    if legacy_stat is not None:
        if stat.S_ISLNK(legacy_stat.st_mode) or not stat.S_ISDIR(legacy_stat.st_mode):
            raise StorageSafetyError(
                "Legacy storage IDs must reference a regular directory."
            )
        legacy_fd = _open_exact_storage_directory_at(
            parent_fd,
            logical_id,
            create=False,
        )
        if legacy_fd is None:
            raise StorageSafetyError("Legacy storage directory changed while opening.")
        opened_stat = os.fstat(legacy_fd)
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            legacy_stat.st_dev,
            legacy_stat.st_ino,
        ):
            os.close(legacy_fd)
            raise StorageSafetyError("Legacy storage directory changed while opening.")
        return legacy_fd, logical_id

    if not create:
        return None
    created_fd = _open_exact_storage_directory_at(
        parent_fd,
        physical_name,
        create=True,
    )
    if created_fd is None:  # pragma: no cover - create=True is fail-or-open
        raise StorageSafetyError("Unable to create encoded storage directory.")
    return created_fd, physical_name


def _open_directory_at(
    parent_fd: int,
    name: str,
    *,
    create: bool,
    mode: int = _DIRECTORY_MODE,
) -> int | None:
    """Open one child directory without ever following its name as a symlink."""
    flags = _directory_open_flags()

    for _attempt in range(3):
        existing = _directory_entry_stat_at(parent_fd, name)
        if existing is None:
            if not create:
                return None
            try:
                os.mkdir(name, mode=mode, dir_fd=parent_fd)
            except FileExistsError as exc:
                if _directory_entry_stat_at(parent_fd, name) is None:
                    raise StorageSafetyError(
                        "A non-exact local storage name aliases the requested path."
                    ) from exc
                continue
            existing = _directory_entry_stat_at(parent_fd, name)
            if existing is None:
                continue
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
            raise StorageSafetyError(
                "Symlinks and non-directory components are not allowed in storage paths."
            )
        expected_identity = (existing.st_dev, existing.st_ino)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _raise_for_unsafe_directory_error(exc)
            raise
        opened_stat = os.fstat(descriptor)
        current = _directory_entry_stat_at(parent_fd, name)
        if (
            current is None
            or (opened_stat.st_dev, opened_stat.st_ino) != expected_identity
            or (current.st_dev, current.st_ino) != expected_identity
        ):
            os.close(descriptor)
            continue
        return descriptor

    raise StorageSafetyError("Storage directory changed repeatedly while opening it.")


def _open_absolute_directory(path: Path, *, create: bool) -> int | None:
    """Walk an absolute path from ``/`` using only descriptor-relative opens."""
    _require_secure_local_storage_support()
    resolved_path = path.expanduser().resolve(strict=False)
    if not resolved_path.is_absolute() or resolved_path.anchor != os.sep:
        raise StorageSafetyError("The configured data directory must be absolute.")

    descriptor = os.open(
        resolved_path.anchor,
        _directory_open_flags(),
    )
    try:
        for component in resolved_path.parts[1:]:
            child_fd = _open_directory_at(
                descriptor,
                component,
                create=create,
            )
            if child_fd is None:
                os.close(descriptor)
                descriptor = -1
                return None
            os.close(descriptor)
            descriptor = child_fd
        result = descriptor
        descriptor = -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_storage_root_fd(config: _StorageConfig, *, create: bool) -> int | None:
    """Open the storage root anchored to the configured resolved data directory."""
    data_root = Path(config.data_dir).expanduser().resolve(strict=False)
    data_fd = _open_absolute_directory(data_root, create=create)
    if data_fd is None:
        return None
    try:
        return _open_directory_at(data_fd, "storage", create=create)
    finally:
        os.close(data_fd)


@contextmanager
def _open_record_directory(
    config: _StorageConfig,
    collection_id: str,
    record_id: str,
    *,
    create: bool,
) -> Iterator[_OpenedRecordDirectory | None]:
    """Open a record directory through a no-follow descriptor chain."""
    safe_collection_id = validate_collection_id(collection_id)
    safe_record_id = validate_record_id(record_id)
    storage_fd = _open_storage_root_fd(config, create=create)
    collection_fd = -1
    record_fd = -1

    try:
        if storage_fd is None:
            yield None
            return
        opened_collection = _open_storage_id_directory_at(
            storage_fd,
            safe_collection_id,
            create=create,
        )
        if opened_collection is None:
            yield None
            return
        collection_fd, collection_name = opened_collection

        opened_record = _open_storage_id_directory_at(
            collection_fd,
            safe_record_id,
            create=create,
        )
        if opened_record is None:
            yield None
            return
        record_fd, record_name = opened_record
        yield _OpenedRecordDirectory(
            collection_fd=collection_fd,
            record_fd=record_fd,
            collection_name=collection_name,
            record_name=record_name,
        )
    finally:
        if record_fd >= 0:
            os.close(record_fd)
        if collection_fd >= 0:
            os.close(collection_fd)
        if storage_fd is not None:
            os.close(storage_fd)


def _lstat_at(directory_fd: int, name: str) -> os.stat_result | None:
    return _directory_entry_stat_at(directory_fd, name)


def _has_nonexact_alias_at(directory_fd: int, name: str) -> bool:
    if _lstat_at(directory_fd, name) is not None:
        return False
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _write_file_exclusive_at(
    directory_fd: int,
    filename: str,
    content: bytes,
) -> tuple[int, int]:
    """Create one regular file relative to a trusted directory descriptor."""
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(filename, flags, _FILE_MODE, dir_fd=directory_fd)
    except FileExistsError as exc:
        existing = _lstat_at(directory_fd, filename)
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise StorageSafetyError(
                "Symlinks are not allowed in storage paths."
            ) from exc
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StorageSafetyError(
                "Symlinks are not allowed in storage paths."
            ) from exc
        raise

    opened_stat = os.fstat(descriptor)
    identity = (opened_stat.st_dev, opened_stat.st_ino)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
    except BaseException:
        current = _lstat_at(directory_fd, filename)
        if (
            current is not None
            and stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino) == identity
        ):
            try:
                os.unlink(filename, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return identity


def _read_regular_file_at(directory_fd: int, filename: str) -> bytes | None:
    """Read one regular file relative to a trusted directory descriptor."""
    opened = _open_regular_file_at(directory_fd, filename)
    if opened is None:
        return None
    try:
        return opened.stream.read()
    finally:
        opened.close()


def _open_regular_file_at(
    directory_fd: int,
    filename: str,
    *,
    config: _StorageConfig | None = None,
    storage_path: Path | None = None,
) -> StorageFileStream | None:
    """Open one regular file relative to a trusted directory descriptor."""
    existing = _lstat_at(directory_fd, filename)
    if existing is None:
        return None
    if stat.S_ISLNK(existing.st_mode):
        raise StorageSafetyError("Symlinks are not allowed in storage paths.")
    if not stat.S_ISREG(existing.st_mode):
        return None
    expected_identity = (existing.st_dev, existing.st_ino)
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StorageSafetyError(
                "Symlinks are not allowed in storage paths."
            ) from exc
        return None
    try:
        opened_stat = os.fstat(descriptor)
        current = _lstat_at(directory_fd, filename)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or (opened_stat.st_dev, opened_stat.st_ino) != expected_identity
            or current is None
            or (current.st_dev, current.st_ino) != expected_identity
        ):
            raise StorageSafetyError(
                "Storage file changed while it was being opened."
            )
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        etag_base = f"{opened_stat.st_mtime}-{opened_stat.st_size}"
        etag = (
            '"'
            + hashlib.md5(  # noqa: S324 - parity with Starlette FileResponse
                etag_base.encode(),
                usedforsecurity=False,
            ).hexdigest()
            + '"'
        )
        return StorageFileStream(
            stream=stream,
            content_length=opened_stat.st_size,
            backend="local",
            config=config,
            storage_path=storage_path,
            etag=etag,
            last_modified=formatdate(opened_stat.st_mtime, usegmt=True),
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _directory_entry_names(directory_fd: int) -> list[str]:
    with os.scandir(directory_fd) as entries:
        return [entry.name for entry in entries]


def _ensure_tree_has_no_symlinks_at(directory_fd: int) -> None:
    """Validate a tree using no-follow opens rooted at ``directory_fd``."""
    for name in _directory_entry_names(directory_fd):
        entry_stat = _lstat_at(directory_fd, name)
        if entry_stat is None:
            continue
        if stat.S_ISLNK(entry_stat.st_mode):
            raise StorageSafetyError(
                "Symlinks are not allowed in record storage directories."
            )
        if not stat.S_ISDIR(entry_stat.st_mode):
            continue
        child_fd = _open_directory_at(directory_fd, name, create=False)
        if child_fd is None:
            continue
        try:
            _ensure_tree_has_no_symlinks_at(child_fd)
        finally:
            os.close(child_fd)


def _remove_tree_contents_at(directory_fd: int) -> None:
    """Recursively remove a validated tree without resolving path strings."""
    for name in _directory_entry_names(directory_fd):
        entry_stat = _lstat_at(directory_fd, name)
        if entry_stat is None:
            continue
        if stat.S_ISLNK(entry_stat.st_mode):
            raise StorageSafetyError(
                "Symlinks are not allowed in record storage directories."
            )
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_directory_at(directory_fd, name, create=False)
            if child_fd is None:
                continue
            try:
                _remove_tree_contents_at(child_fd)
            finally:
                os.close(child_fd)
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                _raise_for_unsafe_directory_error(exc)
                raise StorageSafetyError(
                    "Storage tree changed while it was being removed."
                ) from exc
            continue

        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            continue
        except IsADirectoryError as exc:
            raise StorageSafetyError(
                "Storage tree changed while it was being removed."
            ) from exc


def _remove_tree_contents_except_at(
    directory_fd: int,
    preserved_names: set[str],
) -> None:
    """Remove record entries except final source files and their thumbnails."""
    preserved_entries = set(preserved_names)
    preserved_entries.update(
        directory_name
        for filename in preserved_names
        if (directory_name := _thumbnail_directory_name(filename)) is not None
    )
    _ensure_tree_has_no_symlinks_at(directory_fd)
    for name in _directory_entry_names(directory_fd):
        if name in preserved_entries:
            continue
        entry_stat = _lstat_at(directory_fd, name)
        if entry_stat is None:
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_directory_at(directory_fd, name, create=False)
            if child_fd is None:
                continue
            try:
                _remove_tree_contents_at(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _thumbnail_directory_name(filename: str) -> str | None:
    candidate = f"thumbs_{filename}"
    try:
        return validate_file_reference(candidate)
    except StorageSafetyError:
        return None


def _validate_variant_directory_at(record_fd: int, directory_name: str) -> None:
    existing = _lstat_at(record_fd, directory_name)
    if existing is None:
        return
    if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
        raise StorageSafetyError(
            "Thumbnail variants must be stored in a regular directory."
        )
    variant_fd = _open_directory_at(record_fd, directory_name, create=False)
    if variant_fd is None:
        return
    try:
        _ensure_tree_has_no_symlinks_at(variant_fd)
    finally:
        os.close(variant_fd)


def _remove_variant_directory_at(record_fd: int, directory_name: str) -> None:
    variant_fd = _open_directory_at(record_fd, directory_name, create=False)
    if variant_fd is None:
        return
    try:
        _remove_tree_contents_at(variant_fd)
    finally:
        os.close(variant_fd)
    try:
        os.rmdir(directory_name, dir_fd=record_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        _raise_for_unsafe_directory_error(exc)
        raise StorageSafetyError(
            "Thumbnail directory changed while it was being removed."
        ) from exc


def _remove_record_directory_if_empty_at(
    collection_fd: int,
    record_id: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> bool:
    """Remove the record directory itself only when it is currently empty."""
    if record_id.startswith(_LOCAL_ID_ESCAPE_PREFIX):
        try:
            existing = os.stat(
                record_id,
                dir_fd=collection_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
    else:
        existing = _lstat_at(collection_fd, record_id)
    if existing is None:
        if _has_nonexact_alias_at(collection_fd, record_id):
            raise StorageSafetyError(
                "A non-exact local storage name aliases the record directory."
            )
        return False
    if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
        raise StorageSafetyError(
            "Symlinks and non-directory components are not allowed in storage paths."
        )
    if (
        expected_identity is not None
        and (existing.st_dev, existing.st_ino) != expected_identity
    ):
        raise StorageSafetyError("Record storage changed before directory removal.")
    try:
        os.rmdir(record_id, dir_fd=collection_fd)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            return False
        _raise_for_unsafe_directory_error(exc)
        raise
    return True


def _unlink_regular_file_at(
    directory_fd: int,
    filename: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> bool:
    """Unlink one regular file name without following links or directories."""
    existing = _lstat_at(directory_fd, filename)
    if existing is None:
        return False
    if stat.S_ISLNK(existing.st_mode):
        raise StorageSafetyError("Symlinks are not allowed in storage paths.")
    if not stat.S_ISREG(existing.st_mode):
        return False
    if (
        expected_identity is not None
        and (existing.st_dev, existing.st_ino) != expected_identity
    ):
        return False

    try:
        # unlinkat(2) removes the directory entry itself and therefore cannot
        # follow a symlink inserted after the lstat above.
        os.unlink(filename, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    except IsADirectoryError as exc:
        raise StorageSafetyError(
            "Storage entry changed while it was being removed."
        ) from exc
    return True


def _write_file_atomically_at(
    directory_fd: int,
    filename: str,
    content: bytes,
) -> None:
    """Publish bytes atomically without following the destination name."""
    temp_name = ""
    for _attempt in range(20):
        candidate = f".__ppbase_{_random_suffix(20)}.tmp"
        try:
            _write_file_exclusive_at(directory_fd, candidate, content)
        except FileExistsError:
            continue
        temp_name = candidate
        break
    if not temp_name:
        raise RuntimeError("Unable to allocate a temporary storage filename")

    try:
        if _has_nonexact_alias_at(directory_fd, filename):
            raise StorageSafetyError(
                "A non-exact local storage name aliases the requested file."
            )
        existing = _lstat_at(directory_fd, filename)
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise StorageSafetyError(
                "Symlinks are not allowed in storage paths."
            )
        if existing is not None and stat.S_ISDIR(existing.st_mode):
            raise StorageSafetyError(
                "A storage file cannot replace a directory."
            )
        # renameat(2) replaces a destination symlink as a directory entry; it
        # never follows it. The lstat above preserves the explicit rejection
        # behavior when the symlink already exists before publication.
        os.replace(
            temp_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temp_name = ""
    finally:
        if temp_name:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _select_local_storage_id_name_path(parent: Path, logical_id: str) -> str:
    physical_name = _local_storage_id_name(logical_id)
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name == physical_name:
                    return physical_name
        if physical_name != logical_id:
            with os.scandir(parent) as entries:
                for entry in entries:
                    if entry.name == logical_id:
                        return logical_id
    except (FileNotFoundError, NotADirectoryError):
        return physical_name
    if physical_name == logical_id:
        try:
            os.lstat(parent / physical_name)
        except FileNotFoundError:
            pass
        else:
            raise StorageSafetyError(
                "A non-exact local storage name aliases the requested ID."
            )
    return physical_name


def get_storage_path(collection_id: str, record_id: str) -> Path:
    """Return the confined local storage directory for a record."""
    _require_secure_local_storage_support()
    config = _resolve_storage_config()
    root = _get_storage_root(config)
    safe_collection_id = validate_collection_id(collection_id)
    safe_record_id = validate_record_id(record_id)
    collection_name = _select_local_storage_id_name_path(
        root,
        safe_collection_id,
    )
    collection_path = _confine_storage_path(
        root,
        root / collection_name,
    )
    record_name = _select_local_storage_id_name_path(
        collection_path,
        safe_record_id,
    )
    return _confine_storage_path(
        root,
        collection_path / record_name,
    )


def get_storage_file_path(
    collection_id: str,
    record_id: str,
    filename: str,
) -> Path:
    """Return a confined local path for one stored file reference."""
    storage_dir = get_storage_path(collection_id, record_id)
    safe_filename = validate_file_reference(filename)
    return _confine_storage_path(
        storage_dir,
        storage_dir / safe_filename,
    )


def _sanitize_stem(original_name: str) -> str:
    stem = Path(original_name).stem.strip()
    if not stem:
        return "file"
    stem = stem.replace(" ", "_")
    stem = _SAFE_STEM_PATTERN.sub("_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    return (stem or "file")[:_MAX_SAFE_STEM_LENGTH]


def _random_suffix(length: int = 10) -> str:
    return "".join(secrets.choice(_ALPHANUM) for _ in range(length))


def _generate_storage_filename(original_name: str) -> str:
    raw_extension = Path(original_name).suffix.lstrip(".")
    safe_extension = _SAFE_EXTENSION_PATTERN.sub("", raw_extension)[
        :_MAX_SAFE_EXTENSION_LENGTH
    ]
    ext = f".{safe_extension}" if safe_extension else ""
    stem = _sanitize_stem(original_name)
    return f"{stem}_{_random_suffix()}{ext}"


def _storage_object_key(collection_id: str, record_id: str, filename: str) -> str:
    safe_collection_id = validate_collection_id(collection_id)
    safe_record_id = validate_record_id(record_id)
    safe_filename = validate_file_reference(filename)
    return f"{safe_collection_id}/{safe_record_id}/{safe_filename}"


def _deletable_file_references(filenames: list[str]) -> list[str]:
    """Validate local/S3 names while ignoring legacy remote avatar values."""
    safe_filenames: list[str] = []
    for filename in filenames:
        if is_remote_file_reference(filename):
            continue
        safe_filenames.append(validate_file_reference(filename))
    return safe_filenames


def _get_s3_client(config: _StorageConfig) -> Any:
    global _s3_client_cache
    global _s3_client_cache_key

    cache_key = (
        config.s3_endpoint,
        config.s3_region,
        config.s3_access_key,
        config.s3_secret_key,
        config.s3_force_path_style,
    )

    with _runtime_lock:
        if _s3_client_cache is not None and _s3_client_cache_key == cache_key:
            return _s3_client_cache

    try:
        import boto3
    except Exception as exc:
        raise RuntimeError(
            "S3 storage backend requires boto3. Install it with: pip install boto3"
        ) from exc

    client_kwargs: dict[str, Any] = {
        "aws_access_key_id": config.s3_access_key,
        "aws_secret_access_key": config.s3_secret_key,
    }
    if config.s3_region:
        client_kwargs["region_name"] = config.s3_region
    if config.s3_endpoint:
        client_kwargs["endpoint_url"] = config.s3_endpoint
    if config.s3_force_path_style:
        try:
            from botocore.config import Config as BotocoreConfig

            client_kwargs["config"] = BotocoreConfig(
                s3={"addressing_style": "path"},
            )
        except Exception:
            pass

    client = boto3.client("s3", **client_kwargs)
    with _runtime_lock:
        _s3_client_cache = client
        _s3_client_cache_key = cache_key
    return client


def _is_s3_conditional_conflict(exc: Exception) -> bool:
    """Return whether S3 rejected a conditional create because the key exists."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = str(error.get("Code", "") if isinstance(error, dict) else "")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return code in {
        "PreconditionFailed",
        "ConditionalRequestConflict",
        "412",
    } or status in {409, 412}


def _save_s3_files(
    config: _StorageConfig,
    collection_id: str,
    record_id: str,
    files: list[tuple[str, bytes]],
    max_select: int,
) -> list[str]:
    validate_collection_id(collection_id)
    validate_record_id(record_id)
    client = _get_s3_client(config)
    saved: list[str] = []

    try:
        for original_name, content in files:
            for _attempt in range(20):
                unique_name = _generate_storage_filename(original_name)
                validate_file_reference(unique_name)
                object_key = _storage_object_key(
                    collection_id,
                    record_id,
                    unique_name,
                )
                try:
                    client.put_object(
                        Bucket=config.s3_bucket,
                        Key=object_key,
                        Body=content,
                        IfNoneMatch="*",
                    )
                except Exception as exc:
                    if _is_s3_conditional_conflict(exc):
                        continue
                    # The server may have persisted the object before the
                    # transport failed. Without an ownership proof it is safer
                    # to preserve the possible orphan than delete another
                    # writer's object.
                    raise StorageObjectWriteError(
                        "S3 upload failed with an ambiguous object outcome.",
                        created_filenames=tuple(saved),
                        ambiguous_filenames=(unique_name,),
                    ) from exc
                saved.append(unique_name)
                _track_storage_writes(
                    collection_id,
                    record_id,
                    [unique_name],
                )
                break
            else:
                raise RuntimeError(
                    "Unable to allocate a unique S3 storage object key"
                )
            if max_select == 1:
                break
    except BaseException as write_error:
        # Only ACKed conditional creates are proven to belong to this call.
        # Ambiguous keys are deliberately preserved for later reconciliation.
        cleanup_failures: list[str] = []
        for filename in reversed(saved):
            try:
                client.delete_object(
                    Bucket=config.s3_bucket,
                    Key=_storage_object_key(collection_id, record_id, filename),
                )
            except Exception:
                cleanup_failures.append(filename)
        if isinstance(write_error, StorageObjectWriteError):
            write_error.cleanup_failed_filenames = tuple(cleanup_failures)
            raise
        if cleanup_failures and isinstance(write_error, Exception):
            cleanup_error = StorageObjectWriteError(
                "S3 upload failed and cleanup could not remove "
                f"{len(cleanup_failures)} created object(s).",
                created_filenames=tuple(saved),
                cleanup_failed_filenames=tuple(cleanup_failures),
            )
            raise cleanup_error from write_error
        raise

    return saved


def _delete_s3_files(
    config: _StorageConfig,
    collection_id: str,
    record_id: str,
    filenames: list[str],
) -> None:
    object_keys = [
        _storage_object_key(collection_id, record_id, filename)
        for filename in filenames
    ]
    client = _get_s3_client(config)
    failures = 0
    for object_key in object_keys:
        try:
            client.delete_object(Bucket=config.s3_bucket, Key=object_key)
        except Exception:
            failures += 1
    if failures:
        raise OSError(f"Failed to delete {failures} S3 storage object(s).")


def _delete_all_s3_files(
    config: _StorageConfig,
    collection_id: str,
    record_id: str,
    *,
    preserved_filenames: set[str] | None = None,
) -> None:
    safe_collection_id = validate_collection_id(collection_id)
    safe_record_id = validate_record_id(record_id)
    prefix = f"{safe_collection_id}/{safe_record_id}/"
    preserved_keys = {
        f"{prefix}{filename}"
        for filename in (preserved_filenames or set())
    }
    client = _get_s3_client(config)
    continuation_token: str | None = None
    failures = 0

    while True:
        params: dict[str, Any] = {
            "Bucket": config.s3_bucket,
            "Prefix": prefix,
        }
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        response = client.list_objects_v2(**params)
        contents = response.get("Contents") or []
        keys = [
            {"Key": str(item.get("Key", ""))}
            for item in contents
            if item.get("Key") and str(item.get("Key")) not in preserved_keys
        ]
        if keys:
            try:
                delete_response = client.delete_objects(
                    Bucket=config.s3_bucket,
                    Delete={"Objects": keys, "Quiet": True},
                )
                if isinstance(delete_response, dict):
                    failures += len(delete_response.get("Errors") or [])
            except Exception:
                for key in keys:
                    try:
                        client.delete_object(Bucket=config.s3_bucket, Key=key["Key"])
                    except Exception:
                        failures += 1
        if not response.get("IsTruncated"):
            break
        continuation_token = str(response.get("NextContinuationToken", "") or "") or None
    if failures:
        raise OSError(f"Failed to delete {failures} S3 storage object(s).")


def open_file_stream(
    collection_id: str,
    record_id: str,
    filename: str,
    *,
    byte_range: tuple[int, int] | None = None,
    config: _StorageConfig | None = None,
    if_match: str | None = None,
) -> StorageFileStream | None:
    """Open a regular local file or S3 body without buffering its contents."""
    config = config or _resolve_storage_config()
    if config.backend == "s3":
        object_key = _storage_object_key(collection_id, record_id, filename)
        client = _get_s3_client(config)
        params: dict[str, Any] = {
            "Bucket": config.s3_bucket,
            "Key": object_key,
        }
        if byte_range is not None:
            start, end = byte_range
            params["Range"] = f"bytes={start}-{end}"
        if if_match:
            params["IfMatch"] = if_match
        try:
            response = client.get_object(**params)
        except Exception:
            return None

        body = response.get("Body")
        if body is None:
            return None
        raw_length = response.get("ContentLength")
        try:
            content_length = int(raw_length) if raw_length is not None else None
        except (TypeError, ValueError):
            content_length = None
        raw_etag = response.get("ETag")
        etag = str(raw_etag) if raw_etag else None
        raw_last_modified = response.get("LastModified")
        if isinstance(raw_last_modified, datetime):
            normalized_last_modified = raw_last_modified
            if normalized_last_modified.tzinfo is None:
                normalized_last_modified = normalized_last_modified.replace(
                    tzinfo=timezone.utc
                )
            else:
                normalized_last_modified = normalized_last_modified.astimezone(
                    timezone.utc
                )
            last_modified = format_datetime(
                normalized_last_modified,
                usegmt=True,
            )
        elif raw_last_modified:
            last_modified = str(raw_last_modified)
        else:
            last_modified = None
        return StorageFileStream(
            stream=body,
            content_length=content_length,
            backend="s3",
            config=config,
            object_key=object_key,
            etag=etag,
            last_modified=last_modified,
        )

    safe_filename = validate_file_reference(filename)
    with _open_record_directory(
        config,
        collection_id,
        record_id,
        create=False,
    ) as opened:
        if opened is None:
            return None
        storage_path = (
            _get_storage_root(config)
            / opened.collection_name
            / opened.record_name
            / safe_filename
        )
        return _open_regular_file_at(
            opened.record_fd,
            safe_filename,
            config=config,
            storage_path=storage_path,
        )


def read_file_bytes(collection_id: str, record_id: str, filename: str) -> bytes | None:
    """Read stored file bytes from active backend."""
    opened = open_file_stream(collection_id, record_id, filename)
    if opened is None:
        return None
    try:
        return bytes(opened.stream.read())
    finally:
        opened.close()


def read_local_storage_variant_bytes(
    collection_id: str,
    record_id: str,
    directory_name: str,
    filename: str,
    *,
    config: _StorageConfig | None = None,
) -> bytes | None:
    """Read a local derived file below one validated record subdirectory."""
    config = config or _resolve_storage_config()
    if config.backend != "local":
        raise RuntimeError("Local storage variants require the local backend.")
    safe_directory_name = validate_file_reference(directory_name)
    safe_filename = validate_file_reference(filename)

    with _open_record_directory(
        config,
        collection_id,
        record_id,
        create=False,
    ) as opened:
        if opened is None:
            return None
        variant_fd = _open_directory_at(
            opened.record_fd,
            safe_directory_name,
            create=False,
        )
        if variant_fd is None:
            return None
        try:
            return _read_regular_file_at(variant_fd, safe_filename)
        finally:
            os.close(variant_fd)


def write_local_storage_variant_bytes(
    collection_id: str,
    record_id: str,
    directory_name: str,
    filename: str,
    content: bytes,
    *,
    config: _StorageConfig | None = None,
) -> None:
    """Atomically publish a local derived file below a record directory."""
    config = config or _resolve_storage_config()
    if config.backend != "local":
        raise RuntimeError("Local storage variants require the local backend.")
    safe_directory_name = validate_file_reference(directory_name)
    safe_filename = validate_file_reference(filename)

    with _open_record_directory(
        config,
        collection_id,
        record_id,
        create=False,
    ) as opened:
        if opened is None:
            raise FileNotFoundError("The record storage directory does not exist.")
        variant_fd = _open_directory_at(
            opened.record_fd,
            safe_directory_name,
            create=True,
        )
        if variant_fd is None:  # pragma: no cover - create=True is fail-or-open
            raise StorageSafetyError("Unable to open the storage variant directory.")
        try:
            _write_file_atomically_at(variant_fd, safe_filename, content)
        except BaseException:
            try:
                os.rmdir(safe_directory_name, dir_fd=opened.record_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(variant_fd)


def save_files(
    collection_id: str,
    record_id: str,
    field_name: str,
    files: list[tuple[str, bytes]],
    max_select: int = 1,
) -> list[str]:
    """Save uploaded files and return stored filenames."""
    _ = field_name
    if not files:
        return []

    config = _resolve_storage_config()
    if config.backend == "s3":
        saved = _save_s3_files(config, collection_id, record_id, files, max_select)
        _track_storage_writes(collection_id, record_id, saved)
        return saved

    saved: list[str] = []
    saved_identities: dict[str, tuple[int, int]] = {}
    with _open_record_directory(
        config,
        collection_id,
        record_id,
        create=True,
    ) as opened:
        if opened is None:  # pragma: no cover - create=True is fail-or-open
            raise StorageSafetyError("Unable to open the record storage directory.")
        try:
            for original_name, content in files:
                for _attempt in range(20):
                    unique_name = _generate_storage_filename(original_name)
                    validate_file_reference(unique_name)
                    try:
                        identity = _write_file_exclusive_at(
                            opened.record_fd,
                            unique_name,
                            content,
                        )
                    except FileExistsError:
                        continue
                    saved.append(unique_name)
                    saved_identities[unique_name] = identity
                    _track_storage_writes(
                        collection_id,
                        record_id,
                        [unique_name],
                    )
                    break
                else:
                    raise RuntimeError(
                        "Unable to generate a unique filename for uploaded file"
                    )

                if max_select == 1:
                    break
        except BaseException:
            for filename in reversed(saved):
                try:
                    _unlink_regular_file_at(
                        opened.record_fd,
                        filename,
                        expected_identity=saved_identities.get(filename),
                    )
                except (OSError, StorageSafetyError):
                    continue
            try:
                _remove_record_directory_if_empty_at(
                    opened.collection_fd,
                    opened.record_name,
                    expected_identity=(
                        os.fstat(opened.record_fd).st_dev,
                        os.fstat(opened.record_fd).st_ino,
                    ),
                )
            except (OSError, StorageSafetyError):
                pass
            raise

    _track_storage_writes(collection_id, record_id, saved)
    return saved


def delete_files(collection_id: str, record_id: str, filenames: list[str]) -> None:
    """Delete specific files from active backend."""
    if not filenames:
        return

    safe_collection_id = validate_collection_id(collection_id)
    safe_record_id = validate_record_id(record_id)
    safe_filenames = _deletable_file_references(filenames)
    if not safe_filenames:
        return

    deferred_actions = _storage_delete_tracker.get()
    if deferred_actions is not None:
        deferred_actions.append(
            (
                "files",
                safe_collection_id,
                safe_record_id,
                tuple(safe_filenames),
            )
        )
        return

    config = _resolve_storage_config()
    if config.backend == "s3":
        _delete_s3_files(
            config,
            safe_collection_id,
            safe_record_id,
            safe_filenames,
        )
        return

    with _open_record_directory(
        config,
        safe_collection_id,
        safe_record_id,
        create=False,
    ) as opened:
        if opened is None:
            return

        # Reject every pre-existing symlink before applying any deletion from
        # the batch. A later replacement is still safe because unlinkat(2)
        # never follows the name being removed.
        for filename in safe_filenames:
            existing = _lstat_at(opened.record_fd, filename)
            if existing is not None and stat.S_ISLNK(existing.st_mode):
                raise StorageSafetyError(
                    "Symlinks are not allowed in storage paths."
                )
            variant_name = _thumbnail_directory_name(filename)
            if variant_name is not None:
                _validate_variant_directory_at(opened.record_fd, variant_name)
        for filename in safe_filenames:
            _unlink_regular_file_at(opened.record_fd, filename)
            variant_name = _thumbnail_directory_name(filename)
            if variant_name is not None:
                _remove_variant_directory_at(opened.record_fd, variant_name)

        _remove_record_directory_if_empty_at(
            opened.collection_fd,
            opened.record_name,
            expected_identity=(
                os.fstat(opened.record_fd).st_dev,
                os.fstat(opened.record_fd).st_ino,
            ),
        )


def delete_storage_dir_if_empty(collection_id: str, record_id: str) -> bool:
    """Remove an empty local record directory through its anchored parent FD."""
    config = _resolve_storage_config()
    if config.backend != "local":
        validate_collection_id(collection_id)
        validate_record_id(record_id)
        return False

    safe_record_id = validate_record_id(record_id)
    with _open_record_directory(
        config,
        collection_id,
        safe_record_id,
        create=False,
    ) as opened:
        if opened is None:
            return False
        return _remove_record_directory_if_empty_at(
            opened.collection_fd,
            opened.record_name,
            expected_identity=(
                os.fstat(opened.record_fd).st_dev,
                os.fstat(opened.record_fd).st_ino,
            ),
        )


def delete_all_files_except(
    collection_id: str,
    record_id: str,
    preserved_filenames: set[str],
) -> None:
    """Delete obsolete record storage while preserving final DB references."""
    safe_collection_id = validate_collection_id(collection_id)
    safe_record_id = validate_record_id(record_id)
    safe_preserved: set[str] = set()
    for filename in preserved_filenames:
        if is_remote_file_reference(filename):
            continue
        safe_preserved.add(validate_file_reference(filename))
    if not safe_preserved:
        delete_all_files(safe_collection_id, safe_record_id)
        return

    config = _resolve_storage_config()
    if config.backend == "s3":
        _delete_all_s3_files(
            config,
            safe_collection_id,
            safe_record_id,
            preserved_filenames=safe_preserved,
        )
        return

    with _open_record_directory(
        config,
        safe_collection_id,
        safe_record_id,
        create=False,
    ) as opened:
        if opened is None:
            return
        _remove_tree_contents_except_at(opened.record_fd, safe_preserved)


def delete_all_files(collection_id: str, record_id: str) -> None:
    """Delete all files of a record from active backend."""
    safe_collection_id = validate_collection_id(collection_id)
    safe_record_id = validate_record_id(record_id)
    deferred_actions = _storage_delete_tracker.get()
    if deferred_actions is not None:
        deferred_actions.append(("all", safe_collection_id, safe_record_id, ()))
        return

    config = _resolve_storage_config()
    if config.backend == "s3":
        _delete_all_s3_files(config, safe_collection_id, safe_record_id)
        return

    with _open_record_directory(
        config,
        safe_collection_id,
        safe_record_id,
        create=False,
    ) as opened:
        if opened is None:
            return
        _ensure_tree_has_no_symlinks_at(opened.record_fd)
        _remove_tree_contents_at(opened.record_fd)
        if not _remove_record_directory_if_empty_at(
            opened.collection_fd,
            opened.record_name,
            expected_identity=(
                os.fstat(opened.record_fd).st_dev,
                os.fstat(opened.record_fd).st_ino,
            ),
        ):
            raise StorageSafetyError(
                "Record storage changed while it was being removed."
            )
