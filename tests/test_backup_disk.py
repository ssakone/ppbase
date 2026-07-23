from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import ppbase.backup.disk as disk
from ppbase.backup.disk import BackupDiskSpaceError, local_tree_size, require_disk_space


def test_local_tree_size_counts_regular_files(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    (storage / "nested").mkdir(parents=True)
    (storage / "one.bin").write_bytes(b"123")
    (storage / "nested" / "two.bin").write_bytes(b"4567")

    assert local_tree_size(storage) == 7


def test_local_tree_size_rejects_symlinks(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    target = tmp_path / "target"
    target.write_text("secret", encoding="utf-8")
    (storage / "linked").symlink_to(target)

    with pytest.raises(BackupDiskSpaceError, match="forbidden symlink"):
        local_tree_size(storage)


def test_require_disk_space_reports_required_and_available_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        disk.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1_000, used=900, free=100),
    )

    with pytest.raises(BackupDiskSpaceError) as rejected:
        require_disk_space(tmp_path / "missing", 200, operation="native backup")

    message = str(rejected.value)
    assert "native backup" in message
    assert "requires at least" in message
    assert "only 100 bytes" in message
