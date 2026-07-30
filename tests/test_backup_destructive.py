from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import pytest

import ppbase.backup.destructive as destructive
import ppbase.backup.service as backup_service
from ppbase.backup.control import RuntimeDataRoot
from ppbase.backup.destructive import (
    build_file_reference_inventory,
    DestructiveRestoreError,
    DestructiveRestoreJournal,
    recover_interrupted_destructive_restore,
)


class _TargetDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

    def verify_attached(self) -> None:
        return None

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class _FailingFilesSwappedJournal:
    def __init__(self) -> None:
        self.value: dict[str, object] | None = None
        self.clear_calls = 0

    def write(self, value: dict[str, object]) -> None:
        self.value = value
        if value["status"] == "files_swapped":
            raise DestructiveRestoreError("synthetic files_swapped journal failure")

    def clear(self) -> None:
        self.clear_calls += 1
        self.value = None


def test_storage_swap_rolls_back_when_files_swapped_journal_write_fails(
    tmp_path: Path,
) -> None:
    restore_id = "9" * 32
    data_dir = tmp_path / "pb_data"
    active_storage = data_dir / "storage"
    active_storage.mkdir(parents=True)
    (active_storage / "old.txt").write_text("old", encoding="utf-8")
    (data_dir / ".jwt_secret").write_text("old-secret", encoding="utf-8")

    work_root = data_dir / ".ppbase-restore"
    target_path = work_root / restore_id
    restored_storage = target_path / "storage"
    restored_storage.mkdir(parents=True)
    (restored_storage / "new.txt").write_text("new", encoding="utf-8")
    (target_path / ".jwt_secret").write_text("new-secret", encoding="utf-8")
    target = _TargetDirectory(target_path)
    journal = _FailingFilesSwappedJournal()
    prepared = destructive.PreparedStorageRestore(
        restore_id=restore_id,
        data_dir=data_dir,
        work_root=work_root,
        target=target,  # type: ignore[arg-type]
        journal=journal,  # type: ignore[arg-type]
        file_reference_inventory={},
        previous_storage_name=f".ppbase-storage-before-{restore_id}",
        previous_secret_name=f".ppbase-secret-before-{restore_id}",
    )

    try:
        with pytest.raises(
            DestructiveRestoreError,
            match="synthetic files_swapped journal failure",
        ):
            prepared.swap_into_place()
    finally:
        target.close()

    assert (data_dir / "storage" / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (data_dir / "storage" / "new.txt").exists()
    assert (data_dir / ".jwt_secret").read_text(encoding="utf-8") == "old-secret"
    assert journal.value is None
    assert journal.clear_calls == 1
    assert prepared._swapped is False


@pytest.mark.asyncio
async def test_cancelled_storage_swap_finishes_rollback_before_propagating() -> None:
    swap_finished = threading.Event()
    release_swap = threading.Event()
    rollback_finished = threading.Event()

    class Prepared:
        def swap_into_place(self) -> None:
            swap_finished.set()
            if not release_swap.wait(timeout=2):
                raise RuntimeError("test did not release the swap worker")

        def rollback(self) -> None:
            rollback_finished.set()

    task = asyncio.create_task(
        backup_service._swap_prepared_storage_or_rollback(Prepared())  # type: ignore[arg-type]
    )
    assert await asyncio.to_thread(swap_finished.wait, 2)
    task.cancel()
    release_swap.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert rollback_finished.is_set()


@pytest.mark.asyncio
async def test_storage_swap_rollback_failure_is_explicitly_fail_closed() -> None:
    class Prepared:
        def swap_into_place(self) -> None:
            raise RuntimeError("synthetic swap failure")

        def rollback(self) -> None:
            raise OSError("synthetic rollback failure")

    with pytest.raises(
        backup_service._StorageSwapRollbackFailed,
        match="could not be activated or rolled back safely",
    ):
        await backup_service._swap_prepared_storage_or_rollback(  # type: ignore[arg-type]
            Prepared()
        )


def _settings(tmp_path: Path) -> SimpleNamespace:
    data_dir = tmp_path / "pb_data"
    data_dir.mkdir(mode=0o700)
    return SimpleNamespace(data_dir=str(data_dir))


def _write_journal(settings: SimpleNamespace, value: dict[str, object]) -> None:
    root = RuntimeDataRoot.open(Path(settings.data_dir))
    journal = DestructiveRestoreJournal(root)
    try:
        journal.write(value)
    finally:
        journal.close()
        root.close()


def _journal_value(settings: SimpleNamespace, restore_id: str) -> dict[str, object]:
    data_dir = Path(settings.data_dir)
    work_root = data_dir / ".ppbase-restore"
    work_dir = work_root / restore_id
    work_dir.mkdir(parents=True, mode=0o700)
    return {
        "version": 1,
        "restoreId": restore_id,
        "dataDir": str(data_dir),
        "workRoot": str(work_root),
        "workDir": str(work_dir),
        "previousStorage": f".ppbase-storage-before-{restore_id}",
        "previousSecret": f".ppbase-secret-before-{restore_id}",
        "localFileReferenceInventory": build_file_reference_inventory(()),
        "status": "files_swapped",
    }


@pytest.mark.asyncio
async def test_recovery_rejects_database_marker_without_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)

    async def marker(_connection: object) -> str:
        return "a" * 32

    monkeypatch.setattr(destructive, "read_database_restore_marker", marker)

    with pytest.raises(
        DestructiveRestoreError,
        match="marker exists without its filesystem journal",
    ):
        await recover_interrupted_destructive_restore(settings, object())


