from __future__ import annotations

import asyncio
import io
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ppbase.backup.operations import BackupInUseError
from ppbase.backup.service import BackupServiceError, NativeBackupService
from ppbase.backup.storage import BackupDeletionUncertainError
from ppbase.config import Settings


FIXED_TIME = datetime(2026, 7, 18, 12, 0, 0, 123456, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=str(tmp_path / "active-data"),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        backup_max_upload_bytes=8 * 1024 * 1024,
        backup_max_uncompressed_bytes=16 * 1024 * 1024,
        backup_max_resource_bytes=8 * 1024 * 1024,
        backup_max_archive_entries=128,
        backup_max_compression_ratio=500,
        backup_transport_chunk_size=13,
    )


def _seed_backup(
    service: NativeBackupService,
    backup_id: str = "service-backup",
    *,
    transport_filename: str | None = None,
):
    builder = service.store.begin_set(backup_id)
    builder.database_dump_path.write_bytes(b"PGDMP service transport")
    metadata = {"app_name": "Service Test"}
    if transport_filename is not None:
        metadata["transport"] = {"filename": transport_filename}
    return service.store.finalize_set(
        builder.prepare(),
        metadata=metadata,
        created_at=FIXED_TIME,
        identity_guard=service._require_control_identity_attached,
    )


@pytest.mark.asyncio
async def test_service_download_delete_upload_round_trip_uses_local_identity(
    tmp_path: Path,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    try:
        requested_filename = "service-requested-name.zip"
        original = _seed_backup(
            service,
            transport_filename=requested_filename,
        )

        pinned = await service.materialize_local_backup_zip(
            original.manifest.backup_id
        )
        transport_size = pinned.size
        assert pinned.filename == requested_filename
        payload = b"".join(pinned.iter_bytes(17))
        assert payload.startswith(b"PK")

        await service.delete_local_backup(original.manifest.backup_id)
        assert await service.list_local_backups() == []

        with service.mutation_operation() as lease:
            uploaded = await service.upload_local_backup(
                io.BytesIO(payload),
                operation_lease=lease,
            )

        assert uploaded["key"] == original.manifest.backup_id
        assert uploaded["trustStatus"] == "trusted_local"
        assert uploaded["filename"] == requested_filename
        assert uploaded["size"] == transport_size
        listed = await service.list_local_backups()
        assert listed[0]["key"] == original.manifest.backup_id
        assert listed[0]["modified"] == original.manifest.created_at
        assert listed[0]["size"] == transport_size
        assert listed[0]["totalSize"] == original.manifest.total_size
        assert listed[0]["filename"] == requested_filename
    finally:
        service.close()


@pytest.mark.asyncio
async def test_service_reports_uncertain_delete_as_manual_recovery_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]

    def fail_delete(*_args, **_kwargs) -> None:
        raise BackupDeletionUncertainError("synthetic uncertain delete")

    monkeypatch.setattr(service.store, "delete_set", fail_delete)
    try:
        with pytest.raises(BackupServiceError) as failure:
            await service.delete_local_backup("uncertain-delete")

        assert failure.value.status_code == 500
        assert failure.value.code == "backup_delete_outcome_uncertain"
        assert failure.value.message == (
            "The backup deletion outcome is uncertain and requires manual "
            "recovery."
        )
        assert "synthetic" not in failure.value.message
    finally:
        service.close()


