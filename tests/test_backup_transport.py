from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ppbase.backup.storage import DATA_COPY_RESOURCE, LocalBackupStore
from ppbase.backup.transport import (
    BackupTransportError,
    BackupTransportLimits,
    PinnedBackupZip,
    materialize_backup_zip,
    prepare_backup_zip_import,
)


FIXED_TIME = datetime(2026, 7, 18, 10, 11, 12, 345678, tzinfo=UTC)


def _limits(**overrides: int) -> BackupTransportLimits:
    values = {
        "max_upload_bytes": 8 * 1024 * 1024,
        "max_uncompressed_bytes": 16 * 1024 * 1024,
        "max_resource_bytes": 8 * 1024 * 1024,
        "max_entries": 128,
        "max_central_directory_bytes": 1024 * 1024,
        "max_compression_ratio": 500,
        "chunk_size": 7,
    }
    values.update(overrides)
    return BackupTransportLimits(**values)


def _create_backup(
    tmp_path: Path,
    *,
    backup_id: str = "transport-source",
    copy_payload: bytes = b"native copy payload",
) -> tuple[LocalBackupStore, object]:
    store = LocalBackupStore(tmp_path / f"{backup_id}-workspace")
    storage = tmp_path / f"{backup_id}-data" / "storage"
    business_file = storage / "collection" / "record" / "document.txt"
    business_file.parent.mkdir(parents=True)
    business_file.write_bytes(b"business file payload")
    secret = storage.parent / ".jwt_secret"
    secret.write_bytes(b"D" * 64)

    builder = store.begin_set(backup_id)
    builder.create_database_directory()
    builder.database_schema_path.write_bytes(
        b'{"tables": [], "views": [], "indexes": []}'
    )
    builder.database_copy_path.write_bytes(copy_payload)
    builder.copy_storage(storage)
    builder.copy_jwt_secret(secret)
    inspection = store.finalize_set(
        builder.prepare(),
        metadata={"app_name": "My PPBase App", "ppbase_version": "test"},
        created_at=FIXED_TIME,
    )
    return store, inspection


def _materialized_bytes(store: LocalBackupStore, backup_id: str) -> bytes:
    pinned = materialize_backup_zip(store, backup_id, chunk_size=11)
    try:
        return b"".join(pinned.iter_bytes(13))
    finally:
        pinned.close()


def test_pinned_zip_cleanup_error_does_not_replace_stream_outcome() -> None:
    class FailingCloseHandle(io.BytesIO):
        def close(self) -> None:
            if self.closed:
                return
            super().close()
            raise OSError("synthetic temporary ZIP close failure")

    pinned = PinnedBackupZip(
        filename="backup.zip",
        size=3,
        _handle=FailingCloseHandle(b"ZIP"),
    )

    assert b"".join(pinned.iter_bytes(2)) == b"ZIP"
    pinned.close()
    assert pinned.closed is True


def test_native_zip_contains_backup_json_and_deflated_resources(tmp_path: Path) -> None:
    store, inspection = _create_backup(
        tmp_path,
        copy_payload=b"compressible" * 10_000,
    )
    payload = _materialized_bytes(store, inspection.manifest.backup_id)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        assert names[0] == "backup.json"
        assert "manifest.json" not in names
        assert "manifest.sig" not in names
        assert "signer.pub" not in names
        assert DATA_COPY_RESOURCE in names
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in archive.infolist())
        manifest = archive.read("backup.json")
        assert manifest == inspection.manifest.to_bytes()

    assert len(payload) < inspection.manifest.total_size


def test_native_zip_round_trip_preserves_backup_json_and_resources(tmp_path: Path) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _materialized_bytes(source, inspection.manifest.backup_id)
    destination = LocalBackupStore(tmp_path / "destination")

    prepared = prepare_backup_zip_import(
        destination,
        io.BytesIO(payload),
        limits=_limits(),
    )
    imported = destination.finalize_imported_set(
        prepared.prepared,
        manifest_bytes=prepared.manifest_bytes,
    )

    assert imported.manifest == inspection.manifest
    assert imported.resources_verified is True
    assert (
        imported.path / DATA_COPY_RESOURCE
    ).read_bytes() == b"native copy payload"


def test_resource_tampering_is_rejected_and_partial_is_removed(tmp_path: Path) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _materialized_bytes(source, inspection.manifest.backup_id)
    rewritten = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive, zipfile.ZipFile(
        rewritten,
        "w",
        zipfile.ZIP_DEFLATED,
        compresslevel=1,
    ) as output:
        for info in archive.infolist():
            member = archive.read(info.filename)
            if info.filename == DATA_COPY_RESOURCE:
                member += b"tampered"
            output.writestr(info.filename, member)

    destination = LocalBackupStore(tmp_path / "tampered-destination")
    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            destination,
            io.BytesIO(rewritten.getvalue()),
            limits=_limits(),
        )
    assert error.value.code == "backup_resource_size_mismatch"
    assert list(destination.sets_dir.iterdir()) == []


def test_extra_or_path_traversal_members_are_rejected(tmp_path: Path) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _materialized_bytes(source, inspection.manifest.backup_id)

    for extra_name in ("extra.txt", "../escape.txt", "nested\\escape.txt"):
        rewritten = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive, zipfile.ZipFile(
            rewritten,
            "w",
            zipfile.ZIP_DEFLATED,
            compresslevel=1,
        ) as output:
            for info in archive.infolist():
                output.writestr(info.filename, archive.read(info.filename))
            output.writestr(extra_name, b"x")

        destination = LocalBackupStore(tmp_path / f"destination-{len(extra_name)}-{extra_name[0]}")
        with pytest.raises(BackupTransportError):
            prepare_backup_zip_import(
                destination,
                io.BytesIO(rewritten.getvalue()),
                limits=_limits(),
            )


def test_limits_are_enforced_before_extraction(tmp_path: Path) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _materialized_bytes(source, inspection.manifest.backup_id)
    destination = LocalBackupStore(tmp_path / "limited-destination")

    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            destination,
            io.BytesIO(payload),
            limits=_limits(max_upload_bytes=len(payload) - 1),
        )
    assert error.value.code == "backup_upload_too_large"


def test_pocketbase_sqlite_zip_is_identified_explicitly(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.db", b"sqlite")
    destination = LocalBackupStore(tmp_path / "destination")

    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            destination,
            io.BytesIO(payload.getvalue()),
            limits=_limits(),
        )
    assert error.value.code == "pocketbase_backup_unsupported"
