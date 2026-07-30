from __future__ import annotations

import os
from pathlib import Path

import pytest

from ppbase.backup.control import RuntimeDataRoot
from ppbase.backup.operations import (
    BackupInUseError,
    BackupMaterializationBusyError,
    BackupOperationBusyError,
    BackupOperationCoordinator,
    BackupOperationSafetyError,
    BackupOperationTargetError,
    backup_operation_available,
)


def _coordinators(
    data_dir: Path,
) -> tuple[
    RuntimeDataRoot,
    RuntimeDataRoot,
    BackupOperationCoordinator,
    BackupOperationCoordinator,
]:
    first_root = RuntimeDataRoot.open(data_dir)
    second_root = RuntimeDataRoot.open(data_dir)
    return (
        first_root,
        second_root,
        BackupOperationCoordinator(first_root),
        BackupOperationCoordinator(second_root),
    )


def _close_all(*values: object) -> None:
    for value in values:
        close = getattr(value, "close", None)
        if close is not None:
            close()


def test_coordinator_creates_no_lock_files_or_control_directory(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "pb_data"
    root = RuntimeDataRoot.open(data_dir)
    coordinator = BackupOperationCoordinator(root)
    try:
        assert sorted(path.name for path in data_dir.iterdir()) == ["backups"]
        assert list((data_dir / "backups").iterdir()) == []
    finally:
        _close_all(coordinator, root)


def test_global_mutations_are_serialized_on_data_dir(tmp_path: Path) -> None:
    values = _coordinators(tmp_path / "pb_data")
    first_root, second_root, first, second = values
    try:
        with first.global_exclusive() as lease:
            lease.verify_attached()
            with pytest.raises(BackupOperationBusyError):
                with second.global_exclusive():
                    pass
        with second.global_exclusive():
            pass
    finally:
        _close_all(first, second, first_root, second_root)


def test_availability_probe_reflects_global_operation_lock(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    root = RuntimeDataRoot.open(data_dir)
    coordinator = BackupOperationCoordinator(root)
    try:
        assert backup_operation_available(data_dir) is True
        with coordinator.global_exclusive():
            assert backup_operation_available(data_dir) is False
        assert backup_operation_available(data_dir) is True
    finally:
        _close_all(coordinator, root)


def test_availability_probe_does_not_create_missing_roots(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    data_dir.mkdir()

    assert backup_operation_available(data_dir) is False
    assert not (data_dir / "backups").exists()


def test_shared_backup_use_allows_parallel_readers(tmp_path: Path) -> None:
    values = _coordinators(tmp_path / "pb_data")
    first_root, second_root, first, second = values
    try:
        with first.backup_shared("first.zip"):
            with second.backup_shared("second.zip"):
                pass
    finally:
        _close_all(first, second, first_root, second_root)


def test_exclusive_backup_use_blocks_any_archive_reader(tmp_path: Path) -> None:
    values = _coordinators(tmp_path / "pb_data")
    first_root, second_root, first, second = values
    try:
        with first.backup_shared("first.zip"):
            with pytest.raises(BackupInUseError):
                with second.backup_exclusive("second.zip"):
                    pass
        with second.backup_exclusive("second.zip"):
            pass
    finally:
        _close_all(first, second, first_root, second_root)


def test_materialization_uses_the_archive_directory_lock(tmp_path: Path) -> None:
    values = _coordinators(tmp_path / "pb_data")
    first_root, second_root, first, second = values
    try:
        with first.backup_materialization_exclusive("first.zip"):
            with pytest.raises(BackupMaterializationBusyError):
                with second.backup_materialization_exclusive("second.zip"):
                    pass
    finally:
        _close_all(first, second, first_root, second_root)


@pytest.mark.parametrize("value", ["", "../backup.zip", "nested/backup.zip"])
def test_scoped_leases_reject_unsafe_backup_keys(
    tmp_path: Path,
    value: str,
) -> None:
    root = RuntimeDataRoot.open(tmp_path / "pb_data")
    coordinator = BackupOperationCoordinator(root)
    try:
        with pytest.raises(BackupOperationTargetError):
            with coordinator.backup_shared(value):
                pass
    finally:
        _close_all(coordinator, root)


def test_lease_detects_data_dir_substitution(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    root = RuntimeDataRoot.open(data_dir)
    coordinator = BackupOperationCoordinator(root)
    try:
        with coordinator.global_exclusive() as lease:
            moved = tmp_path / "moved-data"
            os.rename(data_dir, moved)
            data_dir.mkdir()
            with pytest.raises(BackupOperationSafetyError):
                lease.verify_attached()
    finally:
        _close_all(coordinator, root)


def test_closed_coordinator_and_lease_fail_closed(tmp_path: Path) -> None:
    root = RuntimeDataRoot.open(tmp_path / "pb_data")
    coordinator = BackupOperationCoordinator(root)
    lease = coordinator._acquire(
        scope="global",
        mode="exclusive",
        target="data",
        backup_id=None,
    )
    lease.close()
    with pytest.raises(BackupOperationSafetyError):
        lease.verify_attached()
    coordinator.close()
    with pytest.raises(BackupOperationSafetyError):
        coordinator.verify_attached()
    root.close()
