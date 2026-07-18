from __future__ import annotations

import multiprocessing
import os
import stat
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from ppbase.backup import operations as operations_module
from ppbase.backup.control import ControlPlaneRoot
from ppbase.backup.operations import (
    BackupInUseError,
    BackupMaterializationBusyError,
    BackupOperationBusyError,
    BackupOperationCoordinator,
    BackupOperationSafetyError,
)


def _initialize_operation_coordinator_in_process(
    control_dir: str,
    worker_started: Any,
    initialization_locked: Any,
    release_initialization: Any,
    results: Any,
    *,
    block_after_lock: bool,
) -> None:
    worker_started.set()
    if block_after_lock:
        real_flock = operations_module.fcntl.flock

        def blocked_flock(descriptor: int, operation: int) -> None:
            real_flock(descriptor, operation)
            if operation == operations_module.fcntl.LOCK_EX:
                initialization_locked.set()
                if not release_initialization.wait(timeout=10):
                    raise AssertionError(
                        "timed out releasing namespace initialization"
                    )

        operations_module.fcntl.flock = blocked_flock

    root = None
    coordinator = None
    try:
        root = ControlPlaneRoot.open(
            Path(control_dir),
            create_missing=False,
        )
        coordinator = BackupOperationCoordinator(root)
    except BaseException as exc:
        results.put(("error", repr(exc)))
        raise
    else:
        results.put(("ok", None))
    finally:
        if coordinator is not None:
            coordinator.close()
        if root is not None:
            root.close()


def _open_coordinators(
    tmp_path: Path,
) -> tuple[
    Path,
    ControlPlaneRoot,
    ControlPlaneRoot,
    BackupOperationCoordinator,
    BackupOperationCoordinator,
]:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    first_root = ControlPlaneRoot.open(control, create_missing=False)
    second_root = ControlPlaneRoot.open(control, create_missing=False)
    first = BackupOperationCoordinator(first_root)
    second = BackupOperationCoordinator(second_root)
    return control, first_root, second_root, first, second


def _close_coordinators(
    first_root: ControlPlaneRoot,
    second_root: ControlPlaneRoot,
    first: BackupOperationCoordinator,
    second: BackupOperationCoordinator,
) -> None:
    first.close()
    second.close()
    first_root.close()
    second_root.close()


def test_global_operation_is_exclusive_across_coordinator_instances(
    tmp_path: Path,
) -> None:
    _control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    try:
        with first.global_exclusive() as lease:
            assert lease.scope == "global"
            assert lease.mode == "exclusive"
            assert lease.closed is False
            with pytest.raises(BackupOperationBusyError) as error:
                with second.global_exclusive():
                    pass
            assert error.value.code == "backup_operation_in_progress"

        assert lease.closed is True
        with second.global_exclusive():
            pass
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_per_backup_shared_and_exclusive_leases_conflict_safely(
    tmp_path: Path,
) -> None:
    _control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    try:
        with first.backup_shared("backup-a"):
            with second.backup_shared("backup-a"):
                with pytest.raises(BackupInUseError) as error:
                    with second.backup_exclusive("backup-a"):
                        pass
                assert error.value.code == "backup_in_use"

            with second.backup_exclusive("backup-b"):
                pass

        with first.backup_exclusive("backup-a"):
            with pytest.raises(BackupInUseError):
                with second.backup_shared("backup-a"):
                    pass
            with pytest.raises(BackupInUseError):
                with second.backup_exclusive("backup-a"):
                    pass

        with second.backup_exclusive("backup-a"):
            pass
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_backup_zip_materialization_is_single_flight_across_workers(
    tmp_path: Path,
) -> None:
    _control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    try:
        with first.backup_materialization_exclusive("backup-a") as lease:
            assert lease.scope == "materialization"
            with pytest.raises(BackupMaterializationBusyError) as error:
                with second.backup_materialization_exclusive("backup-a"):
                    pass
            assert error.value.code == "backup_download_in_progress"

            with second.backup_materialization_exclusive("backup-b"):
                pass

        with second.backup_materialization_exclusive("backup-a"):
            pass
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_lease_is_released_and_fd_closed_after_body_exception(
    tmp_path: Path,
) -> None:
    _control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    descriptor = -1
    try:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            with first.global_exclusive() as lease:
                descriptor = lease.fileno()
                raise RuntimeError("synthetic failure")

        assert lease.closed is True
        with pytest.raises(OSError):
            os.fstat(descriptor)
        with second.global_exclusive():
            pass
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_lock_files_are_private_owned_regular_single_link_files(
    tmp_path: Path,
) -> None:
    control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    try:
        with first.global_exclusive():
            pass
        with first.backup_shared("backup-a"):
            pass

        entries = sorted((control / "operations").iterdir())
        assert len(entries) == 259
        assert entries[0].name == "backup-slot-000.lock"
        assert {entry.name for entry in entries}.issuperset(
            {
                "global.lock",
                "materialize-slot-127.lock",
                "namespace-init.lock",
                "namespace-v1.ready",
            }
        )
        for entry in entries:
            info = entry.lstat()
            assert stat.S_ISREG(info.st_mode)
            assert info.st_uid == os.geteuid()
            assert stat.S_IMODE(info.st_mode) == 0o600
            assert info.st_nlink == 1
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_per_backup_lock_namespace_is_bounded_for_nonexistent_ids(
    tmp_path: Path,
) -> None:
    control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    operations = control / "operations"
    try:
        before = {
            entry.name: (entry.stat().st_dev, entry.stat().st_ino)
            for entry in operations.iterdir()
        }

        for index in range(300):
            backup_id = f"missing-backup-{index}"
            with first.backup_shared(backup_id):
                pass
            with second.backup_materialization_exclusive(backup_id):
                pass

        after = {
            entry.name: (entry.stat().st_dev, entry.stat().st_ino)
            for entry in operations.iterdir()
        }
        assert after == before
        assert len(after) <= 259
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_existing_lock_namespace_uses_constant_time_read_only_fast_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    first_root = ControlPlaneRoot.open(control, create_missing=False)
    first = BackupOperationCoordinator(first_root)
    second_root = ControlPlaneRoot.open(control, create_missing=False)
    opened_names: list[str] = []
    real_open = operations_module._open_lock_file_at

    def tracked_open(operations_fd, lock_name, *, create_missing):
        opened_names.append(lock_name)
        return real_open(
            operations_fd,
            lock_name,
            create_missing=create_missing,
        )

    def reject_fsync(_descriptor):
        raise AssertionError("existing namespace must not be fsynced")

    monkeypatch.setattr(operations_module, "_open_lock_file_at", tracked_open)
    monkeypatch.setattr(operations_module.os, "fsync", reject_fsync)
    second = None
    try:
        second = BackupOperationCoordinator(second_root)
        assert opened_names == ["namespace-v1.ready"]
    finally:
        if second is not None:
            second.close()
        first.close()
        first_root.close()
        second_root.close()


