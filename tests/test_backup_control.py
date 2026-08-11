from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ppbase.backup.control import (
    ControlPlaneSafetyError,
    RuntimeDataRoot,
    ensure_runtime_backup_roots,
)


def _settings(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=str(data_dir))


def test_runtime_layout_creates_only_pb_data_and_backups(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    legacy_control = tmp_path / "pb_backup_control"

    ensure_runtime_backup_roots(_settings(data_dir))

    assert data_dir.is_dir()
    assert (data_dir / "backups").is_dir()
    assert not legacy_control.exists()
    assert sorted(path.name for path in data_dir.iterdir()) == ["backups"]


def test_runtime_layout_preserves_existing_data_dir_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    data_dir.mkdir(mode=0o755)
    os.chmod(data_dir, 0o755)

    ensure_runtime_backup_roots(_settings(data_dir))

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE((data_dir / "backups").stat().st_mode) == 0o755


def test_runtime_layout_rejects_symlinked_backups_entry(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    outside = tmp_path / "outside"
    data_dir.mkdir()
    outside.mkdir()
    (data_dir / "backups").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ControlPlaneSafetyError):
        ensure_runtime_backup_roots(_settings(data_dir))


def test_runtime_data_root_accepts_configured_data_dir_symlink(tmp_path: Path) -> None:
    real_data = tmp_path / "real-data"
    linked_data = tmp_path / "pb_data"
    real_data.mkdir()
    linked_data.symlink_to(real_data, target_is_directory=True)

    with RuntimeDataRoot.open(linked_data) as root:
        assert root.path == real_data.resolve()
        root.verify_attached()


def test_runtime_data_root_detects_replacement(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    root = RuntimeDataRoot.open(data_dir)
    try:
        os.rename(data_dir, tmp_path / "moved-data")
        data_dir.mkdir()
        with pytest.raises(ControlPlaneSafetyError):
            root.verify_attached()
    finally:
        root.close()
