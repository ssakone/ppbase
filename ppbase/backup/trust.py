"""Descriptor-anchored approval store for external Ed25519 backup signers."""

from __future__ import annotations

import base64
import binascii
import fcntl
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterator

from ppbase.backup.control import (
    ControlPlaneRoot,
    ControlPlaneSafetyError,
    fsync_directory,
    open_flags,
    same_file_identity,
    verify_directory_attached_at,
)
from ppbase.backup.identity import ED25519_PUBLIC_KEY_SIZE, public_key_fingerprint
from ppbase.backup.models import canonical_json_bytes, parse_canonical_json


_TRUST_DIRECTORY = "trust"
_LOCK_FILENAME = "trust.lock"
_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")
_RECORD_SUFFIX = ".json"
_MAX_RECORD_BYTES = 16 * 1024


class BackupTrustError(RuntimeError):
    """Raised when signer approval state is malformed, detached, or unsafe."""


@dataclass(frozen=True, slots=True)
class TrustedBackupSigner:
    fingerprint_sha256: str
    public_key: bytes
    approved_at: str
    actor_id: str | None

    def to_dict(self) -> dict[str, object]:
        encoded_key = (
            base64.urlsafe_b64encode(self.public_key).decode("ascii").rstrip("=")
        )
        return {
            "algorithm": "Ed25519",
            "fingerprintSha256": self.fingerprint_sha256,
            "publicKey": encoded_key,
            "approvedAt": self.approved_at,
            "actorId": self.actor_id,
            "trustStatus": "trusted_external",
        }


def _record_name(fingerprint_sha256: str) -> str:
    if not isinstance(fingerprint_sha256, str) or not _FINGERPRINT_RE.fullmatch(
        fingerprint_sha256
    ):
        raise BackupTrustError("The signer fingerprint SHA-256 is invalid.")
    return f"{fingerprint_sha256}{_RECORD_SUFFIX}"


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise BackupTrustError("The signer approval could not be written safely.")
        view = view[written:]