def test_partial_lock_namespace_is_resumed_without_replacing_safe_inodes(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    operations = control / "operations"
    operations.mkdir(mode=0o700)
    existing_names = {
        "namespace-init.lock",
        "global.lock",
        "backup-slot-017.lock",
        "materialize-slot-094.lock",
    }
    for name in existing_names:
        descriptor = os.open(
            operations / name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)
    existing_identities = {
        name: (
            (info := (operations / name).lstat()).st_dev,
            info.st_ino,
        )
        for name in existing_names
    }

    root = ControlPlaneRoot.open(control, create_missing=False)
    coordinator = None
    try:
        coordinator = BackupOperationCoordinator(root)

        assert (operations / "namespace-v1.ready").is_file()
        assert len(list(operations.iterdir())) == 259
        assert {
            name: (
                (info := (operations / name).lstat()).st_dev,
                info.st_ino,
            )
            for name in existing_names
        } == existing_identities
        with coordinator.global_exclusive():
            pass
    finally:
        if coordinator is not None:
            coordinator.close()
        root.close()


def test_lock_namespace_resumes_after_crash_before_initial_mode_fixup(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    operations = control / "operations"
    operations.mkdir(mode=0o700)
    initialization_lock = operations / "namespace-init.lock"
    previous_umask = os.umask(0o777)
    try:
        descriptor = os.open(
            initialization_lock,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    finally:
        os.umask(previous_umask)
    os.close(descriptor)
    crashed_identity = initialization_lock.lstat()
    assert stat.S_IMODE(crashed_identity.st_mode) == 0

    root = ControlPlaneRoot.open(control, create_missing=False)
    coordinator = None
    try:
        coordinator = BackupOperationCoordinator(root)

        recovered_identity = initialization_lock.lstat()
        assert (recovered_identity.st_dev, recovered_identity.st_ino) == (
            crashed_identity.st_dev,
            crashed_identity.st_ino,
        )
        assert stat.S_IMODE(recovered_identity.st_mode) == 0o600
        assert (operations / "namespace-v1.ready").is_file()
        with coordinator.global_exclusive():
            pass
    finally:
        if coordinator is not None:
            coordinator.close()
        root.close()


def test_lock_namespace_republishes_crash_restricted_ready_marker(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    first_root = ControlPlaneRoot.open(control, create_missing=False)
    first = BackupOperationCoordinator(first_root)
    first.close()
    first_root.close()
    ready_marker = control / "operations" / "namespace-v1.ready"
    ready_marker.chmod(0)
    crashed_identity = ready_marker.lstat()

    second_root = ControlPlaneRoot.open(control, create_missing=False)
    second = None
    try:
        second = BackupOperationCoordinator(second_root)

        recovered_identity = ready_marker.lstat()
        assert (recovered_identity.st_dev, recovered_identity.st_ino) == (
            crashed_identity.st_dev,
            crashed_identity.st_ino,
        )
        assert stat.S_IMODE(recovered_identity.st_mode) == 0o600
        with second.global_exclusive():
            pass
    finally:
        if second is not None:
            second.close()
        second_root.close()


def test_concurrent_coordinators_resume_crash_restricted_namespace(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    operations = control / "operations"
    operations.mkdir(mode=0o700)
    initialization_lock = operations / "namespace-init.lock"
    descriptor = os.open(
        initialization_lock,
        os.O_RDWR | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    os.close(descriptor)
    initialization_lock.chmod(0)

    context = multiprocessing.get_context("spawn")
    first_started = context.Event()
    second_started = context.Event()
    initialization_locked = context.Event()
    unused_locked = context.Event()
    release_initialization = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_initialize_operation_coordinator_in_process,
        args=(
            str(control),
            first_started,
            initialization_locked,
            release_initialization,
            results,
        ),
        kwargs={"block_after_lock": True},
    )
    second = context.Process(
        target=_initialize_operation_coordinator_in_process,
        args=(
            str(control),
            second_started,
            unused_locked,
            release_initialization,
            results,
        ),
        kwargs={"block_after_lock": False},
    )

    first.start()
    try:
        assert first_started.wait(timeout=5)
        assert initialization_locked.wait(timeout=5)
        second.start()
        assert second_started.wait(timeout=5)
        second.join(timeout=0.2)
        assert second.is_alive()

        release_initialization.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert first.exitcode == 0
        assert second.exitcode == 0
    finally:
        release_initialization.set()
        for process in (first, second):
            if process.pid is not None and process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)

    outcomes = []
    for _ in range(2):
        try:
            outcomes.append(results.get(timeout=2))
        except Empty as exc:  # pragma: no cover - child crash diagnostics
            raise AssertionError(
                "operation coordinator worker returned no result"
            ) from exc
    assert {status for status, _value in outcomes} == {"ok"}
    assert stat.S_IMODE(initialization_lock.lstat().st_mode) == 0o600
    assert (operations / "namespace-v1.ready").is_file()
    assert len(list(operations.iterdir())) == 259


def test_hardlinked_lock_file_is_rejected_before_locking(tmp_path: Path) -> None:
    control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    try:
        with first.global_exclusive():
            pass
        global_lock = control / "operations" / "global.lock"
        os.link(global_lock, tmp_path / "unexpected-hardlink")

        with pytest.raises(BackupOperationSafetyError) as error:
            with second.global_exclusive():
                pass
        assert error.value.code == "backup_operation_control_invalid"
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_detached_operations_directory_fails_closed_and_releases_fd(
    tmp_path: Path,
) -> None:
    control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    detached = tmp_path / "detached-operations"
    descriptor = -1
    try:
        with first.global_exclusive() as lease:
            descriptor = lease.fileno()
            (control / "operations").rename(detached)
            (control / "operations").mkdir(mode=0o700)
            with pytest.raises(BackupOperationSafetyError) as error:
                lease.verify_attached()
        assert error.value.code == "backup_operation_control_invalid"
        assert lease.closed is True
        with pytest.raises(OSError):
            os.fstat(descriptor)

        with pytest.raises(BackupOperationSafetyError):
            with first.global_exclusive():
                pass
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_substituted_lock_inode_is_detected_before_release(tmp_path: Path) -> None:
    control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    descriptor = -1
    detached_lock = tmp_path / "detached-backup-lock"
    try:
        with first.backup_shared("backup-a") as lease:
            descriptor = lease.fileno()
            opened = os.fstat(descriptor)
            lock_file = next(
                entry
                for entry in (control / "operations").glob("backup-*.lock")
                if (entry.stat().st_dev, entry.stat().st_ino)
                == (opened.st_dev, opened.st_ino)
            )
            lock_file.rename(detached_lock)
            replacement = os.open(
                lock_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(replacement)
            with pytest.raises(BackupOperationSafetyError) as error:
                lease.verify_attached()
        assert error.value.code == "backup_operation_control_invalid"
        assert lease.closed is True
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_detached_control_root_is_rejected_before_acquisition(
    tmp_path: Path,
) -> None:
    control, first_root, second_root, first, second = _open_coordinators(
        tmp_path
    )
    detached = tmp_path / "detached-control"
    control.rename(detached)
    control.mkdir(mode=0o700)
    try:
        with pytest.raises(BackupOperationSafetyError) as error:
            with first.backup_shared("backup-a"):
                pass
        assert error.value.code == "backup_operation_control_invalid"
    finally:
        _close_coordinators(first_root, second_root, first, second)


def test_coordinator_close_releases_pinned_directory_fd(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    root = ControlPlaneRoot.open(control, create_missing=False)
    coordinator = BackupOperationCoordinator(root)
    descriptor = coordinator.fileno()

    coordinator.close()
    coordinator.close()

    with pytest.raises(OSError):
        os.fstat(descriptor)
    with pytest.raises(BackupOperationSafetyError, match="closed"):
        with coordinator.global_exclusive():
            pass
    root.close()
