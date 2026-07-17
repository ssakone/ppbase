"""Ed25519 identity and detached-signature primitives for backup manifests."""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ppbase.backup.models import BackupError, BackupIntegrityError


MANIFEST_SIGNATURE_DOMAIN = b"PPBASE-BACKUP-MANIFEST-V1\0"
PRIVATE_KEY_FILENAME = "signing-ed25519.key"
_PRIVATE_KEY_LOCK_FILENAME = ".signing-ed25519.lock"
_PRIVATE_KEY_TEMP_PREFIX = f".{PRIVATE_KEY_FILENAME}."
ED25519_PRIVATE_KEY_SIZE = 32
ED25519_PUBLIC_KEY_SIZE = 32
ED25519_SIGNATURE_SIZE = 64


class BackupIdentityError(BackupError):
    """Raised when the local signing identity is missing or unsafe."""


def _open_flags(base: int) -> int:
    flags = base | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, _open_flags(flags))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_control_directory(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while True:
        try:
            candidate.lstat()
        except FileNotFoundError:
            if candidate == candidate.parent:
                raise BackupIdentityError(
                    f"cannot create backup control directory: {path}"
                )
            missing.append(candidate)
            candidate = candidate.parent
            continue
        break
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            pass
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
            raise BackupIdentityError(
                f"backup control path is not a directory: {directory}"
            )
        os.chmod(directory, 0o700, follow_symlinks=False)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)

    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise BackupIdentityError(f"backup control path is not a directory: {path}")
    os.chmod(path, 0o700, follow_symlinks=False)
    if stat.S_IMODE(path.lstat().st_mode) != 0o700:
        raise BackupIdentityError(f"backup control directory must have mode 0700: {path}")
    _fsync_directory(path)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise BackupIdentityError("short write while creating signing identity")
        view = view[written:]


def _cleanup_private_key_temporaries(control_dir: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_descriptor = os.open(control_dir, _open_flags(flags))
    except OSError as exc:
        raise BackupIdentityError(
            "cannot inspect backup identity temporaries"
        ) from exc
    try:
        removed = False
        for name in os.listdir(directory_descriptor):
            if not (
                name.startswith(_PRIVATE_KEY_TEMP_PREFIX)
                and name.endswith(".tmp")
            ):
                continue
            try:
                info = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise BackupIdentityError(
                    "backup identity temporary is not a regular file"
                )
            try:
                os.unlink(name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                continue
            removed = True
        if removed:
            os.fsync(directory_descriptor)
    except OSError as exc:
        raise BackupIdentityError(
            "cannot clean backup identity temporaries"
        ) from exc
    finally:
        os.close(directory_descriptor)


def _open_identity_lock(control_dir: Path) -> int:
    lock_path = control_dir / _PRIVATE_KEY_LOCK_FILENAME
    try:
        descriptor = os.open(
            lock_path,
            _open_flags(os.O_RDWR | os.O_CREAT),
            0o600,
        )
    except OSError as exc:
        raise BackupIdentityError("cannot open backup identity lock") from exc
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BackupIdentityError("backup identity lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _fsync_directory(control_dir)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_private_key_file(path: Path, payload: bytes) -> bool:
    temporary_name = f"{_PRIVATE_KEY_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_descriptor = os.open(path.parent, _open_flags(directory_flags))
    except OSError as exc:
        raise BackupIdentityError(
            "cannot open backup signing-key directory"
        ) from exc
    flags = _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    descriptor = -1
    temporary_exists = False
    try:
        try:
            descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            temporary_exists = True
        except OSError as exc:
            raise BackupIdentityError(
                "cannot allocate backup signing-key temporary"
            ) from exc
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        except OSError as exc:
            raise BackupIdentityError(
                "cannot publish backup signing key"
            ) from exc
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_exists = False
        os.fsync(directory_descriptor)
        return True
    finally:
        try:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_exists:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                    os.fsync(directory_descriptor)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory_descriptor)


def _read_private_key_file(path: Path) -> bytes:
    try:
        descriptor = os.open(path, _open_flags(os.O_RDONLY))
    except (FileNotFoundError, OSError) as exc:
        raise BackupIdentityError(f"cannot open backup signing key: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupIdentityError("backup signing key must be one regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise BackupIdentityError("backup signing key must have mode 0600")
        chunks: list[bytes] = []
        remaining = ED25519_PRIVATE_KEY_SIZE + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(payload) != ED25519_PRIVATE_KEY_SIZE:
        raise BackupIdentityError("backup signing key has an invalid length")
    return payload


def public_key_fingerprint(public_key: bytes) -> str:
    """Return SHA-256(raw Ed25519 public key) as lowercase hexadecimal."""
    if not isinstance(public_key, bytes) or len(public_key) != ED25519_PUBLIC_KEY_SIZE:
        raise BackupIdentityError("Ed25519 public key must contain exactly 32 bytes")
    return hashlib.sha256(public_key).hexdigest()


@dataclass(frozen=True, slots=True)
class BackupIdentity:
    """Persistent control-plane identity used to sign local backup sets."""

    control_dir: Path
    _private_key: Ed25519PrivateKey = field(repr=False)

    @classmethod
    def load_or_create(cls, control_dir: str | Path) -> "BackupIdentity":
        path = Path(control_dir)
        _ensure_control_directory(path)
        key_path = path / PRIVATE_KEY_FILENAME
        lock_descriptor = _open_identity_lock(path)
        try:
            _cleanup_private_key_temporaries(path)
            try:
                key_path.lstat()
            except FileNotFoundError:
                private_key = Ed25519PrivateKey.generate()
                private_bytes = private_key.private_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PrivateFormat.Raw,
                    encryption_algorithm=serialization.NoEncryption(),
                )
                _create_private_key_file(key_path, private_bytes)
            stored_bytes = _read_private_key_file(key_path)
        finally:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
        try:
            stored_key = Ed25519PrivateKey.from_private_bytes(stored_bytes)
        except ValueError as exc:
            raise BackupIdentityError("backup signing key is not valid Ed25519") from exc
        return cls(control_dir=path, _private_key=stored_key)

    @property
    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def fingerprint_sha256(self) -> str:
        return public_key_fingerprint(self.public_key_bytes)

    def sign_manifest(self, canonical_manifest: bytes) -> bytes:
        """Create the raw detached signature for canonical manifest bytes."""
        if not isinstance(canonical_manifest, bytes):
            raise BackupIdentityError("canonical manifest must be bytes")
        return self._private_key.sign(
            MANIFEST_SIGNATURE_DOMAIN + canonical_manifest
        )


def verify_manifest_signature(
    canonical_manifest: bytes,
    signature: bytes,
    public_key: bytes,
) -> str:
    """Verify a detached signature and return the signer's raw-key fingerprint."""
    if not isinstance(canonical_manifest, bytes):
        raise BackupIntegrityError("canonical manifest must be bytes")
    if not isinstance(signature, bytes) or len(signature) != ED25519_SIGNATURE_SIZE:
        raise BackupIntegrityError("Ed25519 manifest signature must contain 64 bytes")
    if not isinstance(public_key, bytes) or len(public_key) != ED25519_PUBLIC_KEY_SIZE:
        raise BackupIntegrityError("Ed25519 signer public key must contain 32 bytes")
    try:
        verifier = Ed25519PublicKey.from_public_bytes(public_key)
        verifier.verify(signature, MANIFEST_SIGNATURE_DOMAIN + canonical_manifest)
    except (InvalidSignature, ValueError) as exc:
        raise BackupIntegrityError("backup manifest signature is invalid") from exc
    return public_key_fingerprint(public_key)
