from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import ppbase.backup.control as control_module
from ppbase.backup.control import (
    ControlPlaneSafetyError,
    ensure_runtime_backup_roots,
    inspect_runtime_backup_root,
)
from ppbase.backup.models import BackupStateError
from ppbase.backup.storage import LocalBackupStore


def _settings(backup_root: Path, control_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        backup_root=str(backup_root),
        backup_control_dir=str(control_root),
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_runtime_roots_are_created_private_independent_of_umask(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    control_root = tmp_path / "control"
    previous_umask = os.umask(0)
    try:
        ensure_runtime_backup_roots(_settings(backup_root, control_root))
    finally:
        os.umask(previous_umask)

    assert _mode(backup_root) == 0o700
    assert _mode(control_root) == 0o700


def test_runtime_roots_normalize_existing_owned_directories(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    control_root = tmp_path / "control"
    backup_root.mkdir(mode=0o755)
    control_root.mkdir(mode=0o755)

    ensure_runtime_backup_roots(_settings(backup_root, control_root))

    assert _mode(backup_root) == 0o700
    assert _mode(control_root) == 0o700


def test_runtime_root_refuses_final_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    linked_root = tmp_path / "backups"
    linked_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ControlPlaneSafetyError, match="symlink"):
        ensure_runtime_backup_roots(
            _settings(linked_root, tmp_path / "control")
        )

    assert linked_root.is_symlink()
    assert _mode(outside) == 0o755

    with pytest.raises(BackupStateError, match="safely"):
        LocalBackupStore(linked_root)


def test_runtime_root_refuses_symlinked_ancestor_without_external_creation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    linked_ancestor = tmp_path / "linked"
    linked_ancestor.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ControlPlaneSafetyError, match="symlink"):
        ensure_runtime_backup_roots(
            _settings(
                linked_ancestor / "backups",
                tmp_path / "control",
            )
        )

    assert not (outside / "backups").exists()


def test_runtime_root_refuses_non_sticky_writable_ancestor(
    tmp_path: Path,
) -> None:
    unsafe_ancestor = tmp_path / "unsafe"
    unsafe_ancestor.mkdir(mode=0o700)
    unsafe_ancestor.chmod(0o777)
    backup_root = unsafe_ancestor / "backups"

    with pytest.raises(ControlPlaneSafetyError, match="non-sticky writable"):
        ensure_runtime_backup_roots(
            _settings(backup_root, tmp_path / "control")
        )

    assert not backup_root.exists()


def test_runtime_root_accepts_sticky_writable_ancestor(tmp_path: Path) -> None:
    sticky_ancestor = tmp_path / "sticky"
    sticky_ancestor.mkdir(mode=0o700)
    sticky_ancestor.chmod(0o1777)
    backup_root = sticky_ancestor / "backups"
    control_root = sticky_ancestor / "control"

    ensure_runtime_backup_roots(_settings(backup_root, control_root))

    assert _mode(backup_root) == 0o700
    assert _mode(control_root) == 0o700


def test_runtime_root_refuses_foreign_owned_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign_ancestor = tmp_path / "foreign"
    foreign_ancestor.mkdir(mode=0o700)
    foreign_identity = foreign_ancestor.stat()
    real_fstat = control_module.os.fstat

    def foreign_fstat(descriptor: int):
        info = real_fstat(descriptor)
        if (
            info.st_dev == foreign_identity.st_dev
            and info.st_ino == foreign_identity.st_ino
        ):
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_uid=os.geteuid() + 1,
                st_dev=info.st_dev,
                st_ino=info.st_ino,
            )
        return info

    monkeypatch.setattr(control_module.os, "fstat", foreign_fstat)

    with pytest.raises(ControlPlaneSafetyError, match="root or the service user"):
        ensure_runtime_backup_roots(
            _settings(
                foreign_ancestor / "backups",
                tmp_path / "control",
            )
        )

    assert not (foreign_ancestor / "backups").exists()


def test_runtime_root_inspection_is_read_only(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "backups"

    inspection = inspect_runtime_backup_root(missing)

    assert inspection.exists is False
    assert not missing.exists()
