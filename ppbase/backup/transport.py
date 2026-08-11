"""Standard ZIP transport for native PPBase backups."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import struct
import tempfile
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import BinaryIO, Callable, Iterator

from ppbase.backup.models import (
    BackupError,
    BackupIntegrityError,
    BackupManifest,
    BackupManifestError,
    BackupResource,
)
from ppbase.backup.storage import (
    MANIFEST_FILENAME,
    SEALED_FILENAME,
    BackupSetBuilder,
    LocalBackupStore,
    PreparedBackupSet,
)


_ENVELOPE_NAMES = (MANIFEST_FILENAME,)
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_SAFE_APP_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_TRANSPORT_METADATA_KEY = "transport"
_TRANSPORT_FILENAME_KEY = "filename"
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_CENTRAL_DIRECTORY_STRUCT = struct.Struct("<4s4B4HL2L5H2L")
_EOCD_STRUCT = struct.Struct("<4s4H2LH")
_ZIP64_EOCD_STRUCT = struct.Struct("<4sQ2H2L4Q")
_ZIP64_LOCATOR_STRUCT = struct.Struct("<4sLQL")


class BackupTransportError(BackupError):
    """Stable transport failure that is safe to expose through the API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BackupTransportLimits:
    """Hard limits applied before and during ZIP ingestion."""

    max_upload_bytes: int
    max_uncompressed_bytes: int
    max_resource_bytes: int
    max_entries: int
    max_central_directory_bytes: int
    max_compression_ratio: int
    chunk_size: int

    def __post_init__(self) -> None:
        for name in (
            "max_upload_bytes",
            "max_uncompressed_bytes",
            "max_resource_bytes",
            "max_entries",
            "max_central_directory_bytes",
            "max_compression_ratio",
            "chunk_size",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @classmethod
    def from_settings(cls, settings: object) -> "BackupTransportLimits":
        return cls(
            max_upload_bytes=int(getattr(settings, "backup_max_upload_bytes")),
            max_uncompressed_bytes=int(
                getattr(settings, "backup_max_uncompressed_bytes")
            ),
            max_resource_bytes=int(
                getattr(settings, "backup_max_resource_bytes")
            ),
            max_entries=int(getattr(settings, "backup_max_archive_entries")),
            max_central_directory_bytes=int(
                getattr(settings, "backup_max_central_directory_bytes")
            ),
            max_compression_ratio=int(
                getattr(settings, "backup_max_compression_ratio")
            ),
            chunk_size=int(getattr(settings, "backup_transport_chunk_size")),
        )


@dataclass(slots=True)
class PinnedBackupZip:
    """A complete anonymous ZIP inode, independent from the canonical set."""

    filename: str
    size: int
    _handle: BinaryIO = field(repr=False, compare=False)
    _close_callback: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def closed(self) -> bool:
        return self._closed

    def iter_bytes(self, chunk_size: int) -> Iterator[bytes]:
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or chunk_size <= 0
        ):
            raise ValueError("chunk_size must be a positive integer")
        try:
            self._handle.seek(0)
            while True:
                chunk = self._handle.read(chunk_size)
                if not chunk:
                    break
                yield bytes(chunk)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_callback = self._close_callback
        self._close_callback = None
        try:
            self._handle.close()
        except OSError:
            pass
        finally:
            if close_callback is not None:
                try:
                    close_callback()
                except Exception:
                    # Cleanup cannot replace a completed or interrupted stream.
                    pass

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """Retain owned resources until this ZIP is actually closed."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        if self._closed:
            callback()
            raise BackupTransportError(
                "backup_transport_closed",
                "The prepared backup ZIP is already closed.",
            )
        previous = self._close_callback
        if previous is None:
            self._close_callback = callback
            return

        def close_callbacks() -> None:
            try:
                callback()
            finally:
                previous()

        self._close_callback = close_callbacks

    def __enter__(self) -> "PinnedBackupZip":
        if self._closed:
            raise BackupTransportError(
                "backup_transport_closed",
                "The prepared backup ZIP is already closed.",
            )
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(slots=True)
class PreparedBackupImport:
    """A fully verified hidden workspace awaiting publication."""

    builder: BackupSetBuilder
    prepared: PreparedBackupSet
    manifest_bytes: bytes

    def abort(self) -> None:
        self.builder.abort()


def backup_transport_filename_from_parts(
    backup_id: str,
    created_at: str,
    app_name: str | None,
) -> str:
    """Return a deterministic, header-safe user-visible ZIP basename."""
    raw_app_name = app_name or "ppbase"
    app_name = str(raw_app_name or "ppbase").strip().casefold()
    slug = _SAFE_APP_SLUG_RE.sub("_", app_name).strip("_")[:48] or "ppbase"
    created = datetime.strptime(
        created_at,
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ).strftime("%Y%m%dT%H%M%SZ")
    short_id = _SAFE_APP_SLUG_RE.sub("_", backup_id.casefold()).strip("_")
    short_id = short_id[:12] or "backup"
    return f"ppbase_backup_{slug}_{created}_{short_id}.zip"


def validate_backup_transport_filename(value: str) -> str:
    """Validate a user-visible ZIP basename without turning it into a path."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("backup filename must be a non-empty basename")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError("backup filename must use NFC Unicode normalization")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("backup filename must not contain path components")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("backup filename must not contain control characters")
    if not value.casefold().endswith(".zip"):
        raise ValueError("backup filename must end in .zip")
    if len(value.encode("utf-8")) > 255:
        raise ValueError("backup filename is too long")
    return value