@pytest.mark.asyncio
async def test_service_rejects_concurrent_mutation_and_delete_of_in_use_backup(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    second = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    try:
        inspection = _seed_backup(first, "in-use-backup")
        with first.mutation_operation():
            with pytest.raises(BackupServiceError) as busy:
                with second.mutation_operation():
                    pass
        assert busy.value.code == "backup_operation_in_progress"

        with first.operations.backup_shared(inspection.manifest.backup_id):
            with pytest.raises(BackupServiceError) as in_use:
                await second.delete_local_backup(inspection.manifest.backup_id)
        assert in_use.value.code == "backup_in_use"
        assert (inspection.path / "SEALED").is_file()
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_service_rejects_concurrent_zip_materialization_across_workers(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    second = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    try:
        inspection = _seed_backup(first, "materialization-single-flight")
        with first.operations.backup_materialization_exclusive(
            inspection.manifest.backup_id
        ):
            with pytest.raises(BackupServiceError) as busy:
                await second.materialize_local_backup_zip(
                    inspection.manifest.backup_id
                )
        assert busy.value.code == "backup_download_in_progress"
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_service_holds_single_flight_until_pinned_stream_is_closed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    second = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    pinned = None
    competing = None
    try:
        inspection = _seed_backup(first, "pinned-single-flight")
        pinned = await first.materialize_local_backup_zip(
            inspection.manifest.backup_id
        )
        # The request-scoped service is closed before StreamingResponse starts.
        first.close()

        with pytest.raises(BackupServiceError) as busy:
            competing = await second.materialize_local_backup_zip(
                inspection.manifest.backup_id
            )
        assert busy.value.code == "backup_download_in_progress"

        stream = pinned.iter_bytes(1)
        assert next(stream)
        stream.close()  # StreamingResponse cancellation closes its iterator.
        assert pinned.closed is True

        competing = await second.materialize_local_backup_zip(
            inspection.manifest.backup_id
        )
    finally:
        if competing is not None:
            competing.close()
        if pinned is not None:
            pinned.close()
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_cancelled_inspection_keeps_shared_lease_until_worker_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    first = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    second = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    started = threading.Event()
    release = threading.Event()
    try:
        inspection = _seed_backup(first, "cancelled-inspection")
        real_inspect = first.store.inspect_set

        def blocked_inspect(*args, **kwargs):
            started.set()
            assert release.wait(timeout=5)
            return real_inspect(*args, **kwargs)

        monkeypatch.setattr(first.store, "inspect_set", blocked_inspect)
        task = asyncio.create_task(
            first.inspect_local_backup(inspection.manifest.backup_id)
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)

        assert task.done() is False
        with pytest.raises(BackupInUseError):
            with second.operations.backup_exclusive(
                inspection.manifest.backup_id
            ):
                pass

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with second.operations.backup_exclusive(inspection.manifest.backup_id):
            pass
    finally:
        release.set()
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_cancelled_authentication_keeps_plan_lease_until_worker_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    first = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    second = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    started = threading.Event()
    release = threading.Event()
    try:
        inspection = _seed_backup(first, "cancelled-authentication")
        real_authenticate = first.store.authenticate_set

        def blocked_authenticate(*args, **kwargs):
            started.set()
            assert release.wait(timeout=5)
            return real_authenticate(*args, **kwargs)

        monkeypatch.setattr(
            first.store,
            "authenticate_set",
            blocked_authenticate,
        )
        task = asyncio.create_task(
            first.create_staging_plan(
                inspection.manifest.backup_id,
                jwt_secret_mode="clone",
                actor_id="admin_1",
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)

        assert task.done() is False
        with pytest.raises(BackupInUseError):
            with second.operations.backup_exclusive(
                inspection.manifest.backup_id
            ):
                pass

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with second.operations.backup_exclusive(inspection.manifest.backup_id):
            pass
    finally:
        release.set()
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_delete_cancellation_before_commit_preserves_backup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    started = threading.Event()
    release = threading.Event()
    try:
        inspection = _seed_backup(service, "delete-cancel-before-commit")

        def blocked_reconcile() -> None:
            started.set()
            assert release.wait(timeout=5)

        monkeypatch.setattr(
            service.store,
            "_reconcile_deleting_sets",
            blocked_reconcile,
        )
        task = asyncio.create_task(
            service.delete_local_backup(inspection.manifest.backup_id)
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)

        assert task.done() is False
        assert (inspection.path / "SEALED").is_file()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (inspection.path / "SEALED").is_file()
        assert not list(service.store.sets_dir.glob(".deleting-*"))
    finally:
        release.set()
        service.close()


@pytest.mark.asyncio
async def test_delete_commit_wins_cancellation_during_tombstone_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    try:
        inspection = _seed_backup(service, "delete-cancel-after-commit")
        real_remove = service.store._remove_owned_directory_at

        def blocked_remove(
            parent_fd: int,
            name: str,
            expected_identity: tuple[int, int],
            *,
            attachment_guard: Callable[[], None],
        ) -> None:
            cleanup_started.set()
            assert release_cleanup.wait(timeout=5)
            real_remove(
                parent_fd,
                name,
                expected_identity,
                attachment_guard=attachment_guard,
            )

        monkeypatch.setattr(
            service.store,
            "_remove_owned_directory_at",
            blocked_remove,
        )
        task = asyncio.create_task(
            service.delete_local_backup(inspection.manifest.backup_id)
        )
        while not cleanup_started.is_set():
            await asyncio.sleep(0)
        assert not inspection.path.exists()

        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False

        release_cleanup.set()
        await task
        assert not inspection.path.exists()
        assert not list(service.store.sets_dir.glob(".deleting-*"))
    finally:
        release_cleanup.set()
        service.close()
