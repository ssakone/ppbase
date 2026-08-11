"""PocketBase-style local storage for PPBase backup ZIP objects."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Iterator

from ppbase.backup.models import (
    BackupAlreadyExistsError,
    BackupNotFoundError,
    BackupStateError,
)
from ppbase.backup.transport import BackupTransportError, PinnedBackupZip


_ATTRS_SUFFIX = ".attrs"
_COPY_CHUNK_SIZE = 1024 * 1024
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def validate_backup_key(value: str, *, require_zip_suffix: bool = False) -> str:
    """Return a safe single filesystem key compatible with PocketBase listings."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("backup key must be a non-empty basename")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError("backup key must use NFC Unicode normalization")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("backup key must not contain path components")
    if value.endswith(_ATTRS_SUFFIX):
        raise ValueError("the .attrs suffix is reserved")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("backup key must not contain control characters")
    if require_zip_suffix and not value.casefold().endswith(".zip"):
        raise ValueError("backup key must end in .zip")
    if len(value.encode("utf-8")) > 255:
        raise ValueError("backup key is too long")
    return value


@dataclass(frozen=True, slots=True)
class BackupArchiveInfo:
    key: str
    size: int
    modified: str

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "size": self.size,
            "modified": self.modified,
        }


class BackupArchiveStore:
    """Descriptor-anchored ``pb_data/backups`` ZIP object store."""

    def __init__(self, data_dir: str | Path) -> None:
        self._closed = False
        self.data_dir = Path(data_dir).expanduser().resolve(strict=False)
        self.root = self.data_dir / "backups"
        self._data_fd: int | None = None
        self._root_fd: int | None = None
        self._open()

    def _open(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            data_info = self.data_dir.lstat()
            if not stat.S_ISDIR(data_info.st_mode) or self.data_dir.is_symlink():
                raise BackupStateError("PPBase data_dir is not a safe directory")
            data_fd = os.open(
                self.data_dir,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                created = False
                try:
                    os.mkdir(
                        "backups",
                        mode=stat.S_IMODE(data_info.st_mode),
                        dir_fd=data_fd,
                    )
                    created = True
                    os.fsync(data_fd)
                except FileExistsError:
                    pass
                root_info = os.stat("backups", dir_fd=data_fd, follow_symlinks=False)
                if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
                    raise BackupStateError("pb_data/backups is not a safe directory")
                root_fd = os.open(
                    "backups",
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=data_fd,
                )
                opened_root = os.fstat(root_fd)
                if created:
                    os.fchmod(root_fd, stat.S_IMODE(data_info.st_mode))
                    os.fsync(root_fd)
                    os.fsync(data_fd)
                    opened_root = os.fstat(root_fd)
                if (
                    opened_root.st_dev != root_info.st_dev
                    or opened_root.st_ino != root_info.st_ino
                    or opened_root.st_uid != data_info.st_uid
                ):
                    os.close(root_fd)
                    raise BackupStateError("pb_data/backups changed while opening")
            except BaseException:
                os.close(data_fd)
                raise
        except BackupStateError:
            raise
        except OSError as exc:
            raise BackupStateError("pb_data/backups could not be opened safely") from exc
        self._data_fd = data_fd
        self._root_fd = root_fd

    def _require_open(self) -> int:
        if self._closed or self._root_fd is None:
            raise BackupStateError("backup archive store is closed")
        return self._root_fd

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        root_fd = self._root_fd
        data_fd = self._data_fd
        self._root_fd = None
        self._data_fd = None
        if root_fd is not None:
            os.close(root_fd)
        if data_fd is not None:
            os.close(data_fd)

    def __enter__(self) -> "BackupArchiveStore":
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    @staticmethod
    def _modified(info: os.stat_result) -> str:
        return (
            datetime.fromtimestamp(info.st_mtime, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def _regular_info(self, key: str) -> os.stat_result:
        root_fd = self._require_open()
        selected = validate_backup_key(key)
        try:
            info = os.stat(selected, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise BackupNotFoundError(f"backup not found: {selected}") from exc
        except OSError as exc:
            raise BackupStateError("backup could not be inspected safely") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise BackupNotFoundError(f"backup not found: {selected}")
        return info

    def exists(self, key: str) -> bool:
        try:
            self._regular_info(key)
        except BackupNotFoundError:
            return False
        return True

    def list(self) -> list[BackupArchiveInfo]:
        root_fd = self._require_open()
        result: list[BackupArchiveInfo] = []
        try:
            with os.scandir(root_fd) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or entry.name.endswith(_ATTRS_SUFFIX):
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        key = validate_backup_key(entry.name)
                        info = entry.stat(follow_symlinks=False)
                    except (OSError, ValueError):
                        continue
                    result.append(
                        BackupArchiveInfo(
                            key=key,
                            size=info.st_size,
                            modified=self._modified(info),
                        )
                    )
        except OSError as exc:
            raise BackupStateError("backup archives could not be listed safely") from exc
        result.sort(key=lambda item: item.modified, reverse=True)
        return result

    @staticmethod
    def _attrs_payload(key: str, md5_digest: bytes) -> bytes:
        return (
            json.dumps(
                {
                    "user.cache_control": "",
                    "user.content_disposition": "",
                    "user.content_encoding": "",
                    "user.content_language": "",
                    "user.content_type": "application/zip",
                    "user.metadata": {"original-filename": key},
                    "md5": base64.b64encode(md5_digest).decode("ascii"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written

    def publish(
        self,
        source: BinaryIO,
        key: str,
        *,
        require_zip_suffix: bool,
        sniff_zip: bool,
        max_size: int | None = None,
    ) -> BackupArchiveInfo:
        """Atomically store one ZIP and its PocketBase-compatible sidecar."""
        root_fd = self._require_open()
        selected = validate_backup_key(key, require_zip_suffix=require_zip_suffix)
        if self.exists(selected):
            raise BackupAlreadyExistsError(f"backup already exists: {selected}")

        token = secrets.token_hex(12)
        temporary = f".upload-{token}.tmp"
        attrs_temporary = f".upload-{token}.attrs.tmp"
        attrs_name = selected + _ATTRS_SUFFIX
        descriptor: int | None = None
        attrs_descriptor: int | None = None
        published = False
        linked = False
        attrs_published = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o666,
                dir_fd=root_fd,
            )
            digest = hashlib.md5(usedforsecurity=False)
            prefix = bytearray()
            size = 0
            while True:
                chunk = source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                payload = bytes(chunk)
                if len(prefix) < 4:
                    prefix.extend(payload[: 4 - len(prefix)])
                digest.update(payload)
                size += len(payload)
                if max_size is not None and size > max_size:
                    raise BackupTransportError(
                        "backup_upload_too_large",
                        "The uploaded backup exceeds the configured size limit.",
                    )
                self._write_all(descriptor, payload)
            if size <= 0:
                raise BackupTransportError(
                    "backup_upload_empty",
                    "The uploaded backup ZIP is empty.",
                )
            if sniff_zip and not any(bytes(prefix).startswith(magic) for magic in _ZIP_MAGIC):
                raise BackupTransportError(
                    "backup_upload_invalid_mime",
                    "The uploaded file is not detected as application/zip.",
                )
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None

            attrs_descriptor = os.open(
                attrs_temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o666,
                dir_fd=root_fd,
            )
            self._write_all(attrs_descriptor, self._attrs_payload(selected, digest.digest()))
            os.fsync(attrs_descriptor)
            os.close(attrs_descriptor)
            attrs_descriptor = None
            try:
                os.link(
                    temporary,
                    selected,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise BackupAlreadyExistsError(
                    f"backup already exists: {selected}"
                ) from exc
            linked = True
            os.unlink(temporary, dir_fd=root_fd)
            try:
                os.link(
                    attrs_temporary,
                    attrs_name,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise BackupAlreadyExistsError(
                    f"backup metadata already exists: {selected}"
                ) from exc
            attrs_published = True
            os.unlink(attrs_temporary, dir_fd=root_fd)
            os.fsync(root_fd)
            published = True
            info = self._regular_info(selected)
            return BackupArchiveInfo(
                key=selected,
                size=info.st_size,
                modified=self._modified(info),
            )
        except (BackupAlreadyExistsError, BackupStateError, BackupTransportError):
            raise
        except OSError as exc:
            raise BackupStateError("backup ZIP could not be stored safely") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if attrs_descriptor is not None:
                os.close(attrs_descriptor)
            for name in (temporary, attrs_temporary):
                try:
                    os.unlink(name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            if not published and linked:
                try:
                    os.unlink(selected, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            if not published and attrs_published:
                try:
                    os.unlink(attrs_name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def publish_pinned(
        self,
        pinned: PinnedBackupZip,
        key: str,
        *,
        require_zip_suffix: bool = True,
    ) -> BackupArchiveInfo:
        """Publish a generated ZIP and close its anonymous source afterward."""
        try:
            pinned._handle.seek(0)
            return self.publish(
                pinned._handle,
                key,
                require_zip_suffix=require_zip_suffix,
                sniff_zip=True,
            )
        finally:
            pinned.close()

    def pin(self, key: str) -> PinnedBackupZip:
        root_fd = self._require_open()
        selected = validate_backup_key(key)
        expected = self._regular_info(selected)
        descriptor: int | None = None
        handle: BinaryIO | None = None
        try:
            descriptor = os.open(
                selected,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != expected.st_dev
                or opened.st_ino != expected.st_ino
            ):
                raise BackupStateError("backup changed while opening")
            handle = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            result = PinnedBackupZip(
                filename=selected,
                size=opened.st_size,
                _handle=handle,
            )
            handle = None
            return result
        except (BackupNotFoundError, BackupStateError):
            raise
        except OSError as exc:
            raise BackupStateError("backup could not be opened safely") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if handle is not None:
                handle.close()

    def open(self, key: str) -> BinaryIO:
        """Open one pinned ZIP stream for validation or restore."""
        pinned = self.pin(key)
        handle = pinned._handle
        pinned._handle = open(os.devnull, "rb")
        pinned.close()
        return handle

    def delete(self, key: str) -> None:
        root_fd = self._require_open()
        selected = validate_backup_key(key)
        self._regular_info(selected)
        try:
            os.unlink(selected, dir_fd=root_fd)
            try:
                os.unlink(selected + _ATTRS_SUFFIX, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            os.fsync(root_fd)
        except FileNotFoundError as exc:
            raise BackupNotFoundError(f"backup not found: {selected}") from exc
        except OSError as exc:
            raise BackupStateError("backup could not be deleted safely") from exc

    def iter_keys(self) -> Iterator[str]:
        for item in self.list():
            yield item.key


__all__ = [
    "BackupArchiveInfo",
    "BackupArchiveStore",
    "validate_backup_key",
]
