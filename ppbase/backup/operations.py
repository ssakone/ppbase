"""Cross-process leases anchored to the existing PPBase data directories.

No lock namespace is created on disk. Global mutations lock the already-open
``data_dir`` directory, while backup use locks the existing
``data_dir/backups`` directory. ``flock`` releases automatically when a process
exits or crashes.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Literal

from ppbase.backup.archive_store import validate_backup_key
from ppbase.backup.control import (
    ControlPlaneSafetyError,
    RuntimeDataRoot,
    directory_open_flags,
    same_file_identity,
)


LeaseMode = Literal["shared", "exclusive"]
LeaseScope = Literal["global", "backup", "materialization"]
_LockTarget = Literal["data", "backups"]


class BackupOperationError(RuntimeError):
    """Stable error raised by the native-backup operation coordinator."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BackupOperationSafetyError(BackupOperationError):
    """The runtime directory lock target is detached or unsafe."""

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
    """The backup archive directory is already in incompatible use."""

    def __init__(self) -> None:
        super().__init__(
            "backup_in_use",
            "A local backup is currently in use.",
        )


class BackupMaterializationBusyError(BackupOperationError):
    """A backup download is already being prepared."""

    def __init__(self) -> None:
        super().__init__(
            "backup_download_in_progress",
            "A local backup is already being prepared for download.",
        )


class BackupOperationTargetError(BackupOperationError):
    """A scoped lease was requested for an invalid backup key."""

    def __init__(self) -> None:
        super().__init__(
            "invalid_backup_id",
            "The backup operation target is invalid.",
        )


def _validate_backup_reference(value: str) -> str:
    try:
        return validate_backup_key(value)
    except ValueError as exc:
        raise BackupOperationTargetError() from exc


def _open_independent_directory(descriptor: int) -> int:
    """Open the pinned directory again with an independent flock domain."""
    opened: int | None = None
    try:
        expected = os.fstat(descriptor)
        opened = os.open(".", directory_open_flags(), dir_fd=descriptor)
        actual = os.fstat(opened)
        if (
            not stat.S_ISDIR(expected.st_mode)
            or not stat.S_ISDIR(actual.st_mode)
            or not same_file_identity(expected, actual)
        ):
            raise BackupOperationSafetyError(
                "The native backup lock target changed while it was opened."
            )
        result = opened
        opened = None
        return result
    except BackupOperationSafetyError:
        raise
    except OSError as exc:
        raise BackupOperationSafetyError(
            "The native backup lock target cannot be opened safely."
        ) from exc
    finally:
        if opened is not None:
            os.close(opened)