def backup_transport_filename(manifest: BackupManifest) -> str:
    transport = manifest.metadata.get(_TRANSPORT_METADATA_KEY)
    if isinstance(transport, dict):
        requested = transport.get(_TRANSPORT_FILENAME_KEY)
        if isinstance(requested, str):
            try:
                return validate_backup_transport_filename(requested)
            except ValueError:
                # Older or externally-authored manifests may contain transport
                # metadata PPBase doesn't understand. Never reflect it into a
                # response header or use it as a filesystem path.
                pass
    return backup_transport_filename_from_parts(
        manifest.backup_id,
        manifest.created_at,
        str(manifest.metadata.get("app_name", "") or "") or None,
    )


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _open_relative_regular_file(set_fd: int, member_name: str) -> int:
    parts = PurePosixPath(member_name).parts
    if not parts:
        raise BackupIntegrityError("backup ZIP member path is empty")
    current_fd = os.dup(set_fd)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            info = os.fstat(next_fd)
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                os.close(next_fd)
                raise BackupIntegrityError(
                    "backup ZIP source contains an unsafe directory"
                )
            os.close(current_fd)
            current_fd = next_fd
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=current_fd,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            os.close(descriptor)
            raise BackupIntegrityError(
                "backup ZIP source contains an unsafe regular file"
            )
        return descriptor
    except BackupError:
        raise
    except OSError as exc:
        raise BackupIntegrityError(
            "backup ZIP source could not be opened safely"
        ) from exc
    finally:
        os.close(current_fd)


def _read_exact_member(
    set_fd: int,
    name: str,
    *,
    maximum_size: int,
) -> bytes:
    descriptor = _open_relative_regular_file(set_fd, name)
    try:
        before = os.fstat(descriptor)
        if before.st_size > maximum_size:
            raise BackupIntegrityError(f"backup envelope member is too large: {name}")
        chunks: list[bytes] = []
        remaining = maximum_size + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) > maximum_size
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(payload) != after.st_size
        ):
            raise BackupIntegrityError(
                f"backup envelope member changed while reading: {name}"
            )
        return payload
    finally:
        os.close(descriptor)


