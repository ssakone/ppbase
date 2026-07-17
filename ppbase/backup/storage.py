"""Private local storage for immutable, signed native backup sets."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from ppbase.backup.control import ControlPlaneRoot, ControlPlaneSafetyError
from ppbase.backup.identity import (
    ED25519_PUBLIC_KEY_SIZE,
    ED25519_SIGNATURE_SIZE,
    BackupIdentity,
    BackupIdentityError,
    verify_manifest_signature,
)
from ppbase.backup.models import (
    DATABASE_DUMP_RESOURCE,
    JWT_SECRET_RESOURCE_PATH,
    BackupAlreadyExistsError,
    BackupError,
    BackupInspection,
    BackupIntegrityError,
    BackupManifest,
    BackupManifestError,
    BackupNotFoundError,
    BackupResource,
    BackupSetSummary,
    BackupStateError,
    BackupUnsafeSourceError,
    JsonValue,
    format_manifest_timestamp,
    validate_backup_id,
)
from ppbase.core.storage_safety import (
    local_storage_id_name,
    validate_collection_id,
    validate_file_reference,
    validate_record_id,
)


MANIFEST_FILENAME = "manifest.json"
SIGNATURE_FILENAME = "manifest.sig"
SIGNER_PUBLIC_KEY_FILENAME = "signer.pub"
SEALED_FILENAME = "SEALED"
RESOURCES_DIRNAME = "resources"
FILES_DIRNAME = "files"
JWT_SECRET_RESOURCE = JWT_SECRET_RESOURCE_PATH

_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_COPY_BUFFER_SIZE = 1024 * 1024


def _open_flags(base: int) -> int:
    return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _directory_open_flags() -> int:
    return _open_flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        _open_flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while not _path_exists(candidate):
        if candidate == candidate.parent:
            raise BackupStateError(f"cannot create backup directory: {path}")
        missing.append(candidate)
        candidate = candidate.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            pass
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
            raise BackupStateError(f"backup path is not a directory: {directory}")
        os.chmod(directory, 0o700, follow_symlinks=False)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)

    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise BackupStateError(f"backup path is not a directory: {path}")
    os.chmod(path, 0o700, follow_symlinks=False)
    if stat.S_IMODE(path.lstat().st_mode) != 0o700:
        raise BackupStateError(f"backup directory must have mode 0700: {path}")
    _fsync_directory(path)


def _create_private_directory_tree_exclusive(path: Path) -> None:
    if _path_exists(path):
        raise FileExistsError(path)
    missing: list[Path] = []
    candidate = path
    while not _path_exists(candidate):
        if candidate == candidate.parent:
            raise BackupStateError(f"cannot create restore directory: {path}")
        missing.append(candidate)
        candidate = candidate.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            if directory == path:
                raise
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
            raise BackupStateError(
                f"restore path is not a safe directory: {directory}"
            )
        os.chmod(directory, 0o700, follow_symlinks=False)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _same_file_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _directory_entry_stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        with os.scandir(parent_fd) as entries:
            for entry in entries:
                if entry.name != name:
                    continue
                try:
                    return entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    return None
    except OSError as exc:
        raise BackupIntegrityError(
            "cannot enumerate restored business-file directory"
        ) from exc
    return None


def _open_exact_restored_directory_at(parent_fd: int, name: str) -> int | None:
    expected = _directory_entry_stat_at(parent_fd, name)
    if expected is None:
        return None
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
        raise BackupIntegrityError(
            "restored business-file path contains an unsafe directory"
        )
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not _same_file_identity(expected, opened)
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise BackupIntegrityError(
                "restored business-file directory changed while opening"
            )
        result = descriptor
        descriptor = None
        return result
    except BackupError:
        raise
    except OSError as exc:
        raise BackupIntegrityError(
            "cannot safely open restored business-file directory"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_restored_storage_id_directory_at(
    parent_fd: int,
    logical_id: str,
) -> int | None:
    physical_name = local_storage_id_name(logical_id)
    descriptor = _open_exact_restored_directory_at(parent_fd, physical_name)
    if descriptor is not None or physical_name == logical_id:
        return descriptor
    return _open_exact_restored_directory_at(parent_fd, logical_id)


def _verify_restored_regular_file_at(parent_fd: int, name: str) -> None:
    expected = _directory_entry_stat_at(parent_fd, name)
    if expected is None:
        raise BackupIntegrityError(
            "database references a file missing from restored storage"
        )
    if (
        stat.S_ISLNK(expected.st_mode)
        or not stat.S_ISREG(expected.st_mode)
        or stat.S_IMODE(expected.st_mode) != 0o600
    ):
        raise BackupIntegrityError(
            "database references an unsafe restored storage entry"
        )
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _open_flags(os.O_RDONLY), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file_identity(
            expected,
            opened,
        ):
            raise BackupIntegrityError(
                "restored business file changed while opening"
            )
    except BackupError:
        raise
    except OSError as exc:
        raise BackupIntegrityError(
            "cannot safely open restored business file"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validated_relative_directory_parts(path: str | Path) -> tuple[str, ...]:
    try:
        relative = Path(path)
    except TypeError as exc:
        raise BackupStateError("staging target must be a relative path") from exc
    if relative.is_absolute() or not relative.parts:
        raise BackupStateError("staging target must be a non-empty relative path")

    parts: list[str] = []
    for part in relative.parts:
        try:
            _validate_source_component(part)
        except BackupUnsafeSourceError as exc:
            raise BackupStateError(
                f"unsafe staging target component: {part!r}"
            ) from exc
        parts.append(part)
    return tuple(parts)


def _open_private_staging_root(path: Path) -> tuple[int, tuple[int, int]]:
    if not path.is_absolute():
        raise BackupStateError("staging_root must be an absolute path")
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise BackupStateError(
            "staging_root must already exist as a private directory"
        ) from exc
    if (
        not stat.S_ISDIR(path_info.st_mode)
        or stat.S_ISLNK(path_info.st_mode)
        or stat.S_IMODE(path_info.st_mode) != 0o700
    ):
        raise BackupStateError("staging_root must be a safe 0700 directory")

    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as exc:
        raise BackupStateError("staging_root cannot be opened safely") from exc
    try:
        opened_info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened_info.st_mode)
            or stat.S_IMODE(opened_info.st_mode) != 0o700
            or not _same_file_identity(path_info, opened_info)
        ):
            raise BackupStateError("staging_root changed while it was opened")
        return descriptor, (opened_info.st_dev, opened_info.st_ino)
    except BaseException:
        os.close(descriptor)
        raise


def _open_new_private_directory_at(
    parent_fd: int,
    name: str,
    *,
    display_path: Path,
) -> tuple[int, tuple[int, int]]:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise BackupAlreadyExistsError(
            f"restore staging target already exists: {display_path}"
        ) from exc
    except OSError as exc:
        raise BackupStateError(
            f"cannot create restore staging directory safely: {display_path}"
        ) from exc

    descriptor: int | None = None
    try:
        created_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(created_info.st_mode):
            raise BackupStateError(
                f"restore staging directory was substituted: {display_path}"
            )
        descriptor = os.open(
            name,
            _directory_open_flags(),
            dir_fd=parent_fd,
        )
        opened_info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened_info.st_mode)
            or not _same_file_identity(created_info, opened_info)
        ):
            raise BackupStateError(
                f"restore staging directory changed while opening: {display_path}"
            )
        os.fchmod(descriptor, 0o700)
        opened_info = os.fstat(descriptor)
        if stat.S_IMODE(opened_info.st_mode) != 0o700:
            raise BackupStateError(
                f"restore staging directory must have mode 0700: {display_path}"
            )
        os.fsync(descriptor)
        os.fsync(parent_fd)
        result = descriptor, (opened_info.st_dev, opened_info.st_ino)
        descriptor = None
        return result
    except BackupError:
        raise
    except OSError as exc:
        raise BackupStateError(
            f"cannot anchor restore staging directory: {display_path}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_anchored_directory_chain(
    staging_root: Path,
    root_identity: tuple[int, int],
    component_identities: tuple[tuple[str, int, int], ...],
    final_fd: int,
) -> None:
    visible_root_fd: int | None = None
    current_fd: int | None = None
    try:
        visible_root_fd = os.open(staging_root, _directory_open_flags())
        root_info = os.fstat(visible_root_fd)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_IMODE(root_info.st_mode) != 0o700
            or (root_info.st_dev, root_info.st_ino) != root_identity
        ):
            raise BackupStateError("staging_root is no longer attached to its path")
        current_fd = visible_root_fd
        visible_root_fd = None

        for name, expected_device, expected_inode in component_identities:
            next_fd = os.open(
                name,
                _directory_open_flags(),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
            info = os.fstat(current_fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino)
                != (expected_device, expected_inode)
            ):
                raise BackupStateError(
                    "restore staging path was renamed or substituted"
                )

        attached_info = os.fstat(current_fd)
        pinned_info = os.fstat(final_fd)
        if not _same_file_identity(attached_info, pinned_info):
            raise BackupStateError("restore staging data_dir is no longer attached")
    except BackupError:
        raise
    except OSError as exc:
        raise BackupStateError(
            "restore staging path was renamed or substituted"
        ) from exc
    finally:
        if current_fd is not None:
            os.close(current_fd)
        if visible_root_fd is not None:
            os.close(visible_root_fd)


def _open_anonymous_regular_file_at(parent_fd: int) -> BinaryIO:
    descriptor: int | None = None
    name: str | None = None
    for _ in range(32):
        candidate = f".restore-input-{secrets.token_hex(16)}"
        try:
            descriptor = os.open(
                candidate,
                _open_flags(os.O_RDWR | os.O_CREAT | os.O_EXCL),
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        name = candidate
        break
    if descriptor is None or name is None:
        raise BackupStateError("cannot allocate an anonymous restore input file")
    try:
        os.fchmod(descriptor, 0o600)
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        handle = os.fdopen(descriptor, "w+b")
        descriptor = None
        return handle
    except OSError as exc:
        raise BackupStateError(
            "cannot prepare an anonymous restore input file"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise BackupStateError("short write while creating backup resource")
        remaining = remaining[written:]


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    else:
        os.close(descriptor)


def _write_exclusive_at(parent_fd: int, name: str, payload: bytes) -> None:
    descriptor = os.open(
        name,
        _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.fsync(parent_fd)
    except BaseException:
        try:
            os.close(descriptor)
        finally:
            try:
                os.unlink(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
        raise
    else:
        os.close(descriptor)


def _read_regular_file(path: Path, *, maximum_size: int) -> bytes:
    try:
        descriptor = os.open(path, _open_flags(os.O_RDONLY))
    except (FileNotFoundError, OSError) as exc:
        raise BackupIntegrityError(f"missing or unsafe backup file: {path.name}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BackupIntegrityError(f"backup file is not regular: {path.name}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise BackupIntegrityError(f"backup file must have mode 0600: {path.name}")
        if info.st_size > maximum_size:
            raise BackupIntegrityError(f"backup file exceeds its size limit: {path.name}")
        chunks: list[bytes] = []
        remaining = maximum_size + 1
        while remaining:
            chunk = os.read(descriptor, min(_COPY_BUFFER_SIZE, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(payload) > maximum_size:
        raise BackupIntegrityError(f"backup file exceeds its size limit: {path.name}")
    return payload


def _validate_source_component(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or name != unicodedata.normalize("NFC", name)
        or "/" in name
        or "\\" in name
        or any(unicodedata.category(character) in {"Cc", "Cs"} for character in name)
    ):
        raise BackupUnsafeSourceError(f"unsafe local storage name: {name!r}")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BackupUnsafeSourceError("local storage name is not valid UTF-8") from exc


def _copy_regular_file_at(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
    *,
    source_display: Path,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    try:
        source_descriptor = os.open(
            source_name,
            _open_flags(os.O_RDONLY),
            dir_fd=source_parent_fd,
        )
    except OSError as exc:
        raise BackupUnsafeSourceError(
            f"cannot safely open source file: {source_display}"
        ) from exc

    destination_descriptor: int | None = None
    destination_created = False
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise BackupUnsafeSourceError(
                f"source is not a regular file: {source_display}"
            )
        if expected_identity is not None and (
            before.st_dev,
            before.st_ino,
        ) != expected_identity:
            raise BackupStateError(
                f"source file changed before backup: {source_display}"
            )

        destination_descriptor = os.open(
            destination_name,
            _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
            dir_fd=destination_parent_fd,
        )
        destination_created = True
        os.fchmod(destination_descriptor, 0o600)
        copied_size = 0
        while True:
            chunk = os.read(source_descriptor, _COPY_BUFFER_SIZE)
            if not chunk:
                break
            _write_all(destination_descriptor, chunk)
            copied_size += len(chunk)
        os.fsync(destination_descriptor)

        after = os.fstat(source_descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or copied_size != after.st_size
        ):
            raise BackupStateError(
                f"source file changed during backup: {source_display}"
            )
    except BaseException:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
            destination_descriptor = None
        if destination_created:
            try:
                os.unlink(destination_name, dir_fd=destination_parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)


def _copy_regular_file(source: Path, destination: Path) -> None:
    source_parent_fd: int | None = None
    destination_parent_fd: int | None = None
    try:
        source_parent_fd = os.open(source.parent, _directory_open_flags())
        destination_parent_fd = os.open(
            destination.parent,
            _directory_open_flags(),
        )
        _copy_regular_file_at(
            source_parent_fd,
            source.name,
            destination_parent_fd,
            destination.name,
            source_display=source,
        )
        os.fsync(destination_parent_fd)
    except OSError as exc:
        raise BackupUnsafeSourceError(
            f"cannot safely copy source file: {source}"
        ) from exc
    finally:
        if source_parent_fd is not None:
            os.close(source_parent_fd)
        if destination_parent_fd is not None:
            os.close(destination_parent_fd)


def _directory_change_token(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_mtime_ns, value.st_ctime_ns, value.st_size)


def _copy_directory_fd(
    source_fd: int,
    destination_fd: int,
    *,
    source_display: Path,
) -> None:
    before = os.fstat(source_fd)
    if not stat.S_ISDIR(before.st_mode):
        raise BackupUnsafeSourceError(
            f"storage source is not a directory: {source_display}"
        )
    try:
        with os.scandir(source_fd) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        raise BackupUnsafeSourceError(
            f"cannot enumerate storage directory: {source_display}"
        ) from exc

    for entry in entries:
        _validate_source_component(entry.name)
        child_display = source_display / entry.name
        try:
            entry_info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise BackupUnsafeSourceError(
                f"cannot inspect storage entry: {child_display}"
            ) from exc
        if stat.S_ISLNK(entry_info.st_mode):
            raise BackupUnsafeSourceError(
                f"storage symlink is forbidden: {child_display}"
            )
        if stat.S_ISDIR(entry_info.st_mode):
            try:
                source_child_fd = os.open(
                    entry.name,
                    _directory_open_flags(),
                    dir_fd=source_fd,
                )
            except OSError as exc:
                raise BackupUnsafeSourceError(
                    f"cannot safely open storage directory: {child_display}"
                ) from exc
            destination_child_fd: int | None = None
            try:
                opened_info = os.fstat(source_child_fd)
                if (
                    opened_info.st_dev != entry_info.st_dev
                    or opened_info.st_ino != entry_info.st_ino
                ):
                    raise BackupStateError(
                        f"storage entry changed during backup: {child_display}"
                    )
                os.mkdir(entry.name, mode=0o700, dir_fd=destination_fd)
                os.fsync(destination_fd)
                destination_entry_info = os.stat(
                    entry.name,
                    dir_fd=destination_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(destination_entry_info.st_mode):
                    raise BackupStateError(
                        f"storage destination was substituted: {child_display}"
                    )
                destination_child_fd = os.open(
                    entry.name,
                    _directory_open_flags(),
                    dir_fd=destination_fd,
                )
                os.fchmod(destination_child_fd, 0o700)
                destination_opened_info = os.fstat(destination_child_fd)
                if (
                    not _same_file_identity(
                        destination_entry_info,
                        destination_opened_info,
                    )
                    or stat.S_IMODE(destination_opened_info.st_mode) != 0o700
                ):
                    raise BackupStateError(
                        f"storage destination changed while opening: {child_display}"
                    )
                _copy_directory_fd(
                    source_child_fd,
                    destination_child_fd,
                    source_display=child_display,
                )
            finally:
                os.close(source_child_fd)
                if destination_child_fd is not None:
                    os.close(destination_child_fd)
        elif stat.S_ISREG(entry_info.st_mode):
            _copy_regular_file_at(
                source_fd,
                entry.name,
                destination_fd,
                entry.name,
                source_display=child_display,
                expected_identity=(entry_info.st_dev, entry_info.st_ino),
            )
        else:
            raise BackupUnsafeSourceError(
                f"special storage file is forbidden: {child_display}"
            )

    after = os.fstat(source_fd)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or _directory_change_token(before) != _directory_change_token(after)
    ):
        raise BackupStateError(
            f"storage directory changed during backup: {source_display}"
        )
    os.fsync(destination_fd)


def _copy_directory(source: Path, destination: Path) -> None:
    try:
        resolved_source = source.resolve(strict=True)
        resolved_destination = destination.resolve(strict=False)
    except OSError as exc:
        raise BackupUnsafeSourceError(
            f"cannot resolve storage copy paths: {source}"
        ) from exc
    if (
        resolved_source == resolved_destination
        or resolved_destination.is_relative_to(resolved_source)
        or resolved_source.is_relative_to(resolved_destination)
    ):
        raise BackupUnsafeSourceError(
            "storage source and destination trees must not overlap"
        )
    _ensure_private_directory(destination)
    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        source_fd = os.open(source, _directory_open_flags())
        destination_fd = os.open(destination, _directory_open_flags())
        _copy_directory_fd(
            source_fd,
            destination_fd,
            source_display=source,
        )
    except BackupUnsafeSourceError:
        raise
    except BackupStateError:
        raise
    except OSError as exc:
        raise BackupUnsafeSourceError(
            f"cannot safely copy storage directory: {source}"
        ) from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def _hash_resource_file_at(
    parent_fd: int,
    name: str,
    *,
    repair_permissions: bool,
    display_path: Path,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, str]:
    try:
        descriptor = os.open(
            name,
            _open_flags(os.O_RDONLY),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise BackupIntegrityError(
            f"cannot safely open backup resource: {display_path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise BackupIntegrityError(
                f"backup resource is not regular: {display_path}"
            )
        if expected_identity is not None and (
            before.st_dev,
            before.st_ino,
        ) != expected_identity:
            raise BackupIntegrityError(
                f"backup resource changed before hashing: {display_path}"
            )
        if repair_permissions:
            os.fchmod(descriptor, 0o600)
            before = os.fstat(descriptor)
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise BackupIntegrityError(
                f"backup resource must have mode 0600: {display_path}"
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, _COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or size != after.st_size
        ):
            raise BackupIntegrityError(
                f"backup resource changed while hashing: {display_path}"
            )
    finally:
        os.close(descriptor)
    return size, digest.hexdigest()


def _hash_resource_file(
    path: Path,
    *,
    repair_permissions: bool,
) -> tuple[int, str]:
    parent_fd: int | None = None
    try:
        parent_fd = os.open(path.parent, _directory_open_flags())
        return _hash_resource_file_at(
            parent_fd,
            path.name,
            repair_permissions=repair_permissions,
            display_path=path,
        )
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _scan_file_tree_fd(
    root_fd: int,
    tree_root: Path,
    *,
    manifest_prefix: str,
    repair_permissions: bool,
) -> tuple[BackupResource, ...]:
    resources: list[BackupResource] = []

    def visit(
        directory_fd: int,
        display_path: Path,
        relative_parts: tuple[str, ...],
    ) -> None:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise BackupIntegrityError(
                f"backup path is not a directory: {display_path}"
            )
        if repair_permissions:
            os.fchmod(directory_fd, 0o700)
            before = os.fstat(directory_fd)
        elif stat.S_IMODE(before.st_mode) != 0o700:
            raise BackupIntegrityError(
                f"backup directory must have mode 0700: {display_path}"
            )
        try:
            with os.scandir(directory_fd) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise BackupIntegrityError(
                f"cannot enumerate backup directory: {display_path}"
            ) from exc
        for entry in entries:
            _validate_source_component(entry.name)
            child_display = display_path / entry.name
            try:
                entry_info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise BackupIntegrityError(
                    f"cannot inspect backup resource: {child_display}"
                ) from exc
            if stat.S_ISLNK(entry_info.st_mode):
                raise BackupIntegrityError(
                    f"backup resource symlink is forbidden: {child_display}"
                )
            if stat.S_ISDIR(entry_info.st_mode):
                try:
                    child_fd = os.open(
                        entry.name,
                        _directory_open_flags(),
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    raise BackupIntegrityError(
                        f"cannot safely open backup directory: {child_display}"
                    ) from exc
                try:
                    opened_info = os.fstat(child_fd)
                    if (
                        opened_info.st_dev != entry_info.st_dev
                        or opened_info.st_ino != entry_info.st_ino
                    ):
                        raise BackupIntegrityError(
                            f"backup directory changed during hashing: {child_display}"
                        )
                    visit(
                        child_fd,
                        child_display,
                        relative_parts + (entry.name,),
                    )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(entry_info.st_mode):
                size, sha256 = _hash_resource_file_at(
                    directory_fd,
                    entry.name,
                    repair_permissions=repair_permissions,
                    display_path=child_display,
                    expected_identity=(entry_info.st_dev, entry_info.st_ino),
                )
                relative_path = "/".join(relative_parts + (entry.name,))
                resources.append(
                    BackupResource(
                        path=f"{manifest_prefix}/{relative_path}",
                        size=size,
                        sha256=sha256,
                    )
                )
            else:
                raise BackupIntegrityError(
                    f"special backup resource is forbidden: {child_display}"
                )
        after = os.fstat(directory_fd)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or _directory_change_token(before) != _directory_change_token(after)
        ):
            raise BackupIntegrityError(
                f"backup directory changed while hashing: {display_path}"
            )
        if repair_permissions:
            os.fsync(directory_fd)

    visit(root_fd, tree_root, ())
    resources.sort(key=lambda resource: resource.path)
    return tuple(resources)


def _scan_file_tree(
    tree_root: Path,
    *,
    manifest_prefix: str,
    repair_permissions: bool,
) -> tuple[BackupResource, ...]:
    root_fd: int | None = None
    try:
        root_fd = os.open(tree_root, _directory_open_flags())
    except OSError as exc:
        raise BackupIntegrityError("backup file tree is not a safe directory") from exc
    try:
        return _scan_file_tree_fd(
            root_fd,
            tree_root,
            manifest_prefix=manifest_prefix,
            repair_permissions=repair_permissions,
        )
    finally:
        os.close(root_fd)


def _scan_resource_tree(
    resource_root: Path,
    *,
    repair_permissions: bool,
) -> tuple[BackupResource, ...]:
    resources = _scan_file_tree(
        resource_root,
        manifest_prefix=RESOURCES_DIRNAME,
        repair_permissions=repair_permissions,
    )
    paths = {resource.path for resource in resources}
    if DATABASE_DUMP_RESOURCE not in paths:
        raise BackupStateError("pg_dump did not create resources/database.dump")
    return resources


def _scan_restored_storage(storage_root: Path) -> tuple[BackupResource, ...]:
    return _scan_file_tree(
        storage_root,
        manifest_prefix=f"{RESOURCES_DIRNAME}/{FILES_DIRNAME}",
        repair_permissions=False,
    )


@dataclass(frozen=True, slots=True)
class PreparedBackupSet:
    """Resources completed under the write barrier but not signed or sealed."""

    backup_id: str
    path: Path
    resources: tuple[BackupResource, ...]
    store_root: Path


@dataclass(frozen=True, slots=True)
class AuthenticatedBackupInspection:
    """Store-issued capability proving approval of the exact signer key."""

    inspection: BackupInspection
    approved_public_key: bytes
    _store_token: object = field(repr=False, compare=False)

    @property
    def path(self) -> Path:
        return self.inspection.path

    @property
    def manifest(self) -> BackupManifest:
        return self.inspection.manifest

    @property
    def signer_public_key(self) -> bytes:
        return self.inspection.signer_public_key

    @property
    def resources_verified(self) -> bool:
        return self.inspection.resources_verified


@dataclass(slots=True)
class AnchoredStagingDataDir:
    """Descriptor-anchored capability for one new staging ``data_dir``.

    ``path`` is informational only. All filesystem mutations use the pinned
    descriptors, and ``verify_attached`` proves that the configured path still
    names the exact directory chain created by the store.
    """

    path: Path
    _store: "LocalBackupStore" = field(repr=False, compare=False)
    _staging_root: Path = field(repr=False, compare=False)
    _root_identity: tuple[int, int] = field(repr=False, compare=False)
    _component_identities: tuple[tuple[str, int, int], ...] = field(
        repr=False,
        compare=False,
    )
    _root_fd: int = field(repr=False, compare=False)
    _parent_fd: int = field(repr=False, compare=False)
    _data_fd: int = field(repr=False, compare=False)
    _store_token: object = field(repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def __enter__(self) -> "AnchoredStagingDataDir":
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise BackupStateError("staging data_dir capability is closed")
        try:
            data_info = os.fstat(self._data_fd)
            parent_info = os.fstat(self._parent_fd)
        except OSError as exc:
            raise BackupStateError(
                "staging data_dir capability has invalid descriptors"
            ) from exc
        if (
            not stat.S_ISDIR(data_info.st_mode)
            or stat.S_IMODE(data_info.st_mode) != 0o700
            or not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_IMODE(parent_info.st_mode) != 0o700
        ):
            raise BackupStateError(
                "staging data_dir capability no longer pins private directories"
            )

    def _require_store(self, store: "LocalBackupStore") -> None:
        self._require_open()
        if self._store is not store or self._store_token is not store._capability_token:
            raise BackupStateError("staging data_dir belongs to another backup store")

    def verify_attached(self) -> None:
        """Fail if any visible path component was renamed or substituted."""
        self._require_open()
        _verify_anchored_directory_chain(
            self._staging_root,
            self._root_identity,
            self._component_identities,
            self._data_fd,
        )

    def fileno(self) -> int:
        """Return the pinned data-directory descriptor; callers must not close it."""
        self._require_open()
        return self._data_fd

    def parent_fileno(self) -> int:
        """Return the pinned plan-directory descriptor; callers must not close it."""
        self._require_open()
        return self._parent_fd

    def duplicate_parent_fd(self) -> int:
        """Return a caller-owned duplicate of the pinned plan directory."""
        self._require_open()
        return os.dup(self._parent_fd)

    def temporary_file(self) -> BinaryIO:
        """Create an unlinked 0600 temporary inode in the pinned plan directory."""
        self.verify_attached()
        handle = _open_anonymous_regular_file_at(self._parent_fd)
        try:
            self.verify_attached()
        except BaseException:
            handle.close()
            raise
        return handle

    def restore_files(
        self,
        authenticated: AuthenticatedBackupInspection,
    ) -> Path:
        """Restore authenticated business files through the pinned descriptor."""
        return self._store.restore_files_to(authenticated, self)

    def verify_local_file_references(
        self,
        references: tuple[tuple[str, str, str], ...],
    ) -> None:
        """Verify DB references against this exact anchored restored tree."""
        canonical = tuple(sorted(set(references)))
        self.verify_attached()
        storage_fd: int | None = None
        try:
            storage_fd = _open_exact_restored_directory_at(self._data_fd, "storage")
            if storage_fd is None:
                if canonical:
                    raise BackupIntegrityError(
                        "restored data_dir has no business-file storage"
                    )
                return

            for raw_collection_id, raw_record_id, raw_filename in canonical:
                collection_id = validate_collection_id(raw_collection_id)
                record_id = validate_record_id(raw_record_id)
                filename = validate_file_reference(raw_filename)
                collection_fd = _open_restored_storage_id_directory_at(
                    storage_fd,
                    collection_id,
                )
                if collection_fd is None:
                    raise BackupIntegrityError(
                        "database references a missing restored collection directory"
                    )
                try:
                    record_fd = _open_restored_storage_id_directory_at(
                        collection_fd,
                        record_id,
                    )
                    if record_fd is None:
                        raise BackupIntegrityError(
                            "database references a missing restored record directory"
                        )
                    try:
                        _verify_restored_regular_file_at(record_fd, filename)
                    finally:
                        os.close(record_fd)
                finally:
                    os.close(collection_fd)

            visible_storage_fd = _open_exact_restored_directory_at(
                self._data_fd,
                "storage",
            )
            if visible_storage_fd is None:
                raise BackupIntegrityError(
                    "restored business-file storage was detached during validation"
                )
            try:
                if not _same_file_identity(
                    os.fstat(storage_fd),
                    os.fstat(visible_storage_fd),
                ):
                    raise BackupIntegrityError(
                        "restored business-file storage changed during validation"
                    )
            finally:
                os.close(visible_storage_fd)
        finally:
            if storage_fd is not None:
                os.close(storage_fd)
            self.verify_attached()

    def install_jwt(
        self,
        authenticated: AuthenticatedBackupInspection,
    ) -> Path:
        """Install the authenticated disaster-recovery JWT secret."""
        return self._store.restore_jwt_secret_to(authenticated, self)

    def write_secret(
        self,
        payload: bytes,
        *,
        name: str = ".jwt_secret",
    ) -> Path:
        """Write one new private secret, primarily for clone-mode JWT rotation."""
        if not isinstance(payload, bytes):
            raise BackupStateError("staging secret payload must be bytes")
        parts = _validated_relative_directory_parts(name)
        if len(parts) != 1:
            raise BackupStateError("staging secret name must be one path component")
        self.verify_attached()
        try:
            _write_exclusive_at(self._data_fd, parts[0], payload)
        except FileExistsError as exc:
            raise BackupAlreadyExistsError(
                f"restore target already contains {parts[0]}"
            ) from exc
        self.verify_attached()
        return self.path / parts[0]

    def close(self) -> None:
        """Release the capability; restored directories remain quarantined."""
        if self._closed:
            return
        self._closed = True
        descriptors = {self._data_fd, self._parent_fd, self._root_fd}
        self._data_fd = -1
        self._parent_fd = -1
        self._root_fd = -1
        for descriptor in descriptors:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


@dataclass(slots=True)
class PinnedBackupArchive:
    """Anonymous verified archive whose inode is held open for pg_restore."""

    source_path: Path
    size: int
    sha256: str
    _handle: BinaryIO = field(repr=False, compare=False)

    def fileno(self) -> int:
        return self._handle.fileno()

    def close(self) -> None:
        self._handle.close()


class BackupSealCancelledError(BackupStateError):
    """Raised only when cancellation wins before SEALED publication."""


class BackupSealGate:
    """Make cancellation and the final SEALED publication mutually exclusive."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._committed = False

    def cancel(self) -> bool:
        """Prevent sealing, or report that the commit point already completed."""
        with self._lock:
            if self._committed:
                return False
            self._cancelled = True
            return True

    def publish(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._cancelled:
                raise BackupSealCancelledError("backup sealing was cancelled")
            callback()
            self._committed = True


class BackupSetBuilder:
    """Single-use builder for the write-barrier-protected phase."""

    def __init__(self, store: "LocalBackupStore", backup_id: str, path: Path) -> None:
        self._store = store
        self.backup_id = backup_id
        self.path = path
        self._state = "building"
        self._storage_copied = False
        self._jwt_secret_copied = False

    def _require_building(self) -> None:
        self._store._require_open()
        if self._state != "building":
            raise BackupStateError(f"backup builder is already {self._state}")

    @property
    def database_dump_path(self) -> Path:
        """Absent destination that must be passed directly to ``pg_dump``."""
        return self.path / DATABASE_DUMP_RESOURCE

    @property
    def files_path(self) -> Path:
        return self.path / RESOURCES_DIRNAME / FILES_DIRNAME

    def copy_storage(self, source: str | Path) -> None:
        """Copy a local storage tree, refusing symlinks and special files."""
        self._require_building()
        if self._storage_copied:
            raise BackupStateError("local storage was already copied")
        if any(self.files_path.iterdir()):
            raise BackupStateError("backup files destination is not empty")
        source_path = Path(source)
        try:
            source_info = source_path.lstat()
        except FileNotFoundError:
            _fsync_directory(self.files_path)
            self._storage_copied = True
            return
        except OSError as exc:
            raise BackupUnsafeSourceError("cannot inspect local storage source") from exc
        if not stat.S_ISDIR(source_info.st_mode) or source_path.is_symlink():
            raise BackupUnsafeSourceError(
                "local storage source is not a safe directory"
            )
        try:
            resolved_source = source_path.resolve(strict=True)
            resolved_destination = self.files_path.resolve(strict=True)
        except OSError as exc:
            raise BackupUnsafeSourceError("cannot resolve local storage source") from exc
        if (
            resolved_destination == resolved_source
            or resolved_destination.is_relative_to(resolved_source)
            or resolved_source.is_relative_to(resolved_destination)
        ):
            raise BackupUnsafeSourceError(
                "local storage source and backup destination must not overlap"
            )
        _copy_directory(source_path, self.files_path)
        self._storage_copied = True

    def copy_jwt_secret(self, source: str | Path) -> None:
        """Copy ``.jwt_secret`` as a 0600 resource for disaster recovery."""
        self._require_building()
        if self._jwt_secret_copied:
            raise BackupStateError("JWT secret was already copied")
        secret_parent = self.path / RESOURCES_DIRNAME / "secrets"
        _ensure_private_directory(secret_parent)
        destination = self.path / JWT_SECRET_RESOURCE
        _copy_regular_file(Path(source), destination)
        _fsync_directory(secret_parent)
        self._jwt_secret_copied = True

    def prepare(self) -> PreparedBackupSet:
        """Hash and fsync every resource without signing or sealing the set."""
        self._require_building()
        resources = _scan_resource_tree(
            self.path / RESOURCES_DIRNAME,
            repair_permissions=True,
        )
        _fsync_directory(self.path)
        self._state = "prepared"
        return PreparedBackupSet(
            backup_id=self.backup_id,
            path=self.path,
            resources=resources,
            store_root=self._store.root,
        )

    def abort(self) -> None:
        """Remove only this unsealed partial set."""
        if self._state == "aborted":
            return
        if self.path.parent != self._store.sets_dir or not self.path.name.startswith(
            ".partial-"
        ):
            raise BackupStateError("refusing to remove a path outside backup staging")
        if _path_exists(self.path):
            shutil.rmtree(self.path)
            _fsync_directory(self._store.sets_dir)
        self._state = "aborted"


class LocalBackupStore:
    """Canonical local backup set store; transport is intentionally separate."""

    def __init__(
        self,
        root: str | Path,
        identity: BackupIdentity | None = None,
        identity_guard: Callable[[], None] | None = None,
    ) -> None:
        self._closed = False
        self._owned_control_root: ControlPlaneRoot | None = None
        self._owns_identity = False
        self.root = Path(root)
        _ensure_private_directory(self.root)
        self.sets_dir = self.root / "sets"
        _ensure_private_directory(self.sets_dir)
        if identity is None:
            try:
                control_root = ControlPlaneRoot.open(self.root / "control")
            except ControlPlaneSafetyError as exc:
                raise BackupIdentityError(str(exc)) from exc
            try:
                identity = BackupIdentity.load_or_create_in_root(control_root)
            except BaseException:
                control_root.close()
                raise
            self._owned_control_root = control_root
            self._owns_identity = True
        self.identity = identity
        self._identity_guard = (
            identity_guard
            if identity_guard is not None
            else (
                self.identity.verify_attached
                if self.identity.control_plane_attached
                else None
            )
        )
        self._capability_token = object()
        _fsync_directory(self.root)

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        control_root = self._owned_control_root
        self._owned_control_root = None
        try:
            if self._owns_identity:
                self.identity.close()
        finally:
            if control_root is not None:
                control_root.close()

    def _require_open(self) -> None:
        if self._closed:
            raise BackupStateError("local backup store is closed")

    def __enter__(self) -> "LocalBackupStore":
        self._require_open()
        if self._identity_guard is not None:
            self._identity_guard()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def open_staging_data_dir(
        self,
        staging_root: str | Path,
        relative_target: str | Path,
    ) -> AnchoredStagingDataDir:
        """Exclusively create and pin a new data_dir below a safe staging root.

        The staging root must already exist as an absolute 0700 directory. Each
        relative component is validated, created with ``mkdirat``, and opened
        with ``openat(O_NOFOLLOW)`` before the next component is considered.
        """
        self._require_open()
        root_path = Path(staging_root)
        parts = _validated_relative_directory_parts(relative_target)
        root_fd: int | None = None
        opened_fds: set[int] = set()
        try:
            root_fd, root_identity = _open_private_staging_root(root_path)
            opened_fds.add(root_fd)
            current_fd = root_fd
            component_identities: list[tuple[str, int, int]] = []
            parent_source_fd: int | None = None

            display_path = root_path
            for index, part in enumerate(parts):
                if index == len(parts) - 1:
                    parent_source_fd = current_fd
                display_path = display_path / part
                child_fd, child_identity = _open_new_private_directory_at(
                    current_fd,
                    part,
                    display_path=display_path,
                )
                opened_fds.add(child_fd)
                component_identities.append(
                    (part, child_identity[0], child_identity[1])
                )
                current_fd = child_fd

            if parent_source_fd is None:
                raise BackupStateError("staging target has no parent directory")
            parent_fd = os.dup(parent_source_fd)
            opened_fds.add(parent_fd)
            data_fd = current_fd
            keep = {root_fd, parent_fd, data_fd}
            for descriptor in tuple(opened_fds - keep):
                os.close(descriptor)
                opened_fds.remove(descriptor)

            target = AnchoredStagingDataDir(
                path=root_path.joinpath(*parts),
                _store=self,
                _staging_root=root_path,
                _root_identity=root_identity,
                _component_identities=tuple(component_identities),
                _root_fd=root_fd,
                _parent_fd=parent_fd,
                _data_fd=data_fd,
                _store_token=self._capability_token,
            )
            opened_fds.clear()
            root_fd = None
            try:
                target.verify_attached()
            except BaseException:
                target.close()
                raise
            return target
        finally:
            for descriptor in opened_fds:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def restore_files_to(
        self,
        authenticated: AuthenticatedBackupInspection,
        target: AnchoredStagingDataDir,
    ) -> Path:
        """Restore business files into a descriptor-anchored staging target."""
        self._require_open()
        target._require_store(self)
        target.verify_attached()
        inspection = self._reinspect(authenticated)
        source = inspection.path / RESOURCES_DIRNAME / FILES_DIRNAME

        try:
            with os.scandir(target.fileno()) as iterator:
                if next(iterator, None) is not None:
                    raise BackupAlreadyExistsError(
                        "restore staging data_dir is not empty"
                    )
        except BackupError:
            raise
        except OSError as exc:
            raise BackupStateError(
                "cannot inspect anchored restore data_dir"
            ) from exc

        source_fd: int | None = None
        storage_fd: int | None = None
        try:
            source_fd = os.open(source, _directory_open_flags())
            storage_fd, storage_identity = _open_new_private_directory_at(
                target.fileno(),
                "storage",
                display_path=target.path / "storage",
            )
            _copy_directory_fd(
                source_fd,
                storage_fd,
                source_display=source,
            )

            expected = tuple(
                resource
                for resource in inspection.manifest.resources
                if resource.path.startswith(
                    f"{RESOURCES_DIRNAME}/{FILES_DIRNAME}/"
                )
            )
            actual = _scan_file_tree_fd(
                storage_fd,
                target.path / "storage",
                manifest_prefix=f"{RESOURCES_DIRNAME}/{FILES_DIRNAME}",
                repair_permissions=False,
            )
            if actual != expected:
                raise BackupIntegrityError(
                    "restored business-file checksums do not match"
                )

            visible_storage = os.stat(
                "storage",
                dir_fd=target.fileno(),
                follow_symlinks=False,
            )
            pinned_storage = os.fstat(storage_fd)
            if (
                not stat.S_ISDIR(visible_storage.st_mode)
                or (visible_storage.st_dev, visible_storage.st_ino)
                != storage_identity
                or not _same_file_identity(visible_storage, pinned_storage)
            ):
                raise BackupStateError(
                    "restored storage directory was renamed or substituted"
                )
            os.fsync(target.fileno())
            target.verify_attached()
            return target.path
        except BackupError:
            raise
        except OSError as exc:
            raise BackupStateError(
                "business files could not be restored safely"
            ) from exc
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if storage_fd is not None:
                os.close(storage_fd)

    def restore_jwt_secret_to(
        self,
        authenticated: AuthenticatedBackupInspection,
        target: AnchoredStagingDataDir,
    ) -> Path:
        """Install a signed DR JWT secret through the pinned data-dir FD."""
        self._require_open()
        target._require_store(self)
        target.verify_attached()
        inspection = self._reinspect(authenticated)
        matches = [
            resource
            for resource in inspection.manifest.resources
            if resource.path == JWT_SECRET_RESOURCE
        ]
        if len(matches) != 1:
            raise BackupNotFoundError(
                "backup set has no disaster-recovery JWT secret"
            )
        expected = matches[0]
        source = inspection.path / JWT_SECRET_RESOURCE

        source_parent_fd: int | None = None
        try:
            source_parent_fd = os.open(source.parent, _directory_open_flags())
            _copy_regular_file_at(
                source_parent_fd,
                source.name,
                target.fileno(),
                ".jwt_secret",
                source_display=source,
            )
            size, sha256 = _hash_resource_file_at(
                target.fileno(),
                ".jwt_secret",
                repair_permissions=False,
                display_path=target.path / ".jwt_secret",
            )
            if size != expected.size or sha256 != expected.sha256:
                os.unlink(".jwt_secret", dir_fd=target.fileno())
                os.fsync(target.fileno())
                raise BackupIntegrityError(
                    "restored JWT secret checksum does not match"
                )
            os.fsync(target.fileno())
            target.verify_attached()
            return target.path / ".jwt_secret"
        except FileExistsError as exc:
            raise BackupAlreadyExistsError(
                "restore target already contains .jwt_secret"
            ) from exc
        except BackupError:
            raise
        except OSError as exc:
            raise BackupStateError(
                "JWT secret could not be restored safely"
            ) from exc
        finally:
            if source_parent_fd is not None:
                os.close(source_parent_fd)

    def begin_set(self, backup_id: str | None = None) -> BackupSetBuilder:
        """Create a private partial set; no listing API can observe it."""
        self._require_open()
        if self._identity_guard is not None:
            self._identity_guard()
        selected_id = validate_backup_id(backup_id or self._generate_backup_id())
        final_path = self.sets_dir / selected_id
        if _path_exists(final_path):
            raise BackupAlreadyExistsError(f"backup set already exists: {selected_id}")

        partial_name = f".partial-{selected_id}-{secrets.token_hex(8)}"
        partial_path = self.sets_dir / partial_name
        partial_path.mkdir(mode=0o700, exist_ok=False)
        os.chmod(partial_path, 0o700, follow_symlinks=False)
        resources_path = partial_path / RESOURCES_DIRNAME
        files_path = resources_path / FILES_DIRNAME
        resources_path.mkdir(mode=0o700)
        files_path.mkdir(mode=0o700)
        os.chmod(resources_path, 0o700, follow_symlinks=False)
        os.chmod(files_path, 0o700, follow_symlinks=False)
        _fsync_directory(files_path)
        _fsync_directory(resources_path)
        _fsync_directory(partial_path)
        _fsync_directory(self.sets_dir)
        return BackupSetBuilder(self, selected_id, partial_path)

    def finalize_set(
        self,
        prepared: PreparedBackupSet,
        *,
        metadata: Mapping[str, JsonValue] | None = None,
        created_at: datetime | None = None,
        seal_gate: BackupSealGate | None = None,
        identity_guard: Callable[[], None] | None = None,
    ) -> BackupInspection:
        """Revalidate, sign, publish and finally seal a prepared set."""
        self._require_open()
        prepared_identity = self._validate_prepared_location(prepared)
        final_path: Path | None = None
        final_identity: tuple[int, int] | None = None
        sealed_committed = False
        effective_identity_guard = identity_guard or self._identity_guard
        try:
            if effective_identity_guard is not None:
                effective_identity_guard()
            current_resources = _scan_resource_tree(
                prepared.path / RESOURCES_DIRNAME,
                repair_permissions=False,
            )
            if current_resources != prepared.resources:
                raise BackupIntegrityError(
                    "prepared backup resources changed after write-barrier release"
                )

            manifest = BackupManifest(
                backup_id=prepared.backup_id,
                created_at=format_manifest_timestamp(created_at or datetime.now(UTC)),
                signer_fingerprint_sha256=self.identity.fingerprint_sha256,
                resources=current_resources,
                metadata=metadata or {},
            )
            manifest_bytes = manifest.to_bytes()
            if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
                raise BackupStateError(
                    "generated backup manifest exceeds its size limit"
                )
            signature = self.identity.sign_manifest(manifest_bytes)
            if effective_identity_guard is not None:
                effective_identity_guard()

            _write_exclusive(prepared.path / MANIFEST_FILENAME, manifest_bytes)
            _write_exclusive(prepared.path / SIGNATURE_FILENAME, signature)
            _write_exclusive(
                prepared.path / SIGNER_PUBLIC_KEY_FILENAME,
                self.identity.public_key_bytes,
            )
            _fsync_directory(prepared.path)

            final_path, final_identity = self._publish_unsealed(prepared)
            inspection = self._inspect_set_path(
                prepared.backup_id,
                final_path,
                expected_public_key=self.identity.public_key_bytes,
                verify_resources=True,
                sealed=False,
            )

            def seal() -> None:
                self._publish_seal(
                    final_path,
                    pre_commit_guard=effective_identity_guard,
                )

            if seal_gate is None:
                seal()
            else:
                seal_gate.publish(seal)
            sealed_committed = True
            return inspection
        except BaseException as exc:
            if sealed_committed:
                raise
            cleanup_failed = False
            if final_path is not None and final_identity is not None:
                try:
                    self._remove_owned_directory(final_path, final_identity)
                except Exception:
                    cleanup_failed = True
            try:
                self._remove_owned_directory(prepared.path, prepared_identity)
            except Exception:
                cleanup_failed = True
            if cleanup_failed:
                raise BackupStateError(
                    "failed backup finalization could not be cleaned safely"
                ) from exc
            raise

    def list_sets(self) -> list[BackupSetSummary]:
        """List structurally authenticated sealed sets, never partial sets."""
        self._require_open()
        if self._identity_guard is not None:
            self._identity_guard()
        summaries: list[BackupSetSummary] = []
        try:
            with os.scandir(self.sets_dir) as iterator:
                entries = sorted(
                    iterator,
                    key=lambda entry: entry.name,
                    reverse=True,
                )
        except OSError as exc:
            raise BackupStateError("cannot enumerate local backup sets") from exc
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                backup_id = validate_backup_id(entry.name)
            except BackupManifestError:
                continue
            if not self._has_seal(Path(entry.path)):
                continue
            try:
                inspection = self.inspect_set(
                    backup_id,
                    expected_public_key=self.identity.public_key_bytes,
                    verify_resources=False,
                )
            except (BackupError, OSError):
                summaries.append(
                    BackupSetSummary(
                        backup_id=backup_id,
                        created_at=None,
                        signer_fingerprint_sha256=None,
                        resource_count=None,
                        total_size=None,
                        integrity_status="invalid",
                        error_code="integrity_failed",
                    )
                )
                continue
            manifest = inspection.manifest
            summaries.append(
                BackupSetSummary(
                    backup_id=manifest.backup_id,
                    created_at=manifest.created_at,
                    signer_fingerprint_sha256=manifest.signer_fingerprint_sha256,
                    resource_count=len(manifest.resources),
                    total_size=manifest.total_size,
                    integrity_status="valid",
                    error_code=None,
                )
            )
        if self._identity_guard is not None:
            self._identity_guard()
        return summaries

    def inspect_set(
        self,
        backup_id: str,
        *,
        expected_public_key: bytes | None = None,
        verify_resources: bool = True,
    ) -> BackupInspection:
        """Verify canonical manifest, detached signature and resource checksums."""
        self._require_open()
        selected_id = validate_backup_id(backup_id)
        set_path = self.sets_dir / selected_id
        if not self._has_seal(set_path):
            raise BackupNotFoundError(f"sealed backup set not found: {selected_id}")
        return self._inspect_set_path(
            selected_id,
            set_path,
            expected_public_key=expected_public_key,
            verify_resources=verify_resources,
            sealed=True,
        )

    def _inspect_set_path(
        self,
        selected_id: str,
        set_path: Path,
        *,
        expected_public_key: bytes | None,
        verify_resources: bool,
        sealed: bool,
    ) -> BackupInspection:
        """Fully inspect one published set before or after its seal commit."""
        self._validate_set_layout(set_path, sealed=sealed)

        manifest_bytes = _read_regular_file(
            set_path / MANIFEST_FILENAME,
            maximum_size=_MAX_MANIFEST_BYTES,
        )
        signature = _read_regular_file(
            set_path / SIGNATURE_FILENAME,
            maximum_size=ED25519_SIGNATURE_SIZE,
        )
        public_key = _read_regular_file(
            set_path / SIGNER_PUBLIC_KEY_FILENAME,
            maximum_size=ED25519_PUBLIC_KEY_SIZE,
        )
        manifest = BackupManifest.from_bytes(manifest_bytes)
        if manifest.backup_id != selected_id:
            raise BackupIntegrityError("manifest backup_id does not match its directory")
        if expected_public_key is not None and not secrets.compare_digest(
            public_key,
            expected_public_key,
        ):
            raise BackupIntegrityError("backup signer does not match the expected key")
        fingerprint = verify_manifest_signature(
            manifest_bytes,
            signature,
            public_key,
        )
        if fingerprint != manifest.signer_fingerprint_sha256:
            raise BackupIntegrityError("manifest signer fingerprint is inconsistent")

        if verify_resources:
            actual_resources = _scan_resource_tree(
                set_path / RESOURCES_DIRNAME,
                repair_permissions=False,
            )
            if actual_resources != manifest.resources:
                raise BackupIntegrityError("backup resource checksum validation failed")
        return BackupInspection(
            path=set_path,
            manifest=manifest,
            signer_public_key=public_key,
            resources_verified=verify_resources,
        )

    def authenticate_set(
        self,
        backup_id: str,
        *,
        approved_public_key: bytes,
    ) -> AuthenticatedBackupInspection:
        """Issue a restore capability after explicit signer-key approval."""
        self._require_open()
        if (
            not isinstance(approved_public_key, bytes)
            or len(approved_public_key) != ED25519_PUBLIC_KEY_SIZE
        ):
            raise BackupIntegrityError(
                "approved Ed25519 public key must contain exactly 32 bytes"
            )
        inspection = self.inspect_set(
            backup_id,
            expected_public_key=approved_public_key,
            verify_resources=True,
        )
        return AuthenticatedBackupInspection(
            inspection=inspection,
            approved_public_key=approved_public_key,
            _store_token=self._capability_token,
        )

    def pin_database_dump(
        self,
        authenticated: AuthenticatedBackupInspection,
        *,
        directory: str | Path | AnchoredStagingDataDir,
    ) -> PinnedBackupArchive:
        """Copy the authenticated dump into an anonymous verified inode.

        The returned descriptor, not a reopenable path, is the only input that
        should be handed to ``pg_restore``. The caller must close it.
        """
        self._require_open()
        inspection = self._reinspect(authenticated)
        matches = [
            resource
            for resource in inspection.manifest.resources
            if resource.path == DATABASE_DUMP_RESOURCE
        ]
        if len(matches) != 1:
            raise BackupIntegrityError("backup set has no unique database dump")
        expected = matches[0]

        anchored_target: AnchoredStagingDataDir | None = None
        temporary_directory: Path | None = None
        if isinstance(directory, AnchoredStagingDataDir):
            directory._require_store(self)
            directory.verify_attached()
            anchored_target = directory
        else:
            temporary_directory = Path(directory)
            try:
                directory_info = temporary_directory.lstat()
            except OSError as exc:
                raise BackupStateError(
                    "pinned-archive directory does not exist"
                ) from exc
            if (
                not stat.S_ISDIR(directory_info.st_mode)
                or temporary_directory.is_symlink()
                or stat.S_IMODE(directory_info.st_mode) != 0o700
            ):
                raise BackupStateError(
                    "pinned-archive directory must be a safe 0700 directory"
                )

        source = inspection.path / DATABASE_DUMP_RESOURCE
        handle: BinaryIO | None = None
        source_parent_fd: int | None = None
        source_fd: int | None = None
        try:
            if anchored_target is not None:
                handle = anchored_target.temporary_file()
            else:
                handle = tempfile.TemporaryFile(mode="w+b", dir=temporary_directory)
            os.fchmod(handle.fileno(), 0o600)
            source_parent_fd = os.open(source.parent, _directory_open_flags())
            source_fd = os.open(
                source.name,
                _open_flags(os.O_RDONLY),
                dir_fd=source_parent_fd,
            )
            before = os.fstat(source_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise BackupIntegrityError(
                    "database dump is not a private regular file"
                )

            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(source_fd, _COPY_BUFFER_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                handle.write(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())

            after = os.fstat(source_fd)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or size != after.st_size
            ):
                raise BackupIntegrityError(
                    "database dump changed while pinning restore input"
                )
            actual_sha256 = digest.hexdigest()
            if size != expected.size or actual_sha256 != expected.sha256:
                raise BackupIntegrityError(
                    "pinned database dump differs from the signed manifest"
                )
            handle.seek(0)
            if anchored_target is not None:
                anchored_target.verify_attached()
            pinned = PinnedBackupArchive(
                source_path=source,
                size=size,
                sha256=actual_sha256,
                _handle=handle,
            )
            handle = None
            return pinned
        except BackupError:
            raise
        except OSError as exc:
            raise BackupStateError("database dump could not be pinned safely") from exc
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if source_parent_fd is not None:
                os.close(source_parent_fd)
            if handle is not None:
                handle.close()

    def restore_files(
        self,
        authenticated: AuthenticatedBackupInspection,
        target_data_dir: str | Path,
    ) -> Path:
        """Restore files only from a store-issued authenticated capability."""
        self._require_open()
        inspection = self._reinspect(authenticated)
        source = inspection.path / RESOURCES_DIRNAME / FILES_DIRNAME
        target = Path(target_data_dir)
        if _path_exists(target):
            raise BackupAlreadyExistsError(
                f"restore data_dir must not already exist: {target}"
            )

        try:
            resolved_source = source.resolve(strict=True)
            resolved_target = target.resolve(strict=False)
        except OSError as exc:
            raise BackupStateError("cannot resolve restore target") from exc
        if (
            resolved_target == resolved_source
            or resolved_target.is_relative_to(resolved_source)
            or resolved_source.is_relative_to(resolved_target)
        ):
            raise BackupStateError("restore target cannot be inside backup resources")

        try:
            _create_private_directory_tree_exclusive(target)
        except FileExistsError as exc:
            raise BackupAlreadyExistsError(
                f"restore data_dir must not already exist: {target}"
            ) from exc
        storage_target = target / "storage"
        _copy_directory(source, storage_target)

        expected = tuple(
            resource
            for resource in inspection.manifest.resources
            if resource.path.startswith(f"{RESOURCES_DIRNAME}/{FILES_DIRNAME}/")
        )
        actual = _scan_restored_storage(storage_target)
        if actual != expected:
            raise BackupIntegrityError("restored business-file checksums do not match")
        _fsync_directory(target)
        return target

    def restore_jwt_secret(
        self,
        authenticated: AuthenticatedBackupInspection,
        target_data_dir: str | Path,
    ) -> Path:
        """Install the preserved JWT secret for disaster-recovery mode only."""
        self._require_open()
        inspection = self._reinspect(authenticated)
        matches = [
            resource
            for resource in inspection.manifest.resources
            if resource.path == JWT_SECRET_RESOURCE
        ]
        if len(matches) != 1:
            raise BackupNotFoundError("backup set has no disaster-recovery JWT secret")
        expected = matches[0]

        target = Path(target_data_dir)
        try:
            target_info = target.lstat()
        except OSError as exc:
            raise BackupStateError(
                "restore data_dir must exist before installing the JWT secret"
            ) from exc
        if (
            not stat.S_ISDIR(target_info.st_mode)
            or target.is_symlink()
            or stat.S_IMODE(target_info.st_mode) != 0o700
        ):
            raise BackupStateError("restore data_dir must be a safe 0700 directory")

        destination = target / ".jwt_secret"
        if _path_exists(destination):
            raise BackupAlreadyExistsError(
                "restore target already contains .jwt_secret"
            )
        source = inspection.path / JWT_SECRET_RESOURCE
        _copy_regular_file(source, destination)
        size, sha256 = _hash_resource_file(
            destination,
            repair_permissions=False,
        )
        if size != expected.size or sha256 != expected.sha256:
            destination.unlink()
            _fsync_directory(target)
            raise BackupIntegrityError("restored JWT secret checksum does not match")
        _fsync_directory(target)
        return destination

    @staticmethod
    def _generate_backup_id() -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{timestamp}-{secrets.token_hex(6)}"

    def _validate_prepared_location(
        self,
        prepared: PreparedBackupSet,
    ) -> tuple[int, int]:
        if not isinstance(prepared, PreparedBackupSet):
            raise BackupStateError("finalize_set requires a PreparedBackupSet")
        validate_backup_id(prepared.backup_id)
        if prepared.store_root != self.root:
            raise BackupStateError("prepared backup belongs to another store")
        if (
            prepared.path.parent != self.sets_dir
            or not prepared.path.name.startswith(f".partial-{prepared.backup_id}-")
        ):
            raise BackupStateError("prepared backup path is outside this store")
        try:
            info = prepared.path.lstat()
        except OSError as exc:
            raise BackupStateError("prepared backup path no longer exists") from exc
        if not stat.S_ISDIR(info.st_mode) or prepared.path.is_symlink():
            raise BackupStateError("prepared backup path is not a safe directory")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise BackupStateError("prepared backup directory must have mode 0700")
        return info.st_dev, info.st_ino

    @staticmethod
    def _remove_owned_directory(
        path: Path,
        expected_identity: tuple[int, int],
    ) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BackupStateError(
                "failed backup directory could not be inspected safely"
            ) from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or path.is_symlink()
            or (info.st_dev, info.st_ino) != expected_identity
        ):
            raise BackupStateError(
                "failed backup directory changed before cleanup"
            )
        try:
            shutil.rmtree(path)
            _fsync_directory(path.parent)
        except OSError as exc:
            raise BackupStateError(
                "failed backup directory could not be removed safely"
            ) from exc

    def _reinspect(
        self,
        authenticated: AuthenticatedBackupInspection,
    ) -> BackupInspection:
        if not isinstance(authenticated, AuthenticatedBackupInspection):
            raise BackupStateError(
                "restore requires an authenticated backup capability"
            )
        if authenticated._store_token is not self._capability_token:
            raise BackupStateError("backup capability belongs to another store")
        inspection = authenticated.inspection
        expected_path = self.sets_dir / inspection.manifest.backup_id
        if inspection.path != expected_path:
            raise BackupStateError("backup inspection belongs to another store")
        return self.inspect_set(
            inspection.manifest.backup_id,
            expected_public_key=authenticated.approved_public_key,
            verify_resources=True,
        )

    def _publish_unsealed(
        self,
        prepared: PreparedBackupSet,
    ) -> tuple[Path, tuple[int, int]]:
        expected_children = {
            MANIFEST_FILENAME,
            SIGNATURE_FILENAME,
            SIGNER_PUBLIC_KEY_FILENAME,
            RESOURCES_DIRNAME,
        }
        with os.scandir(prepared.path) as iterator:
            actual_children = {entry.name for entry in iterator}
        if actual_children != expected_children:
            raise BackupStateError("prepared backup contains unexpected top-level files")

        final_path = self.sets_dir / prepared.backup_id
        try:
            final_path.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError as exc:
            raise BackupAlreadyExistsError(
                f"backup set already exists: {prepared.backup_id}"
            ) from exc
        final_identity: tuple[int, int] | None = None
        try:
            created_info = final_path.lstat()
            if not stat.S_ISDIR(created_info.st_mode) or final_path.is_symlink():
                raise BackupStateError(
                    "new unsealed backup directory was substituted"
                )
            final_identity = (created_info.st_dev, created_info.st_ino)
            os.chmod(final_path, 0o700, follow_symlinks=False)
            _fsync_directory(self.sets_dir)

            for name in sorted(expected_children):
                os.rename(prepared.path / name, final_path / name)
            _fsync_directory(final_path)
            prepared.path.rmdir()
            _fsync_directory(self.sets_dir)
            return final_path, final_identity
        except BaseException as exc:
            try:
                if final_identity is None:
                    current_info = final_path.lstat()
                    if (
                        not stat.S_ISDIR(current_info.st_mode)
                        or final_path.is_symlink()
                    ):
                        raise BackupStateError(
                            "new unsealed backup directory changed before cleanup"
                        )
                    final_identity = (current_info.st_dev, current_info.st_ino)
                self._remove_owned_directory(final_path, final_identity)
            except FileNotFoundError:
                pass
            except Exception:
                raise BackupStateError(
                    "unsealed backup publication could not be cleaned safely"
                ) from exc
            raise

    def _publish_seal(
        self,
        final_path: Path,
        *,
        pre_commit_guard: Callable[[], None] | None = None,
    ) -> None:
        """Publish SEALED only after its private inode is durably prepared."""
        temporary = final_path / f".{SEALED_FILENAME}-{secrets.token_hex(8)}.tmp"
        _write_exclusive(temporary, b"")
        _fsync_directory(final_path)
        try:
            if pre_commit_guard is not None:
                pre_commit_guard()
            os.link(
                temporary,
                final_path / SEALED_FILENAME,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise BackupStateError("backup set already contains a seal") from exc
        temporary.unlink()
        _fsync_directory(final_path)
        _fsync_directory(self.sets_dir)

    @staticmethod
    def _has_seal(set_path: Path) -> bool:
        try:
            set_info = set_path.lstat()
            seal_info = (set_path / SEALED_FILENAME).lstat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(set_info.st_mode)
            and not set_path.is_symlink()
            and stat.S_IMODE(set_info.st_mode) == 0o700
            and stat.S_ISREG(seal_info.st_mode)
            and not (set_path / SEALED_FILENAME).is_symlink()
            and seal_info.st_size == 0
            and stat.S_IMODE(seal_info.st_mode) == 0o600
        )

    @staticmethod
    def _validate_set_layout(set_path: Path, *, sealed: bool) -> None:
        expected_children = {
            MANIFEST_FILENAME,
            SIGNATURE_FILENAME,
            SIGNER_PUBLIC_KEY_FILENAME,
            RESOURCES_DIRNAME,
        }
        if sealed:
            expected_children.add(SEALED_FILENAME)
        try:
            set_info = set_path.lstat()
            if (
                not stat.S_ISDIR(set_info.st_mode)
                or set_path.is_symlink()
                or stat.S_IMODE(set_info.st_mode) != 0o700
            ):
                raise BackupIntegrityError(
                    "published backup set is not a private directory"
                )
            with os.scandir(set_path) as iterator:
                actual_children = {entry.name for entry in iterator}
        except BackupError:
            raise
        except OSError as exc:
            raise BackupIntegrityError("cannot enumerate published backup set") from exc
        if actual_children != expected_children:
            raise BackupIntegrityError("published backup set has unexpected files")