@dataclass(slots=True)
class BackupOperationLease:
    """One held nonblocking directory lease; callers must close it."""

    scope: LeaseScope
    mode: LeaseMode
    backup_id: str | None
    _coordinator: "BackupOperationCoordinator" = field(
        repr=False,
        compare=False,
    )
    _target: _LockTarget = field(repr=False, compare=False)
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
        self._coordinator.verify_lease(self._target, self._lock_fd)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptor = self._lock_fd
        self._lock_fd = -1
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> "BackupOperationLease":
        self.verify_attached()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class BackupOperationCoordinator:
    """Coordinate native-backup operations without creating lock files."""

    def __init__(
        self,
        data_root: RuntimeDataRoot,
        *,
        create_missing: bool = True,
    ) -> None:
        if not isinstance(data_root, RuntimeDataRoot):
            raise BackupOperationSafetyError(
                "A descriptor-anchored PPBase data directory is required."
            )
        self._data_root = data_root
        self._backups_fd = -1
        self._closed = False
        try:
            self._backups_fd = data_root.open_child_directory(
                "backups",
                create_missing=create_missing,
            )
            self.verify_attached()
        except ControlPlaneSafetyError as exc:
            self.close()
            raise BackupOperationSafetyError(
                "The PPBase backup directory is missing or unsafe."
            ) from exc
        except BaseException:
            self.close()
            raise

    def _require_open(self) -> None:
        if self._closed or self._backups_fd < 0:
            raise BackupOperationSafetyError(
                "The native backup operation coordinator is closed."
            )

    def fileno(self) -> int:
        self._require_open()
        return self._backups_fd

    def verify_attached(self) -> None:
        self._require_open()
        try:
            self._data_root.verify_attached()
            self._data_root.verify_child_directory("backups", self._backups_fd)
        except ControlPlaneSafetyError as exc:
            raise BackupOperationSafetyError(
                "The PPBase backup directories were detached or substituted."
            ) from exc

    def _target_fd(self, target: _LockTarget) -> int:
        self.verify_attached()
        return (
            self._data_root.fileno()
            if target == "data"
            else self._backups_fd
        )

    def verify_lease(self, target: _LockTarget, lock_fd: int) -> None:
        expected_fd = self._target_fd(target)
        try:
            expected = os.fstat(expected_fd)
            opened = os.fstat(lock_fd)
        except OSError as exc:
            raise BackupOperationSafetyError(
                "The native backup operation lease was detached."
            ) from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not same_file_identity(expected, opened)
        ):
            raise BackupOperationSafetyError(
                "The native backup operation lease was substituted."
            )
        self.verify_attached()

    def _acquire(
        self,
        *,
        scope: LeaseScope,
        mode: LeaseMode,
        target: _LockTarget,
        backup_id: str | None,
    ) -> BackupOperationLease:
        target_fd = self._target_fd(target)
        lock_fd: int | None = None
        locked = False
        try:
            lock_fd = _open_independent_directory(target_fd)
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
            self.verify_lease(target, lock_fd)
            lease = BackupOperationLease(
                scope=scope,
                mode=mode,
                backup_id=backup_id,
                _coordinator=self,
                _target=target,
                _lock_fd=lock_fd,
            )
            lock_fd = None
            return lease
        finally:
            if lock_fd is not None:
                if locked:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(lock_fd)

    @contextmanager
    def global_exclusive(self) -> Iterator[BackupOperationLease]:
        """Acquire the single nonblocking mutation lease on ``data_dir``."""
        lease = self._acquire(
            scope="global",
            mode="exclusive",
            target="data",
            backup_id=None,
        )
        try:
            yield lease
        finally:
            lease.close()

    @contextmanager
    def backup_shared(self, backup_id: str) -> Iterator[BackupOperationLease]:
        """Acquire shared use of the local backup archive directory."""
        selected = _validate_backup_reference(backup_id)
        lease = self._acquire(
            scope="backup",
            mode="shared",
            target="backups",
            backup_id=selected,
        )
        try:
            yield lease
        finally:
            lease.close()

    @contextmanager
    def backup_exclusive(self, backup_id: str) -> Iterator[BackupOperationLease]:
        """Acquire exclusive mutation of the local backup archive directory."""
        selected = _validate_backup_reference(backup_id)
        lease = self._acquire(
            scope="backup",
            mode="exclusive",
            target="backups",
            backup_id=selected,
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
        """Acquire single-flight preparation of a local backup download."""
        selected = _validate_backup_reference(backup_id)
        lease = self._acquire(
            scope="materialization",
            mode="exclusive",
            target="backups",
            backup_id=selected,
        )
        try:
            yield lease
        finally:
            lease.close()

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        descriptor = self._backups_fd
        self._backups_fd = -1
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


def backup_operation_available(data_dir: str | os.PathLike[str]) -> bool:
    """Return whether a global backup mutation lease is available now.

    This is a read-only status probe, not a reservation.  It opens the runtime
    roots without creating missing entries, briefly attempts the same
    inter-process lock used by create/upload/restore, and releases it
    immediately.
    """
    try:
        with RuntimeDataRoot.open(data_dir, create_missing=False) as data_root:
            with BackupOperationCoordinator(
                data_root,
                create_missing=False,
            ) as coordinator:
                with coordinator.global_exclusive():
                    return True
    except (BackupOperationError, ControlPlaneSafetyError, OSError):
        return False
