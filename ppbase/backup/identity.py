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

from ppbase.backup.control import (
    ControlPlaneRoot,
    ControlPlaneSafetyError,
    open_flags as _control_open_flags,
    same_file_identity,
    verify_directory_attached_at,
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


class BackupIdentityMissingError(BackupIdentityError):
    """Raised when a pre-existing identity is required but absent."""


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise BackupIdentityError("short write while creating signing identity")
        view = view[written:]


def _cleanup_private_key_temporaries_at(directory_fd: int) -> None:
    try:
        removed = False
        for name in os.listdir(directory_fd):
            if not (
                name.startswith(_PRIVATE_KEY_TEMP_PREFIX)
                and name.endswith(".tmp")
            ):
                continue
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise BackupIdentityError(
                    "backup identity temporary is not a private regular file"
                )
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            removed = True
        if removed:
            os.fsync(directory_fd)
    except BackupIdentityError:
        raise
    except OSError as exc:
        raise BackupIdentityError(
            "cannot clean backup identity temporaries"
        ) from exc


def _open_identity_lock_at(directory_fd: int) -> int:
    try:
        descriptor = os.open(
            _PRIVATE_KEY_LOCK_FILENAME,
            _control_open_flags(os.O_RDWR | os.O_CREAT),
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise BackupIdentityError("cannot open backup identity lock") from exc
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BackupIdentityError("backup identity lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.fsync(directory_fd)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_private_key_file_at(directory_fd: int, payload: bytes) -> bool:
    temporary_name = f"{_PRIVATE_KEY_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
    descriptor = -1
    temporary_exists = False
    try:
        try:
            descriptor = os.open(
                temporary_name,
                _control_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                0o600,
                dir_fd=directory_fd,
            )
            temporary_exists = True
        except OSError as exc:
            raise BackupIdentityError(
                "cannot allocate backup signing-key temporary"
            ) from exc
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BackupIdentityError("backup signing-key temporary is unsafe")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary_name,
                PRIVATE_KEY_FILENAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        except OSError as exc:
            raise BackupIdentityError(
                "cannot publish backup signing key"
            ) from exc
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_exists = False
        os.fsync(directory_fd)
        return True
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                pass


def _validate_private_key_descriptor(descriptor: int) -> os.stat_result:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        raise BackupIdentityError(
            "backup signing key must be one private regular file"
        )
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise BackupIdentityError("backup signing key must have mode 0600")
    return info


def _read_private_key_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = ED25519_PRIVATE_KEY_SIZE + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) != ED25519_PRIVATE_KEY_SIZE:
        raise BackupIdentityError("backup signing key has an invalid length")
    return payload


def _read_attached_private_key_descriptor(
    directory_fd: int,
    descriptor: int,
) -> bytes:
    before = _validate_private_key_descriptor(descriptor)
    payload = _read_private_key_descriptor(descriptor)
    after = _validate_private_key_descriptor(descriptor)
    try:
        visible = os.stat(
            PRIVATE_KEY_FILENAME,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise BackupIdentityError("backup signing key was detached") from exc
    if (
        not same_file_identity(before, after)
        or not same_file_identity(after, visible)
    ):
        raise BackupIdentityError(
            "backup signing key was detached or substituted"
        )
    return payload


def _open_private_key_file_at(directory_fd: int) -> tuple[int, bytes]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            PRIVATE_KEY_FILENAME,
            _control_open_flags(os.O_RDONLY),
            dir_fd=directory_fd,
        )
    except (FileNotFoundError, OSError) as exc:
        raise BackupIdentityError("cannot open backup signing key") from exc
    try:
        payload = _read_attached_private_key_descriptor(directory_fd, descriptor)
        result = (descriptor, payload)
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_private_key_file_at(directory_fd: int) -> bytes:
    descriptor, payload = _open_private_key_file_at(directory_fd)
    os.close(descriptor)
    return payload


def _load_private_key_at(
    directory_fd: int,
    *,
    create_missing: bool,
) -> Ed25519PrivateKey:
    lock_descriptor = _open_identity_lock_at(directory_fd)
    try:
        _cleanup_private_key_temporaries_at(directory_fd)
        try:
            os.stat(
                PRIVATE_KEY_FILENAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not create_missing:
                raise BackupIdentityMissingError(
                    "backup signing identity is missing"
                )
            private_key = Ed25519PrivateKey.generate()
            private_bytes = private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
            _create_private_key_file_at(directory_fd, private_bytes)
        stored_bytes = _read_private_key_file_at(directory_fd)
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)
    try:
        return Ed25519PrivateKey.from_private_bytes(stored_bytes)
    except ValueError as exc:
        raise BackupIdentityError("backup signing key is not valid Ed25519") from exc


def _load_or_create_private_key_at(directory_fd: int) -> Ed25519PrivateKey:
    return _load_private_key_at(directory_fd, create_missing=True)


def _load_existing_private_key_at(directory_fd: int) -> Ed25519PrivateKey:
    return _load_private_key_at(directory_fd, create_missing=False)


def public_key_fingerprint(public_key: bytes) -> str:
    """Return SHA-256(raw Ed25519 public key) as lowercase hexadecimal."""
    if not isinstance(public_key, bytes) or len(public_key) != ED25519_PUBLIC_KEY_SIZE:
        raise BackupIdentityError("Ed25519 public key must contain exactly 32 bytes")
    return hashlib.sha256(public_key).hexdigest()


def _verify_identity_directory_attachment(
    control_root: ControlPlaneRoot,
    directory_name: str | None,
    directory_fd: int,
) -> None:
    control_root.verify_attached()
    if directory_name is None:
        try:
            root_info = os.fstat(control_root.fileno())
            opened = os.fstat(directory_fd)
        except OSError as exc:
            raise BackupIdentityError(
                "backup identity control directory was detached"
            ) from exc
        if not same_file_identity(root_info, opened):
            raise BackupIdentityError(
                "backup identity control directory was detached or substituted"
            )
    else:
        verify_directory_attached_at(
            control_root.fileno(),
            directory_name,
            directory_fd,
            label="The backup identity directory",
        )
    control_root.verify_attached()


@dataclass(slots=True)
class BackupIdentity:
    """Persistent control-plane identity used to sign local backup sets."""

    control_dir: Path
    _private_key: Ed25519PrivateKey = field(repr=False)
    _control_root: ControlPlaneRoot | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _directory_name: str | None = field(default=None, repr=False, compare=False)
    _directory_fd: int = field(default=-1, repr=False, compare=False)
    _key_fd: int = field(default=-1, repr=False, compare=False)
    _owns_control_root: bool = field(default=False, repr=False, compare=False)
    _closed: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def private_key_exists_at(
        cls,
        control_root: ControlPlaneRoot,
        directory_name: str = "identity",
    ) -> bool:
        directory_fd: int | None = None
        try:
            control_root.verify_attached()
            try:
                os.stat(
                    directory_name,
                    dir_fd=control_root.fileno(),
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            directory_fd = control_root.open_private_directory(
                directory_name,
                label="The backup identity directory",
                create_missing=False,
            )
            try:
                os.stat(
                    PRIVATE_KEY_FILENAME,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                exists = False
            else:
                exists = True
            verify_directory_attached_at(
                control_root.fileno(),
                directory_name,
                directory_fd,
                label="The backup identity directory",
            )
            control_root.verify_attached()
            return exists
        except ControlPlaneSafetyError as exc:
            raise BackupIdentityError(str(exc)) from exc
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    @classmethod
    def load_or_create_at(
        cls,
        control_root: ControlPlaneRoot,
        directory_name: str = "identity",
    ) -> "BackupIdentity":
        return cls._load_anchored(
            control_root,
            directory_name,
            create_directory=True,
            create_key=True,
        )

    @classmethod
    def load_existing_at(
        cls,
        control_root: ControlPlaneRoot,
        directory_name: str = "identity",
    ) -> "BackupIdentity":
        """Load and pin an existing identity without ever creating a key."""
        try:
            os.stat(
                directory_name,
                dir_fd=control_root.fileno(),
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise BackupIdentityMissingError(
                "backup signing identity is missing"
            ) from exc
        except OSError as exc:
            raise BackupIdentityError(
                "backup signing identity cannot be inspected safely"
            ) from exc
        return cls._load_anchored(
            control_root,
            directory_name,
            create_directory=False,
            create_key=False,
        )

    @classmethod
    def load_or_create_in_root(
        cls,
        control_root: ControlPlaneRoot,
    ) -> "BackupIdentity":
        """Load an identity stored directly in an owned control root."""
        return cls._load_anchored(
            control_root,
            None,
            create_directory=False,
            create_key=True,
        )

    @classmethod
    def _load_anchored(
        cls,
        control_root: ControlPlaneRoot,
        directory_name: str | None,
        *,
        create_directory: bool,
        create_key: bool,
    ) -> "BackupIdentity":
        directory_fd: int | None = None
        key_fd: int | None = None
        try:
            control_root.verify_attached()
            if directory_name is None:
                directory_fd = os.dup(control_root.fileno())
            else:
                directory_fd = control_root.open_private_directory(
                    directory_name,
                    label="The backup identity directory",
                    create_missing=create_directory,
                )
            _verify_identity_directory_attachment(
                control_root,
                directory_name,
                directory_fd,
            )
            stored_key = (
                _load_or_create_private_key_at(directory_fd)
                if create_key
                else _load_existing_private_key_at(directory_fd)
            )
            key_fd, pinned_bytes = _open_private_key_file_at(directory_fd)
            pinned_key = Ed25519PrivateKey.from_private_bytes(pinned_bytes)
            pinned_public_key = pinned_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            stored_public_key = stored_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            if not secrets.compare_digest(pinned_public_key, stored_public_key):
                raise BackupIdentityError(
                    "backup signing identity changed while it was loaded"
                )
            _verify_identity_directory_attachment(
                control_root,
                directory_name,
                directory_fd,
            )
            result = cls(
                control_dir=(
                    control_root.path
                    if directory_name is None
                    else control_root.path / directory_name
                ),
                _private_key=stored_key,
                _control_root=control_root,
                _directory_name=directory_name,
                _directory_fd=directory_fd,
                _key_fd=key_fd,
            )
            directory_fd = None
            key_fd = None
            return result
        except ControlPlaneSafetyError as exc:
            raise BackupIdentityError(str(exc)) from exc
        finally:
            if key_fd is not None:
                os.close(key_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    @classmethod
    def load_or_create(cls, control_dir: str | Path) -> "BackupIdentity":
        try:
            control_root = ControlPlaneRoot.open(control_dir)
        except ControlPlaneSafetyError as exc:
            raise BackupIdentityError(str(exc)) from exc
        try:
            identity = cls.load_or_create_in_root(control_root)
        except BaseException:
            control_root.close()
            raise
        identity._owns_control_root = True
        return identity

    def verify_attached(self) -> None:
        """Verify that the cached key is still the canonical control identity."""
        self._require_open()
        control_root = self._control_root
        directory_name = self._directory_name
        if (
            control_root is None
            or self._directory_fd < 0
            or self._key_fd < 0
        ):
            raise BackupIdentityError(
                "backup signing identity has no live control-plane attachment"
            )
        try:
            _verify_identity_directory_attachment(
                control_root,
                directory_name,
                self._directory_fd,
            )
            stored_bytes = _read_attached_private_key_descriptor(
                self._directory_fd,
                self._key_fd,
            )
            try:
                stored_key = Ed25519PrivateKey.from_private_bytes(stored_bytes)
            except ValueError as exc:
                raise BackupIdentityError(
                    "backup signing key is not valid Ed25519"
                ) from exc
            stored_public_key = stored_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            if not secrets.compare_digest(
                stored_public_key,
                self._public_key_bytes_unchecked(),
            ):
                raise BackupIdentityError(
                    "backup signing identity was detached or substituted"
                )
            _verify_identity_directory_attachment(
                control_root,
                directory_name,
                self._directory_fd,
            )
        except ControlPlaneSafetyError as exc:
            raise BackupIdentityError(str(exc)) from exc

    @property
    def control_plane_attached(self) -> bool:
        return (
            self._control_root is not None
            and self._directory_fd >= 0
            and self._key_fd >= 0
        )

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        key_fd = self._key_fd
        directory_fd = self._directory_fd
        control_root = self._control_root
        owns_control_root = self._owns_control_root
        self._key_fd = -1
        self._directory_fd = -1
        self._control_root = None
        self._directory_name = None
        self._owns_control_root = False
        if key_fd >= 0:
            try:
                os.close(key_fd)
            except OSError:
                pass
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        if owns_control_root and control_root is not None:
            control_root.close()

    def __del__(self) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise BackupIdentityError("backup signing identity is closed")

    @property
    def public_key_bytes(self) -> bytes:
        self._require_open()
        if self.control_plane_attached:
            self.verify_attached()
        return self._public_key_bytes_unchecked()

    def _public_key_bytes_unchecked(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def fingerprint_sha256(self) -> str:
        return public_key_fingerprint(self.public_key_bytes)

    def sign_manifest(self, canonical_manifest: bytes) -> bytes:
        """Create the raw detached signature for canonical manifest bytes."""
        self._require_open()
        if not isinstance(canonical_manifest, bytes):
            raise BackupIdentityError("canonical manifest must be bytes")
        if self.control_plane_attached:
            self.verify_attached()
        signature = self._private_key.sign(
            MANIFEST_SIGNATURE_DOMAIN + canonical_manifest
        )
        if self.control_plane_attached:
            self.verify_attached()
        return signature


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
