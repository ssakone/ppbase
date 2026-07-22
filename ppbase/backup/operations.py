"""Cross-worker operation leases for the native backup subsystem.

The coordinator uses a fixed pool of empty lock files below the
descriptor-anchored backup control plane. ``flock`` provides process-crash
cleanup while retained directory descriptors and attachment checks prevent a
stale coordinator from continuing in a renamed or substituted lock namespace.
The fixed slots bound inode use even when arbitrary nonexistent backup IDs are
requested; hash collisions only serialize otherwise independent operations.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Literal

from ppbase.backup.control import (
    ControlPlaneRoot,
    ControlPlaneSafetyError,
    open_flags,
    same_file_identity,
    verify_directory_attached_at,
)
from ppbase.backup.models import BackupManifestError, validate_backup_id


_OPERATIONS_DIRECTORY = "operations"
_GLOBAL_LOCK_FILENAME = "global.lock"
_NAMESPACE_INITIALIZATION_LOCK_FILENAME = "namespace-init.lock"
_NAMESPACE_READY_FILENAME = "namespace-v1.ready"
_BACKUP_LOCK_DOMAIN = b"PPBASE-NATIVE-BACKUP-OPERATION-LEASE-V1\0"
_MATERIALIZATION_LOCK_DOMAIN = b"PPBASE-NATIVE-BACKUP-MATERIALIZATION-LEASE-V1\0"
_LOCK_SLOT_COUNT = 128

LeaseMode = Literal["shared", "exclusive"]
LeaseScope = Literal["global", "backup", "materialization"]


class BackupOperationError(RuntimeError):
    """Stable error raised by the native-backup operation coordinator."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BackupOperationSafetyError(BackupOperationError):
    """The operation-lock namespace or one of its files is unsafe."""

    def __init__(self, message: str) -> None:
        super().__init__("backup_operation_control_invalid", message)


class BackupOperationBusyError(BackupOperationError):
    """Another global native-backup mutation already owns the lease."""

    def __init__(self) -> None:
        super().__init__(
            "backup_operation_in_progress",
            "Another native backup operation is already active.",
        )


class BackupInUseError(BackupOperationError):
    """The requested backup cannot acquire its shared or exclusive lease."""

    def __init__(self) -> None:
        super().__init__(
            "backup_in_use",
            "The selected backup is currently in use.",
        )


class BackupMaterializationBusyError(BackupOperationError):
    """The same backup is already being materialized for download."""

    def __init__(self) -> None:
        super().__init__(
            "backup_download_in_progress",
            "The selected backup is already being prepared for download.",
        )


class BackupOperationTargetError(BackupOperationError):
    """A per-backup lease was requested for an invalid backup identifier."""

    def __init__(self) -> None:
        super().__init__(
            "invalid_backup_id",
            "The backup operation target is invalid.",
        )


def _validate_lock_file_structure(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_size != 0
    ):
        raise BackupOperationSafetyError(
            "A native backup operation lock file is unsafe."
        )


def _validate_private_lock_file(info: os.stat_result) -> None:
    _validate_lock_file_structure(info)
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise BackupOperationSafetyError(
            "A native backup operation lock file is unsafe."
        )