def _read_all(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            raise BackupTrustError("The signer approval is truncated.")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise BackupTrustError("The signer approval changed while it was read.")
    return b"".join(chunks)


class BackupTrustStore:
    """Persist exact external signer keys below one pinned control root."""

    def __init__(self, control_root: ControlPlaneRoot) -> None:
        if not isinstance(control_root, ControlPlaneRoot):
            raise BackupTrustError("A descriptor-anchored control root is required.")
        self._control_root = control_root
        self._directory_fd = -1
        self._lock_fd = -1
        self._closed = False
        try:
            self._directory_fd = control_root.open_private_directory(
                _TRUST_DIRECTORY,
                label="The backup signer trust directory",
                create_missing=True,
            )
            self._lock_fd = self._open_lock_file()
            self.verify_attached()
        except BaseException:
            self.close()
            raise

    def _require_open(self) -> None:
        if self._closed or self._directory_fd < 0 or self._lock_fd < 0:
            raise BackupTrustError("The backup signer trust store is closed.")

    def _open_lock_file(self) -> int:
        descriptor: int | None = None
        created = False
        flags = open_flags(os.O_RDWR)
        try:
            try:
                descriptor = os.open(
                    _LOCK_FILENAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    mode=0o600,
                    dir_fd=self._directory_fd,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(
                    _LOCK_FILENAME,
                    flags,
                    dir_fd=self._directory_fd,
                )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise BackupTrustError("The backup signer trust lock is unsafe.")
            if created:
                os.fsync(descriptor)
                fsync_directory(self._directory_fd)
            result = descriptor
            descriptor = None
            return result
        except BackupTrustError:
            raise
        except OSError as exc:
            raise BackupTrustError(
                "The backup signer trust lock cannot be opened safely."
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def verify_attached(self) -> None:
        self._require_open()
        try:
            self._control_root.verify_attached()
            verify_directory_attached_at(
                self._control_root.fileno(),
                _TRUST_DIRECTORY,
                self._directory_fd,
                label="The backup signer trust directory",
            )
            visible_lock = os.stat(
                _LOCK_FILENAME,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            opened_lock = os.fstat(self._lock_fd)
            if (
                stat.S_ISLNK(visible_lock.st_mode)
                or not stat.S_ISREG(visible_lock.st_mode)
                or not stat.S_ISREG(opened_lock.st_mode)
                or not same_file_identity(visible_lock, opened_lock)
                or opened_lock.st_uid != os.geteuid()
                or opened_lock.st_nlink != 1
                or stat.S_IMODE(opened_lock.st_mode) != 0o600
            ):
                raise BackupTrustError("The backup signer trust lock was substituted.")
            self._control_root.verify_attached()
        except BackupTrustError:
            raise
        except (ControlPlaneSafetyError, OSError) as exc:
            raise BackupTrustError(
                "The backup signer trust store is detached or unsafe."
            ) from exc

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.verify_attached()
        try:
            fcntl.flock(
                self._lock_fd,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
        except OSError as exc:
            raise BackupTrustError(
                "The backup signer trust store cannot be locked safely."
            ) from exc
        try:
            self.verify_attached()
            yield
            self.verify_attached()
        finally:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def _read_record_unlocked(self, fingerprint_sha256: str) -> TrustedBackupSigner:
        name = _record_name(fingerprint_sha256)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                open_flags(os.O_RDONLY),
                dir_fd=self._directory_fd,
            )
            visible = os.stat(
                name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            opened = os.fstat(descriptor)
            if (
                stat.S_ISLNK(visible.st_mode)
                or not stat.S_ISREG(visible.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or not same_file_identity(visible, opened)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size <= 0
                or opened.st_size > _MAX_RECORD_BYTES
            ):
                raise BackupTrustError("A backup signer approval is unsafe.")
            payload = _read_all(descriptor, opened.st_size)
            visible_after = os.stat(
                name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            if not same_file_identity(opened, visible_after):
                raise BackupTrustError(
                    "A backup signer approval changed while it was read."
                )
        except FileNotFoundError:
            raise
        except BackupTrustError:
            raise
        except OSError as exc:
            raise BackupTrustError(
                "A backup signer approval cannot be read safely."
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

        try:
            value = parse_canonical_json(payload)
            if not isinstance(value, dict) or set(value) != {
                "actorId",
                "algorithm",
                "approvedAt",
                "fingerprintSha256",
                "formatVersion",
                "publicKey",
            }:
                raise BackupTrustError("A backup signer approval has invalid fields.")
            if value.get("formatVersion") != 1 or value.get("algorithm") != "Ed25519":
                raise BackupTrustError("A backup signer approval has an invalid format.")
            encoded_key = value.get("publicKey")
            if not isinstance(encoded_key, str):
                raise BackupTrustError("A backup signer approval has an invalid key.")
            padding = "=" * (-len(encoded_key) % 4)
            public_key = base64.b64decode(
                encoded_key + padding,
                altchars=b"-_",
                validate=True,
            )
            if len(public_key) != ED25519_PUBLIC_KEY_SIZE:
                raise BackupTrustError("A backup signer approval has an invalid key.")
            declared_fingerprint = value.get("fingerprintSha256")
            if (
                declared_fingerprint != fingerprint_sha256
                or public_key_fingerprint(public_key) != fingerprint_sha256
            ):
                raise BackupTrustError(
                    "A backup signer approval does not match its exact key."
                )
            approved_at = value.get("approvedAt")
            actor_id = value.get("actorId")
            if (
                not isinstance(approved_at, str)
                or not approved_at.endswith("Z")
                or (actor_id is not None and not isinstance(actor_id, str))
            ):
                raise BackupTrustError("A backup signer approval has invalid metadata.")
            return TrustedBackupSigner(
                fingerprint_sha256=fingerprint_sha256,
                public_key=public_key,
                approved_at=approved_at,
                actor_id=actor_id,
            )
        except BackupTrustError:
            raise
        except (ValueError, TypeError, binascii.Error) as exc:
            raise BackupTrustError("A backup signer approval is invalid.") from exc

    def list(self) -> list[TrustedBackupSigner]:
        with self._locked(exclusive=False):
            try:
                names = sorted(
                    entry.name
                    for entry in os.scandir(self._directory_fd)
                    if entry.name.endswith(_RECORD_SUFFIX)
                    and not entry.name.startswith(".")
                )
            except OSError as exc:
                raise BackupTrustError(
                    "Backup signer approvals cannot be enumerated safely."
                ) from exc
            records: list[TrustedBackupSigner] = []
            for name in names:
                fingerprint = name[: -len(_RECORD_SUFFIX)]
                _record_name(fingerprint)
                try:
                    records.append(self._read_record_unlocked(fingerprint))
                except FileNotFoundError as exc:
                    raise BackupTrustError(
                        "A backup signer approval disappeared during enumeration."
                    ) from exc
            return records

    def approved_public_key(self, fingerprint_sha256: str) -> bytes | None:
        _record_name(fingerprint_sha256)
        with self._locked(exclusive=False):
            try:
                return self._read_record_unlocked(fingerprint_sha256).public_key
            except FileNotFoundError:
                return None

    def approve(
        self,
        public_key: bytes,
        *,
        actor_id: str | None,
    ) -> TrustedBackupSigner:
        if (
            not isinstance(public_key, bytes)
            or len(public_key) != ED25519_PUBLIC_KEY_SIZE
        ):
            raise BackupTrustError("An Ed25519 public key must contain exactly 32 bytes.")
        if actor_id is not None and not isinstance(actor_id, str):
            raise BackupTrustError("The signer approval actor is invalid.")
        fingerprint = public_key_fingerprint(public_key)
        name = _record_name(fingerprint)
        record = TrustedBackupSigner(
            fingerprint_sha256=fingerprint,
            public_key=public_key,
            approved_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            actor_id=actor_id,
        )
        encoded_key = (
            base64.urlsafe_b64encode(public_key).decode("ascii").rstrip("=")
        )
        payload = canonical_json_bytes(
            {
                "formatVersion": 1,
                "algorithm": "Ed25519",
                "fingerprintSha256": fingerprint,
                "publicKey": encoded_key,
                "approvedAt": record.approved_at,
                "actorId": actor_id,
            }
        )
        if len(payload) > _MAX_RECORD_BYTES:  # pragma: no cover - fixed schema
            raise BackupTrustError("The signer approval exceeds its size limit.")

        with self._locked(exclusive=True):
            try:
                existing = self._read_record_unlocked(fingerprint)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if not secrets.compare_digest(existing.public_key, public_key):
                    raise BackupTrustError(
                        "The signer fingerprint is already bound to another key."
                    )
                return existing

            temporary_name = f".approve-{secrets.token_hex(16)}.tmp"
            temporary_fd: int | None = None
            published = False
            try:
                temporary_fd = os.open(
                    temporary_name,
                    open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                    mode=0o600,
                    dir_fd=self._directory_fd,
                )
                _write_all(temporary_fd, payload)
                os.fsync(temporary_fd)
                temporary_info = os.fstat(temporary_fd)
                if (
                    not stat.S_ISREG(temporary_info.st_mode)
                    or temporary_info.st_uid != os.geteuid()
                    or temporary_info.st_nlink != 1
                    or stat.S_IMODE(temporary_info.st_mode) != 0o600
                ):
                    raise BackupTrustError(
                        "The temporary signer approval became unsafe."
                    )
                self.verify_attached()
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
                published = True
                fsync_directory(self._directory_fd)
                os.unlink(temporary_name, dir_fd=self._directory_fd)
                fsync_directory(self._directory_fd)
                self.verify_attached()
                return self._read_record_unlocked(fingerprint)
            except FileExistsError as exc:
                raise BackupTrustError(
                    "The signer approval was published concurrently."
                ) from exc
            except BackupTrustError:
                raise
            except OSError as exc:
                raise BackupTrustError(
                    "The signer approval could not be published safely."
                ) from exc
            finally:
                if temporary_fd is not None:
                    os.close(temporary_fd)
                try:
                    os.unlink(temporary_name, dir_fd=self._directory_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    if not published:
                        raise BackupTrustError(
                            "A rejected signer approval could not be cleaned safely."
                        )

    def revoke(self, fingerprint_sha256: str) -> bool:
        name = _record_name(fingerprint_sha256)
        with self._locked(exclusive=True):
            try:
                self._read_record_unlocked(fingerprint_sha256)
            except FileNotFoundError:
                return False
            try:
                os.unlink(name, dir_fd=self._directory_fd)
                fsync_directory(self._directory_fd)
                self.verify_attached()
            except OSError as exc:
                raise BackupTrustError(
                    "The signer approval could not be revoked durably."
                ) from exc
            return True

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        descriptors = {self._lock_fd, self._directory_fd}
        self._lock_fd = -1
        self._directory_fd = -1
        for descriptor in descriptors:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def __enter__(self) -> "BackupTrustStore":
        self.verify_attached()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()