@pytest.mark.asyncio
async def test_recovery_preserves_data_dir_mode_without_control_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    os.chmod(data_dir, 0o755)

    async def no_marker(_connection: object) -> None:
        return None

    monkeypatch.setattr(destructive, "read_database_restore_marker", no_marker)

    result = await recover_interrupted_destructive_restore(settings, object())

    assert result is None
    assert stat.S_IMODE(data_dir.lstat().st_mode) == 0o755
    assert not (tmp_path / "pb_backup_control").exists()


@pytest.mark.asyncio
async def test_recovery_rejects_work_path_outside_restore_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    restore_id = "b" * 32
    value = _journal_value(settings, restore_id)
    value["workDir"] = str(Path(settings.data_dir) / "storage")
    _write_journal(settings, value)

    async def no_marker(_connection: object) -> None:
        return None

    monkeypatch.setattr(destructive, "read_database_restore_marker", no_marker)

    with pytest.raises(DestructiveRestoreError, match="working paths are invalid"):
        await recover_interrupted_destructive_restore(settings, object())


@pytest.mark.asyncio
async def test_recovery_rolls_files_back_when_database_did_not_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    restore_id = "c" * 32
    data_dir = Path(settings.data_dir)
    value = _journal_value(settings, restore_id)
    (data_dir / "storage").mkdir()
    (data_dir / "storage" / "new.txt").write_text("new", encoding="utf-8")
    (data_dir / ".jwt_secret").write_text("new-secret", encoding="utf-8")
    old_storage = data_dir / str(value["previousStorage"])
    old_storage.mkdir()
    (old_storage / "old.txt").write_text("old", encoding="utf-8")
    (data_dir / str(value["previousSecret"])).write_text(
        "old-secret",
        encoding="utf-8",
    )
    _write_journal(settings, value)

    async def no_marker(_connection: object) -> None:
        return None

    monkeypatch.setattr(destructive, "read_database_restore_marker", no_marker)

    result = await recover_interrupted_destructive_restore(settings, object())

    assert result == {"restoreId": restore_id, "outcome": "rolled_back"}
    assert (data_dir / "storage" / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (data_dir / "storage" / "new.txt").exists()
    assert (data_dir / ".jwt_secret").read_text(encoding="utf-8") == "old-secret"
    assert not Path(str(value["workDir"])).exists()


@pytest.mark.asyncio
async def test_recovery_finalizes_files_when_database_commit_marker_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    restore_id = "d" * 32
    data_dir = Path(settings.data_dir)
    value = _journal_value(settings, restore_id)
    (data_dir / "storage").mkdir()
    (data_dir / "storage" / "restored.txt").write_text("restored", encoding="utf-8")
    (data_dir / ".jwt_secret").write_text("restored-secret", encoding="utf-8")
    old_storage = data_dir / str(value["previousStorage"])
    old_storage.mkdir()
    (old_storage / "old.txt").write_text("old", encoding="utf-8")
    (data_dir / str(value["previousSecret"])).write_text(
        "old-secret",
        encoding="utf-8",
    )
    _write_journal(settings, value)
    cleared: list[str] = []
    commits: list[bool] = []

    class Connection:
        async def commit(self) -> None:
            commits.append(True)

    async def marker(_connection: object) -> str:
        return restore_id

    async def clear(_connection: object, selected_restore_id: str) -> None:
        root = RuntimeDataRoot.open(Path(settings.data_dir))
        journal = DestructiveRestoreJournal(root)
        try:
            persisted = journal.read()
            assert persisted is not None
            assert persisted["status"] == "finalizing"
        finally:
            journal.close()
            root.close()
        cleared.append(selected_restore_id)

    async def validate(
        _settings: object,
        _connection: object,
        expected_inventory: dict[str, object],
    ) -> tuple[object, ...]:
        assert expected_inventory == build_file_reference_inventory(())
        return ()

    monkeypatch.setattr(destructive, "read_database_restore_marker", marker)
    monkeypatch.setattr(destructive, "clear_database_restore_marker", clear)
    monkeypatch.setattr(
        destructive,
        "validate_committed_destructive_restore",
        validate,
    )

    result = await recover_interrupted_destructive_restore(settings, Connection())

    assert result == {"restoreId": restore_id, "outcome": "committed"}
    assert (data_dir / "storage" / "restored.txt").read_text(encoding="utf-8") == "restored"
    assert not old_storage.exists()
    assert not (data_dir / str(value["previousSecret"])).exists()
    assert cleared == [restore_id]
    assert commits == [True]
    assert not Path(str(value["workDir"])).exists()


@pytest.mark.asyncio
async def test_recovery_resumes_finalization_after_marker_clear_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    restore_id = "1" * 32
    data_dir = Path(settings.data_dir)
    value = _journal_value(settings, restore_id)
    value["status"] = "finalizing"
    (data_dir / "storage").mkdir()
    (data_dir / "storage" / "restored.txt").write_text(
        "restored",
        encoding="utf-8",
    )
    (data_dir / ".jwt_secret").write_text("restored-secret", encoding="utf-8")
    old_storage = data_dir / str(value["previousStorage"])
    old_storage.mkdir()
    old_secret = data_dir / str(value["previousSecret"])
    old_secret.write_text("old-secret", encoding="utf-8")
    _write_journal(settings, value)

    async def no_marker(_connection: object) -> None:
        return None

    async def validate(
        _settings: object,
        _connection: object,
        expected_inventory: dict[str, object],
    ) -> tuple[object, ...]:
        assert expected_inventory == build_file_reference_inventory(())
        return ()

    monkeypatch.setattr(destructive, "read_database_restore_marker", no_marker)
    monkeypatch.setattr(
        destructive,
        "validate_committed_destructive_restore",
        validate,
    )

    result = await recover_interrupted_destructive_restore(settings, object())

    assert result == {"restoreId": restore_id, "outcome": "committed"}
    assert not old_storage.exists()
    assert not old_secret.exists()
    assert (data_dir / "storage" / "restored.txt").is_file()


@pytest.mark.asyncio
async def test_recovery_keeps_journal_until_workspace_cleanup_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    restore_id = "2" * 32
    data_dir = Path(settings.data_dir)
    value = _journal_value(settings, restore_id)
    value["status"] = "finalizing"
    (data_dir / "storage").mkdir()
    (data_dir / ".jwt_secret").write_text("restored-secret", encoding="utf-8")
    _write_journal(settings, value)

    async def no_marker(_connection: object) -> None:
        return None

    async def validate(
        _settings: object,
        _connection: object,
        expected_inventory: dict[str, object],
    ) -> tuple[object, ...]:
        assert expected_inventory == build_file_reference_inventory(())
        return ()

    monkeypatch.setattr(destructive, "read_database_restore_marker", no_marker)
    monkeypatch.setattr(
        destructive,
        "validate_committed_destructive_restore",
        validate,
    )

    real_rmtree = destructive.shutil.rmtree

    def remove_then_fail(path: Path) -> None:
        real_rmtree(path)
        raise OSError("synthetic crash after workspace deletion")

    monkeypatch.setattr(destructive.shutil, "rmtree", remove_then_fail)

    with pytest.raises(OSError, match="synthetic crash"):
        await recover_interrupted_destructive_restore(settings, object())

    root = RuntimeDataRoot.open(Path(settings.data_dir))
    journal = DestructiveRestoreJournal(root)
    try:
        assert journal.read() is not None
    finally:
        journal.close()
        root.close()

    monkeypatch.setattr(destructive.shutil, "rmtree", real_rmtree)
    result = await recover_interrupted_destructive_restore(settings, object())

    assert result == {"restoreId": restore_id, "outcome": "committed"}
    root = RuntimeDataRoot.open(Path(settings.data_dir))
    journal = DestructiveRestoreJournal(root)
    try:
        assert journal.read() is None
    finally:
        journal.close()
        root.close()


@pytest.mark.asyncio
async def test_recovery_keeps_previous_files_when_backup_inventory_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    restore_id = "e" * 32
    data_dir = Path(settings.data_dir)
    value = _journal_value(settings, restore_id)
    (data_dir / "storage").mkdir()
    (data_dir / ".jwt_secret").write_text("restored-secret", encoding="utf-8")
    old_storage = data_dir / str(value["previousStorage"])
    old_storage.mkdir()
    (old_storage / "old.txt").write_text("old", encoding="utf-8")
    old_secret = data_dir / str(value["previousSecret"])
    old_secret.write_text("old-secret", encoding="utf-8")
    _write_journal(settings, value)

    async def marker(_connection: object) -> str:
        return restore_id

    async def restored_references(
        _connection: object,
    ) -> tuple[tuple[str, str, str], ...]:
        return (("collection", "record", "missing.txt"),)

    monkeypatch.setattr(destructive, "read_database_restore_marker", marker)
    monkeypatch.setattr(
        destructive,
        "read_canonical_local_file_references",
        restored_references,
    )

    with pytest.raises(
        DestructiveRestoreError,
        match="differ from the backup inventory",
    ):
        await recover_interrupted_destructive_restore(settings, object())

    assert old_storage.is_dir()
    assert old_secret.is_file()
    assert (data_dir / "storage").is_dir()
    assert Path(str(value["workDir"])).is_dir()
    root = RuntimeDataRoot.open(Path(settings.data_dir))
    journal = DestructiveRestoreJournal(root)
    try:
        assert journal.read() is not None
    finally:
        journal.close()
        root.close()


@pytest.mark.asyncio
async def test_recovery_keeps_previous_files_when_active_reference_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    restore_id = "f" * 32
    reference = ("collection", "record", "missing.txt")
    data_dir = Path(settings.data_dir)
    value = _journal_value(settings, restore_id)
    value["localFileReferenceInventory"] = build_file_reference_inventory(
        (reference,)
    )
    (data_dir / "storage").mkdir()
    (data_dir / ".jwt_secret").write_text("restored-secret", encoding="utf-8")
    old_storage = data_dir / str(value["previousStorage"])
    old_storage.mkdir()
    old_secret = data_dir / str(value["previousSecret"])
    old_secret.write_text("old-secret", encoding="utf-8")
    _write_journal(settings, value)

    class Result:
        def scalar_one_or_none(self) -> None:
            return None

    class Connection:
        async def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    async def marker(_connection: object) -> str:
        return restore_id

    async def restored_references(
        _connection: object,
    ) -> tuple[tuple[str, str, str], ...]:
        return (reference,)

    monkeypatch.setattr(destructive, "read_database_restore_marker", marker)
    monkeypatch.setattr(
        destructive,
        "read_canonical_local_file_references",
        restored_references,
    )

    with pytest.raises(
        DestructiveRestoreError,
        match="missing local file",
    ):
        await recover_interrupted_destructive_restore(settings, Connection())

    assert old_storage.is_dir()
    assert old_secret.is_file()