def materialize_backup_zip(
    store: LocalBackupStore,
    backup_id: str,
    *,
    chunk_size: int,
) -> PinnedBackupZip:
    """Build a complete verified ZIP without persisting it canonically."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    inspection = store.inspect_set(
        backup_id,
        verify_resources=True,
    )
    handle: BinaryIO | None = None
    set_fd: int | None = None
    try:
        handle = tempfile.TemporaryFile(mode="w+b", dir=store.root)
        os.fchmod(handle.fileno(), 0o600)
        set_fd = os.open(
            inspection.path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        set_info = os.fstat(set_fd)
        visible_info = inspection.path.lstat()
        if (
            not stat.S_ISDIR(set_info.st_mode)
            or stat.S_IMODE(set_info.st_mode) != 0o700
            or (set_info.st_dev, set_info.st_ino)
            != (visible_info.st_dev, visible_info.st_ino)
        ):
            raise BackupIntegrityError("backup set changed before ZIP materialization")

        manifest_bytes = _read_exact_member(
            set_fd,
            MANIFEST_FILENAME,
            maximum_size=_MAX_MANIFEST_BYTES,
        )
        if manifest_bytes != inspection.manifest.to_bytes():
            raise BackupIntegrityError(
                "backup envelope changed before ZIP materialization"
            )

        with zipfile.ZipFile(
            handle,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as archive:
            archive.writestr(_zip_info(MANIFEST_FILENAME), manifest_bytes)

            for resource in inspection.manifest.resources:
                source_fd = _open_relative_regular_file(set_fd, resource.path)
                try:
                    before = os.fstat(source_fd)
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(
                        _zip_info(resource.path),
                        mode="w",
                        force_zip64=True,
                    ) as destination:
                        while True:
                            chunk = os.read(source_fd, chunk_size)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > resource.size:
                                raise BackupIntegrityError(
                                    "backup resource grew during ZIP materialization"
                                )
                            digest.update(chunk)
                            destination.write(chunk)
                    after = os.fstat(source_fd)
                    if (
                        before.st_dev != after.st_dev
                        or before.st_ino != after.st_ino
                        or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                        or size != resource.size
                        or not secrets.compare_digest(
                            digest.hexdigest(),
                            resource.sha256,
                        )
                    ):
                        raise BackupIntegrityError(
                            "backup resource changed during ZIP materialization"
                        )
                finally:
                    os.close(source_fd)

        handle.flush()
        os.fsync(handle.fileno())
        size = handle.seek(0, os.SEEK_END)
        handle.seek(0)
        confirmed = store.inspect_set(
            backup_id,
            verify_resources=True,
        )
        if confirmed.manifest != inspection.manifest:
            raise BackupIntegrityError(
                "backup set changed after ZIP materialization"
            )
        result = PinnedBackupZip(
            filename=backup_transport_filename(inspection.manifest),
            size=size,
            _handle=handle,
        )
        handle = None
        return result
    except BackupError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise BackupTransportError(
            "backup_zip_creation_failed",
            "The backup could not be materialized as a ZIP.",
        ) from exc
    finally:
        if set_fd is not None:
            try:
                os.close(set_fd)
            except OSError:
                pass
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def _validate_archive_name(value: str) -> str:
    if not isinstance(value, str):
        raise BackupTransportError(
            "backup_zip_unsafe_path",
            "The ZIP contains an unsafe member path.",
        )
    if (
        not value
        or value != unicodedata.normalize("NFC", value)
        or "\\" in value
        or "\x00" in value
        or any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)
    ):
        raise BackupTransportError(
            "backup_zip_unsafe_path",
            "The ZIP contains an unsafe member path.",
        )
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not raw_parts
        or any(part in {"", ".", ".."} for part in raw_parts)
        or path.as_posix() != value
    ):
        raise BackupTransportError(
            "backup_zip_unsafe_path",
            "The ZIP contains an unsafe member path.",
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BackupTransportError(
            "backup_zip_unsafe_path",
            "The ZIP contains an unsafe member path.",
        ) from exc
    return value


def _validate_archive_infos(
    infos: list[zipfile.ZipInfo],
    limits: BackupTransportLimits,
) -> dict[str, zipfile.ZipInfo]:
    if not infos:
        raise BackupTransportError(
            "backup_zip_invalid",
            "The uploaded ZIP is empty.",
        )
    if len(infos) > limits.max_entries:
        raise BackupTransportError(
            "backup_zip_too_many_entries",
            "The uploaded ZIP contains too many entries.",
        )

    members: dict[str, zipfile.ZipInfo] = {}
    total_uncompressed = 0
    total_compressed = 0
    for info in infos:
        original = getattr(info, "orig_filename", info.filename)
        if "\x00" in original:
            raise BackupTransportError(
                "backup_zip_unsafe_path",
                "The ZIP contains a NUL byte in a member path.",
            )
        if info.is_dir() or info.filename.endswith("/"):
            raise BackupTransportError(
                "backup_zip_special_entry",
                "ZIP directory entries are not accepted.",
            )
        name = _validate_archive_name(info.filename)
        if name in members:
            raise BackupTransportError(
                "backup_zip_duplicate_entry",
                "The ZIP contains duplicate member names.",
            )
        if info.flag_bits & 0x1:
            raise BackupTransportError(
                "backup_zip_encrypted",
                "Encrypted ZIP members are not accepted.",
            )
        if info.compress_type not in _SUPPORTED_COMPRESSION:
            raise BackupTransportError(
                "backup_zip_compression_unsupported",
                "The ZIP uses an unsupported compression method.",
            )
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if file_type not in {0, stat.S_IFREG}:
            raise BackupTransportError(
                "backup_zip_special_entry",
                "The ZIP contains a symlink or special filesystem entry.",
            )
        if info.file_size < 0 or info.compress_size < 0:
            raise BackupTransportError(
                "backup_zip_invalid",
                "The ZIP contains invalid member sizes.",
            )
        if info.file_size > limits.max_resource_bytes and name.startswith("resources/"):
            raise BackupTransportError(
                "backup_zip_resource_too_large",
                "A backup resource exceeds the configured size limit.",
            )
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > limits.max_compression_ratio:
            raise BackupTransportError(
                "backup_zip_ratio_exceeded",
                "A ZIP member exceeds the configured compression ratio.",
            )
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
        if total_uncompressed > limits.max_uncompressed_bytes:
            raise BackupTransportError(
                "backup_zip_uncompressed_too_large",
                "The ZIP exceeds the configured uncompressed size limit.",
            )
        members[name] = info

    if total_uncompressed / max(total_compressed, 1) > limits.max_compression_ratio:
        raise BackupTransportError(
            "backup_zip_ratio_exceeded",
            "The ZIP exceeds the configured aggregate compression ratio.",
        )
    return members


def _read_archive_range(source: BinaryIO, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0:
        raise BackupTransportError(
            "backup_zip_invalid",
            "The uploaded ZIP has an invalid directory layout.",
        )
    source.seek(offset)
    payload = source.read(size)
    if len(payload) != size:
        raise BackupTransportError(
            "backup_zip_invalid",
            "The uploaded ZIP has a truncated directory record.",
        )
    return payload


def _prevalidate_zip_directory(
    source: BinaryIO,
    archive_size: int,
    limits: BackupTransportLimits,
) -> None:
    """Bound the central directory before ``zipfile`` allocates it."""
    maximum_tail = 65_535 + _EOCD_STRUCT.size
    tail_size = min(archive_size, maximum_tail)
    tail_offset = archive_size - tail_size
    tail = _read_archive_range(source, tail_offset, tail_size)

    parsed_eocd: tuple[int, tuple[object, ...]] | None = None
    search_end = len(tail)
    while search_end >= 0:
        index = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
        if index < 0:
            break
        if index + _EOCD_STRUCT.size <= len(tail):
            values = _EOCD_STRUCT.unpack_from(tail, index)
            comment_size = int(values[-1])
            if index + _EOCD_STRUCT.size + comment_size == len(tail):
                parsed_eocd = (tail_offset + index, values)
                break
        search_end = index
    if parsed_eocd is None:
        raise BackupTransportError(
            "backup_zip_invalid",
            "The uploaded file has no valid ZIP end record.",
        )

    eocd_offset, eocd = parsed_eocd
    (
        _signature,
        disk_number,
        directory_disk,
        entries_on_disk,
        entries_total,
        directory_size,
        directory_offset,
        _comment_size,
    ) = eocd
    if disk_number != 0 or directory_disk != 0:
        raise BackupTransportError(
            "backup_zip_invalid",
            "Multi-disk ZIP archives are not accepted.",
        )

    locator_offset = eocd_offset - _ZIP64_LOCATOR_STRUCT.size
    locator: tuple[object, ...] | None = None
    if locator_offset >= 0:
        candidate = _read_archive_range(
            source,
            locator_offset,
            _ZIP64_LOCATOR_STRUCT.size,
        )
        if candidate.startswith(_ZIP64_LOCATOR_SIGNATURE):
            locator = _ZIP64_LOCATOR_STRUCT.unpack(candidate)

    uses_zip64 = locator is not None or any(
        value == sentinel
        for value, sentinel in (
            (entries_on_disk, 0xFFFF),
            (entries_total, 0xFFFF),
            (directory_size, 0xFFFFFFFF),
            (directory_offset, 0xFFFFFFFF),
        )
    )
    if uses_zip64:
        if locator is None:
            raise BackupTransportError(
                "backup_zip_invalid",
                "The uploaded ZIP has no valid ZIP64 locator.",
            )
        _locator_signature, locator_disk, zip64_offset, total_disks = locator
        if locator_disk != 0 or total_disks != 1:
            raise BackupTransportError(
                "backup_zip_invalid",
                "Multi-disk ZIP64 archives are not accepted.",
            )
        fixed_record = _read_archive_range(
            source,
            int(zip64_offset),
            _ZIP64_EOCD_STRUCT.size,
        )
        zip64 = _ZIP64_EOCD_STRUCT.unpack(fixed_record)
        (
            zip64_signature,
            zip64_record_size,
            _create_version,
            _extract_version,
            zip64_disk,
            zip64_directory_disk,
            zip64_entries_on_disk,
            zip64_entries_total,
            zip64_directory_size,
            zip64_directory_offset,
        ) = zip64
        if zip64_signature != _ZIP64_EOCD_SIGNATURE:
            raise BackupTransportError(
                "backup_zip_invalid",
                "The uploaded ZIP64 end record is invalid.",
            )
        if (
            zip64_record_size < _ZIP64_EOCD_STRUCT.size - 12
            or int(zip64_offset) + 12 + int(zip64_record_size) != locator_offset
            or zip64_disk != 0
            or zip64_directory_disk != 0
            or zip64_entries_on_disk != zip64_entries_total
        ):
            raise BackupTransportError(
                "backup_zip_invalid",
                "The uploaded ZIP64 directory layout is invalid.",
            )
        for legacy, actual, sentinel in (
            (entries_on_disk, zip64_entries_on_disk, 0xFFFF),
            (entries_total, zip64_entries_total, 0xFFFF),
            (directory_size, zip64_directory_size, 0xFFFFFFFF),
            (directory_offset, zip64_directory_offset, 0xFFFFFFFF),
        ):
            if legacy not in {sentinel, actual}:
                raise BackupTransportError(
                    "backup_zip_invalid",
                    "The ZIP and ZIP64 directory records disagree.",
                )
        entries_on_disk = zip64_entries_on_disk
        entries_total = zip64_entries_total
        directory_size = zip64_directory_size
        directory_offset = zip64_directory_offset
        directory_end = int(zip64_offset)
    else:
        if entries_on_disk != entries_total:
            raise BackupTransportError(
                "backup_zip_invalid",
                "Multi-disk ZIP archives are not accepted.",
            )
        directory_end = eocd_offset

    entries_total = int(entries_total)
    directory_size = int(directory_size)
    directory_offset = int(directory_offset)
    if entries_total > limits.max_entries:
        raise BackupTransportError(
            "backup_zip_too_many_entries",
            "The uploaded ZIP contains too many entries.",
        )
    if directory_size > limits.max_central_directory_bytes:
        raise BackupTransportError(
            "backup_zip_central_directory_too_large",
            "The uploaded ZIP central directory exceeds its size limit.",
        )
    if directory_size < entries_total * zipfile.sizeCentralDir:
        raise BackupTransportError(
            "backup_zip_invalid",
            "The uploaded ZIP central directory is inconsistent.",
        )
    if directory_offset + directory_size != directory_end:
        raise BackupTransportError(
            "backup_zip_invalid",
            "The uploaded ZIP contains an unsupported directory layout.",
        )

    cursor = directory_offset
    actual_entries = 0
    source.seek(directory_offset)
    while cursor < directory_end:
        if actual_entries >= limits.max_entries:
            raise BackupTransportError(
                "backup_zip_too_many_entries",
                "The uploaded ZIP contains too many entries.",
            )
        header = source.read(_CENTRAL_DIRECTORY_STRUCT.size)
        if len(header) != _CENTRAL_DIRECTORY_STRUCT.size:
            raise BackupTransportError(
                "backup_zip_invalid",
                "The uploaded ZIP central directory is truncated.",
            )
        values = _CENTRAL_DIRECTORY_STRUCT.unpack(header)
        if values[0] != _CENTRAL_DIRECTORY_SIGNATURE:
            raise BackupTransportError(
                "backup_zip_invalid",
                "The uploaded ZIP central directory has an invalid entry.",
            )
        variable_size = int(values[12]) + int(values[13]) + int(values[14])
        next_cursor = cursor + _CENTRAL_DIRECTORY_STRUCT.size + variable_size
        if next_cursor > directory_end:
            raise BackupTransportError(
                "backup_zip_invalid",
                "The uploaded ZIP central directory entry exceeds its bounds.",
            )
        source.seek(variable_size, os.SEEK_CUR)
        cursor = next_cursor
        actual_entries += 1
    if cursor != directory_end or actual_entries != entries_total:
        raise BackupTransportError(
            "backup_zip_invalid",
            "The uploaded ZIP central directory count is inconsistent.",
        )
    source.seek(0)


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum_size: int,
    chunk_size: int,
) -> bytes:
    if info.file_size > maximum_size:
        raise BackupTransportError(
            "backup_zip_metadata_too_large",
            "A ZIP metadata member exceeds its size limit.",
        )
    payload = bytearray()
    try:
        with archive.open(info, mode="r") as source:
            while True:
                chunk = source.read(min(chunk_size, maximum_size + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > maximum_size:
                    raise BackupTransportError(
                        "backup_zip_metadata_too_large",
                        "A ZIP metadata member exceeds its size limit.",
                    )
    except BackupTransportError:
        raise
    except (
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        NotImplementedError,
        zlib.error,
    ) as exc:
        raise BackupTransportError(
            "backup_zip_invalid",
            "A ZIP metadata member could not be verified.",
        ) from exc
    if len(payload) != info.file_size:
        raise BackupTransportError(
            "backup_zip_invalid",
            "A ZIP metadata member has an inconsistent size.",
        )
    return bytes(payload)


def _verify_zip_resource(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    resource: BackupResource,
    *,
    chunk_size: int,
) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info, mode="r") as source:
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > resource.size:
                    raise BackupTransportError(
                        "backup_resource_size_mismatch",
                        "A ZIP resource exceeds its declared size.",
                    )
                digest.update(chunk)
    except BackupTransportError:
        raise
    except (
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        NotImplementedError,
        zlib.error,
    ) as exc:
        raise BackupTransportError(
            "backup_resource_invalid",
            "A ZIP resource could not be verified.",
        ) from exc
    if size != resource.size:
        raise BackupTransportError(
            "backup_resource_size_mismatch",
            "A ZIP resource size differs from backup.json.",
        )
    if not secrets.compare_digest(digest.hexdigest(), resource.sha256):
        raise BackupTransportError(
            "backup_resource_checksum_mismatch",
            "A ZIP resource checksum differs from backup.json.",
        )


def prepare_backup_zip_import(
    store: LocalBackupStore,
    source: BinaryIO,
    *,
    limits: BackupTransportLimits,
) -> PreparedBackupImport:
    """Validate and extract one ZIP into a hidden store-owned partial set."""
    builder: BackupSetBuilder | None = None
    try:
        source.seek(0, os.SEEK_END)
        archive_size = source.tell()
        source.seek(0)
        if archive_size <= 0:
            raise BackupTransportError(
                "backup_zip_invalid",
                "The uploaded ZIP is empty.",
            )
        if archive_size > limits.max_upload_bytes:
            raise BackupTransportError(
                "backup_upload_too_large",
                "The uploaded ZIP exceeds the configured size limit.",
            )

        _prevalidate_zip_directory(source, archive_size, limits)

        try:
            archive = zipfile.ZipFile(source, mode="r", allowZip64=True)
        except (OSError, zipfile.BadZipFile, NotImplementedError) as exc:
            raise BackupTransportError(
                "backup_zip_invalid",
                "The uploaded file is not a valid ZIP archive.",
            ) from exc

        with archive:
            infos = archive.infolist()
            raw_names = {
                str(getattr(info, "orig_filename", info.filename))
                for info in infos
            }
            if MANIFEST_FILENAME not in raw_names and any(
                name == "data.db" or name.endswith("/data.db")
                for name in raw_names
            ):
                raise BackupTransportError(
                    "pocketbase_backup_unsupported",
                    "This is a PocketBase SQLite backup, not a native PPBase backup.",
                )
            members = _validate_archive_infos(infos, limits)

            for required in _ENVELOPE_NAMES:
                if required not in members:
                    raise BackupTransportError(
                        "backup_zip_envelope_missing",
                        "The ZIP is missing backup.json.",
                    )
            manifest_bytes = _read_zip_member(
                archive,
                members[MANIFEST_FILENAME],
                maximum_size=_MAX_MANIFEST_BYTES,
                chunk_size=limits.chunk_size,
            )
            try:
                manifest = BackupManifest.from_bytes(manifest_bytes)
            except BackupManifestError as exc:
                code = (
                    "backup_format_unsupported"
                    if "unsupported backup format" in str(exc)
                    else "backup_manifest_invalid"
                )
                raise BackupTransportError(
                    code,
                    "The ZIP manifest is invalid or unsupported.",
                ) from exc
            expected_names = set(_ENVELOPE_NAMES)
            expected_names.update(resource.path for resource in manifest.resources)
            if set(members) != expected_names or SEALED_FILENAME in members:
                raise BackupTransportError(
                    "backup_zip_members_invalid",
                    "The ZIP members do not exactly match backup.json.",
                )
            resource_infos: dict[str, tuple[BackupResource, zipfile.ZipInfo]] = {}
            for resource in manifest.resources:
                info = members[resource.path]
                if info.file_size != resource.size:
                    raise BackupTransportError(
                        "backup_resource_size_mismatch",
                        "A ZIP resource size differs from backup.json.",
                    )
                resource_infos[resource.path] = (resource, info)

            builder = store.begin_set(
                manifest.backup_id,
                format_version=manifest.format_version,
            )
            for resource in manifest.resources:
                info = resource_infos[resource.path][1]
                try:
                    with archive.open(info, mode="r") as member:
                        builder.write_imported_resource(
                            resource,
                            member,
                            chunk_size=limits.chunk_size,
                        )
                except BackupError:
                    raise
                except (
                    OSError,
                    RuntimeError,
                    zipfile.BadZipFile,
                    NotImplementedError,
                    zlib.error,
                ) as exc:
                    raise BackupTransportError(
                        "backup_resource_invalid",
                        "A ZIP resource could not be verified.",
                    ) from exc
            prepared = builder.prepare()
            return PreparedBackupImport(
                builder=builder,
                prepared=prepared,
                manifest_bytes=manifest_bytes,
            )
    except BackupTransportError:
        if builder is not None:
            builder.abort()
        raise
    except BackupIntegrityError as exc:
        if builder is not None:
            builder.abort()
        raise BackupTransportError(
            "backup_integrity_failed",
            "The uploaded ZIP failed resource verification.",
        ) from exc
    except BackupError:
        if builder is not None:
            builder.abort()
        raise
    except (OSError, ValueError, zlib.error) as exc:
        if builder is not None:
            builder.abort()
        raise BackupTransportError(
            "backup_zip_invalid",
            "The uploaded ZIP could not be processed safely.",
        ) from exc