def _normalize_restrictive_lock_file_mode_at(
    operations_fd: int,
    lock_name: str,
    expected: os.stat_result,
) -> os.stat_result:
    """Repair only crash-created owner permissions without following links."""
    _validate_lock_file_structure(expected)
    mode = stat.S_IMODE(expected.st_mode)
    if mode == 0o600:
        return expected
    if mode & ~0o600:
        raise BackupOperationSafetyError(
            "A native backup operation lock file is unsafe."
        )

    try:
        os.chmod(
            lock_name,
            0o600,
            dir_fd=operations_fd,
            follow_symlinks=False,
        )
        visible_after = os.stat(
            lock_name,
            dir_fd=operations_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise BackupOperationSafetyError(
            "A native backup operation lock file cannot be normalized safely."
        ) from exc
    _validate_private_lock_file(visible_after)
    if not same_file_identity(expected, visible_after):
        raise BackupOperationSafetyError(
            "A native backup operation lock file changed during normalization."
        )
    return visible_after


def _verify_operations_attachment(
    control_root: ControlPlaneRoot,
    operations_fd: int,
) -> None:
    try:
        control_root.verify_attached()
        verify_directory_attached_at(
            control_root.fileno(),
            _OPERATIONS_DIRECTORY,
            operations_fd,
            label="The backup operations directory",
        )
        control_root.verify_attached()
    except ControlPlaneSafetyError as exc:
        raise BackupOperationSafetyError(
            "The native backup operations directory was detached or substituted."
        ) from exc
    except OSError as exc:
        raise BackupOperationSafetyError(
            "The native backup operations directory cannot be verified safely."
        ) from exc


def _verify_lock_attachment(
    control_root: ControlPlaneRoot,
    operations_fd: int,
    lock_name: str,
    lock_fd: int,
) -> None:
    _verify_operations_attachment(control_root, operations_fd)
    try:
        visible = os.stat(
            lock_name,
            dir_fd=operations_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(lock_fd)
        _validate_private_lock_file(visible)
        _validate_private_lock_file(opened)
        if not same_file_identity(visible, opened):
            raise BackupOperationSafetyError(
                "A native backup operation lock file was substituted."
            )

        # Re-read the named entry after validating the open inode.  This catches
        # rename/substitution races at both acquisition and release boundaries.
        visible_after = os.stat(
            lock_name,
            dir_fd=operations_fd,
            follow_symlinks=False,
        )
        _validate_private_lock_file(visible_after)
        if not same_file_identity(visible_after, opened):
            raise BackupOperationSafetyError(
                "A native backup operation lock file was substituted."
            )
    except BackupOperationSafetyError:
        raise
    except OSError as exc:
        raise BackupOperationSafetyError(
            "A native backup operation lock file cannot be verified safely."
        ) from exc
    _verify_operations_attachment(control_root, operations_fd)


def _open_lock_file_at(
    operations_fd: int,
    lock_name: str,
    *,
    create_missing: bool,
) -> int:
    descriptor: int | None = None
    try:
        if create_missing:
            try:
                descriptor = os.open(
                    lock_name,
                    open_flags(os.O_RDWR | os.O_CREAT | os.O_EXCL),
                    0o600,
                    dir_fd=operations_fd,
                )
            except FileExistsError:
                descriptor = None
            else:
                os.fchmod(descriptor, 0o600)

        if descriptor is None:
            expected = os.stat(
                lock_name,
                dir_fd=operations_fd,
                follow_symlinks=False,
            )
            if create_missing:
                expected = _normalize_restrictive_lock_file_mode_at(
                    operations_fd,
                    lock_name,
                    expected,
                )
            else:
                _validate_private_lock_file(expected)
            descriptor = os.open(
                lock_name,
                open_flags(os.O_RDWR),
                dir_fd=operations_fd,
            )
            opened = os.fstat(descriptor)
            _validate_private_lock_file(opened)
            if not same_file_identity(expected, opened):
                raise BackupOperationSafetyError(
                    "A native backup operation lock file changed while opening."
                )

        opened = os.fstat(descriptor)
        visible = os.stat(
            lock_name,
            dir_fd=operations_fd,
            follow_symlinks=False,
        )
        _validate_private_lock_file(opened)
        _validate_private_lock_file(visible)
        if not same_file_identity(opened, visible):
            raise BackupOperationSafetyError(
                "A native backup operation lock file changed while opening."
            )
        if create_missing:
            # The namespace directory is fsynced only after every expected
            # file is present.  Flush reused inodes too so a crash-recovered
            # chmod or earlier file creation is durable before that boundary.
            os.fsync(descriptor)
        result = descriptor
        descriptor = None
        return result
    except BackupOperationSafetyError:
        raise
    except OSError as exc:
        raise BackupOperationSafetyError(
            "A native backup operation lock file cannot be opened safely."
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        # The bounded slot files are intentionally persistent. Persistence
        # gives every worker a stable inode and avoids split lock domains caused
        # by unlinking a file that another process still holds.


def _lock_slot_filename(
    backup_id: str,
    *,
    domain: bytes,
    prefix: str,
) -> str:
    try:
        selected_id = validate_backup_id(backup_id)
    except BackupManifestError as exc:
        raise BackupOperationTargetError() from exc
    digest = hashlib.sha256(domain + selected_id.encode("ascii")).digest()
    slot = int.from_bytes(digest[:8], "big") % _LOCK_SLOT_COUNT
    return f"{prefix}-slot-{slot:03d}.lock"


def _backup_lock_filename(backup_id: str) -> str:
    return _lock_slot_filename(
        backup_id,
        domain=_BACKUP_LOCK_DOMAIN,
        prefix="backup",
    )


def _materialization_lock_filename(backup_id: str) -> str:
    return _lock_slot_filename(
        backup_id,
        domain=_MATERIALIZATION_LOCK_DOMAIN,
        prefix="materialize",
    )


def _lock_namespace_filenames() -> Iterator[str]:
    yield _GLOBAL_LOCK_FILENAME
    for prefix in ("backup", "materialize"):
        for slot in range(_LOCK_SLOT_COUNT):
            yield f"{prefix}-slot-{slot:03d}.lock"


def _open_optional_lock_file_at(
    operations_fd: int,
    lock_name: str,
) -> int | None:
    try:
        visible = os.stat(
            lock_name,
            dir_fd=operations_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupOperationSafetyError(
            "A native backup operation lock file cannot be inspected safely."
        ) from exc
    _validate_lock_file_structure(visible)
    mode = stat.S_IMODE(visible.st_mode)
    if mode != 0o600:
        if mode & ~0o600:
            raise BackupOperationSafetyError(
                "A native backup operation lock file is unsafe."
            )
        # A crash may leave the final marker at a stricter owner-only mode.
        # Treat it as unpublished so the flock-protected slow path repairs it
        # only after validating and flushing the complete namespace again.
        return None
    return _open_lock_file_at(
        operations_fd,
        lock_name,
        create_missing=False,
    )


def _namespace_is_ready(
    control_root: ControlPlaneRoot,
    operations_fd: int,
) -> bool:
    ready_fd = _open_optional_lock_file_at(
        operations_fd,
        _NAMESPACE_READY_FILENAME,
    )
    if ready_fd is None:
        return False
    try:
        _verify_lock_attachment(
            control_root,
            operations_fd,
            _NAMESPACE_READY_FILENAME,
            ready_fd,
        )
        return True
    finally:
        try:
            os.close(ready_fd)
        except OSError:
            pass


def _initialize_lock_namespace(
    control_root: ControlPlaneRoot,
    operations_fd: int,
) -> None:
    """Publish the bounded namespace once, then use a constant-time fast path."""
    _verify_operations_attachment(control_root, operations_fd)
    if _namespace_is_ready(control_root, operations_fd):
        return

    initialization_fd: int | None = None
    locked = False
    try:
        initialization_fd = _open_lock_file_at(
            operations_fd,
            _NAMESPACE_INITIALIZATION_LOCK_FILENAME,
            create_missing=True,
        )
        _verify_lock_attachment(
            control_root,
            operations_fd,
            _NAMESPACE_INITIALIZATION_LOCK_FILENAME,
            initialization_fd,
        )
        fcntl.flock(initialization_fd, fcntl.LOCK_EX)
        locked = True
        _verify_lock_attachment(
            control_root,
            operations_fd,
            _NAMESPACE_INITIALIZATION_LOCK_FILENAME,
            initialization_fd,
        )
        if _namespace_is_ready(control_root, operations_fd):
            return

        # Initialization is deliberately restartable while holding the stable
        # namespace lock.  A process may have crashed after publishing any
        # subset of these files; safe existing inodes are validated and reused
        # by ``_open_lock_file_at`` while only missing files are created.
        for lock_name in _lock_namespace_filenames():
            lock_fd = _open_lock_file_at(
                operations_fd,
                lock_name,
                create_missing=True,
            )
            try:
                os.close(lock_fd)
            except OSError:
                pass

        # Make the complete slot namespace durable before publishing the ready
        # marker.  Its presence is the constant-time fast-path contract used by
        # later coordinators, so it must always be the final namespace entry.
        os.fsync(operations_fd)
        ready_fd = _open_lock_file_at(
            operations_fd,
            _NAMESPACE_READY_FILENAME,
            create_missing=True,
        )
        try:
            _verify_lock_attachment(
                control_root,
                operations_fd,
                _NAMESPACE_READY_FILENAME,
                ready_fd,
            )
        finally:
            try:
                os.close(ready_fd)
            except OSError:
                pass
        os.fsync(operations_fd)
    except BackupOperationSafetyError:
        raise
    except OSError as exc:
        raise BackupOperationSafetyError(
            "The native backup operation lock namespace cannot be initialized "
            "safely."
        ) from exc
    finally:
        if initialization_fd is not None:
            if locked:
                try:
                    fcntl.flock(initialization_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(initialization_fd)
            except OSError:
                pass
    _verify_operations_attachment(control_root, operations_fd)


@dataclass(slots=True)
class BackupOperationLease:
    """One held nonblocking filesystem lease; callers must close it."""

    scope: LeaseScope
    mode: LeaseMode
    backup_id: str | None
    _control_root: ControlPlaneRoot = field(repr=False, compare=False)
    _operations_fd: int = field(repr=False, compare=False)
    _lock_name: str = field(repr=False, compare=False)
    _lock_fd: int = field(repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def closed(self) -> bool:
        return self._closed

    def fileno(self) -> int:
        if self._closed or self._lock_fd < 0:
            raise BackupOperationSafetyError(
                "The native backup operation lease is closed."
            )
        return self._lock_fd

    def verify_attached(self) -> None:
        if self._closed:
            raise BackupOperationSafetyError(
                "The native backup operation lease is closed."
            )
        _verify_lock_attachment(
            self._control_root,
            self._operations_fd,
            self._lock_name,
            self._lock_fd,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        lock_fd = self._lock_fd
        operations_fd = self._operations_fd
        self._lock_fd = -1
        self._operations_fd = -1
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            try:
                os.close(operations_fd)
            except OSError:
                pass

    def __enter__(self) -> "BackupOperationLease":
        self.verify_attached()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class BackupOperationCoordinator:
    """Coordinate native-backup mutations and per-set use across workers."""

    def __init__(self, control_root: ControlPlaneRoot) -> None:
        if not isinstance(control_root, ControlPlaneRoot):
            raise BackupOperationSafetyError(
                "A descriptor-anchored backup control root is required."
            )
        self._control_root = control_root
        self._operations_fd = -1
        self._closed = False
        try:
            operations_fd = control_root.open_private_directory(
                _OPERATIONS_DIRECTORY,
                label="The backup operations directory",
                create_missing=True,
            )
            self._operations_fd = operations_fd
            _verify_operations_attachment(control_root, operations_fd)
            _initialize_lock_namespace(control_root, operations_fd)
        except ControlPlaneSafetyError as exc:
            self.close()
            raise BackupOperationSafetyError(
                "The native backup operations directory is missing or unsafe."
            ) from exc
        except BaseException:
            self.close()
            raise

    def _require_open(self) -> None:
        if self._closed or self._operations_fd < 0:
            raise BackupOperationSafetyError(
                "The native backup operation coordinator is closed."
            )

    def fileno(self) -> int:
        self._require_open()
        return self._operations_fd

    def verify_attached(self) -> None:
        self._require_open()
        _verify_operations_attachment(
            self._control_root,
            self._operations_fd,
        )

    def _acquire(
        self,
        *,
        scope: LeaseScope,
        mode: LeaseMode,
        lock_name: str,
        backup_id: str | None,
    ) -> BackupOperationLease:
        self.verify_attached()
        operations_fd: int | None = None
        lock_fd: int | None = None
        locked = False
        try:
            try:
                operations_fd = os.dup(self._operations_fd)
            except OSError as exc:
                raise BackupOperationSafetyError(
                    "The native backup operations descriptor cannot be duplicated."
                ) from exc
            _verify_operations_attachment(self._control_root, operations_fd)
            lock_fd = _open_lock_file_at(
                operations_fd,
                lock_name,
                create_missing=False,
            )
            _verify_lock_attachment(
                self._control_root,
                operations_fd,
                lock_name,
                lock_fd,
            )
            flock_mode = (
                fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
            ) | fcntl.LOCK_NB
            try:
                fcntl.flock(lock_fd, flock_mode)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    if scope == "global":
                        raise BackupOperationBusyError() from exc
                    if scope == "materialization":
                        raise BackupMaterializationBusyError() from exc
                    raise BackupInUseError() from exc
                raise BackupOperationSafetyError(
                    "The native backup operation lease cannot be acquired safely."
                ) from exc
            locked = True
            _verify_lock_attachment(
                self._control_root,
                operations_fd,
                lock_name,
                lock_fd,
            )
            lease = BackupOperationLease(
                scope=scope,
                mode=mode,
                backup_id=backup_id,
                _control_root=self._control_root,
                _operations_fd=operations_fd,
                _lock_name=lock_name,
                _lock_fd=lock_fd,
            )
            operations_fd = None
            lock_fd = None
            return lease
        finally:
            if lock_fd is not None:
                if locked:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
            if operations_fd is not None:
                try:
                    os.close(operations_fd)
                except OSError:
                    pass

    @contextmanager
    def global_exclusive(self) -> Iterator[BackupOperationLease]:
        """Acquire the single nonblocking native-backup mutation lease."""
        lease = self._acquire(
            scope="global",
            mode="exclusive",
            lock_name=_GLOBAL_LOCK_FILENAME,
            backup_id=None,
        )
        try:
            yield lease
        finally:
            lease.close()

    @contextmanager
    def backup_shared(self, backup_id: str) -> Iterator[BackupOperationLease]:
        """Acquire nonblocking shared use of one canonical backup set."""
        lock_name = _backup_lock_filename(backup_id)
        lease = self._acquire(
            scope="backup",
            mode="shared",
            lock_name=lock_name,
            backup_id=backup_id,
        )
        try:
            yield lease
        finally:
            lease.close()

    @contextmanager
    def backup_exclusive(self, backup_id: str) -> Iterator[BackupOperationLease]:
        """Acquire nonblocking exclusive mutation of one canonical backup set."""
        lock_name = _backup_lock_filename(backup_id)
        lease = self._acquire(
            scope="backup",
            mode="exclusive",
            lock_name=lock_name,
            backup_id=backup_id,
        )
        try:
            yield lease
        finally:
            lease.close()

    @contextmanager
    def backup_materialization_exclusive(
        self,
        backup_id: str,
    ) -> Iterator[BackupOperationLease]:
        """Single-flight creation of a temporary ZIP for one backup."""
        lock_name = _materialization_lock_filename(backup_id)
        lease = self._acquire(
            scope="materialization",
            mode="exclusive",
            lock_name=lock_name,
            backup_id=backup_id,
        )
        try:
            yield lease
        finally:
            lease.close()

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        descriptor = self._operations_fd
        self._operations_fd = -1
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> "BackupOperationCoordinator":
        self.verify_attached()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()
