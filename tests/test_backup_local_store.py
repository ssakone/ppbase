from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ppbase.backup import (
    JWT_SECRET_RESOURCE,
    BackupAlreadyExistsError,
    BackupIntegrityError,
    BackupManifest,
    BackupManifestError,
    BackupNotFoundError,
    BackupResource,
    BackupStateError,
    BackupUnsafeSourceError,
    LocalBackupStore,
    canonical_json_bytes,
)
from ppbase.backup.models import parse_canonical_json
from ppbase.backup.storage import DATA_COPY_RESOURCE, SCHEMA_JSON_RESOURCE


FIXED_TIME = datetime(2026, 7, 16, 12, 34, 56, 123456, tzinfo=UTC)
SCHEMA_PAYLOAD = b'{"tables": [], "views": [], "indexes": []}'


def _prepare_set(
    store: LocalBackupStore,
    backup_id: str,
    *,
    copy_payload: bytes = b"native copy payload",
    include_storage: bool = False,
    include_secret: bool = False,
):
    builder = store.begin_set(backup_id)
    builder.create_database_directory()
    builder.database_schema_path.write_bytes(SCHEMA_PAYLOAD)
    builder.database_copy_path.write_bytes(copy_payload)
    if include_storage:
        storage = store.root.parent / f"{backup_id}-data" / "storage"
        file_path = storage / "collection" / "record" / "document.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b"document")
        builder.copy_storage(storage)
    if include_secret:
        builder.write_jwt_secret("S" * 64)
    return builder, builder.prepare()


def _finalize(
    store: LocalBackupStore,
    backup_id: str = "backup-one",
    *,
    include_storage: bool = False,
    include_secret: bool = False,
):
    builder, prepared = _prepare_set(
        store,
        backup_id,
        include_storage=include_storage,
        include_secret=include_secret,
    )
    inspection = store.finalize_set(
        prepared,
        metadata={"app_name": "PPBase", "ppbase_version": "test"},
        created_at=FIXED_TIME,
    )
    return builder, inspection


def test_canonical_json_is_nfc_sorted_and_rejects_floats() -> None:
    payload = canonical_json_bytes({"z": "é", "a": [True, None, 1]})
    assert payload == b'{"a":[true,null,1],"z":"\xc3\xa9"}'
    assert parse_canonical_json(payload) == {"a": [True, None, 1], "z": "é"}
    with pytest.raises(BackupManifestError):
        canonical_json_bytes({"value": 1.5})


def test_backup_json_roundtrip_has_no_signature_or_provenance_fields() -> None:
    manifest = BackupManifest(
        backup_id="manifest-roundtrip",
        created_at="2026-07-16T12:34:56.123456Z",
        resources=(
            BackupResource(
                path=DATA_COPY_RESOURCE,
                size=1,
                sha256="a" * 64,
            ),
            BackupResource(
                path=SCHEMA_JSON_RESOURCE,
                size=1,
                sha256="b" * 64,
            ),
        ),
        metadata={"ppbase_version": "test"},
    )

    decoded = parse_canonical_json(manifest.to_bytes())
    assert set(decoded) == {
        "backup_id",
        "created_at",
        "format",
        "format_version",
        "metadata",
        "resources",
    }
    assert "signing" not in decoded
    assert BackupManifest.from_bytes(manifest.to_bytes()) == manifest


def test_manifest_rejects_unknown_fields() -> None:
    payload = {
        "backup_id": "manifest-roundtrip",
        "created_at": "2026-07-16T12:34:56.123456Z",
        "format": "ppbase-native",
        "format_version": 2,
        "metadata": {},
        "resources": [
            {"path": DATA_COPY_RESOURCE, "size": 0, "sha256": "a" * 64},
            {"path": SCHEMA_JSON_RESOURCE, "size": 0, "sha256": "b" * 64},
        ],
        "signing": {},
    }
    with pytest.raises(BackupManifestError, match="unknown or missing"):
        BackupManifest.from_bytes(canonical_json_bytes(payload))


def test_finalize_creates_one_checksummed_workspace(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "workspace")
    _, inspection = _finalize(
        store,
        include_storage=True,
        include_secret=True,
    )

    assert (inspection.path / "backup.json").read_bytes() == inspection.manifest.to_bytes()
    assert not (inspection.path / "manifest.sig").exists()
    assert not (inspection.path / "signer.pub").exists()
    assert inspection.resources_verified is True
    assert {resource.path for resource in inspection.manifest.resources} >= {
        DATA_COPY_RESOURCE,
        SCHEMA_JSON_RESOURCE,
        JWT_SECRET_RESOURCE,
    }


def test_resource_tampering_is_detected(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "workspace")
    _, inspection = _finalize(store)
    (inspection.path / DATA_COPY_RESOURCE).write_bytes(b"tampered")

    with pytest.raises(BackupIntegrityError):
        store.inspect_set(inspection.manifest.backup_id, verify_resources=True)


def test_finalize_revalidates_resources_after_prepare(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "workspace")
    _, prepared = _prepare_set(store, "changed-after-prepare")
    (prepared.path / DATA_COPY_RESOURCE).write_bytes(b"changed")

    with pytest.raises(BackupIntegrityError):
        store.finalize_set(prepared, created_at=FIXED_TIME)
    assert not prepared.path.exists()


def test_publication_never_overwrites_an_existing_set(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "workspace")
    _finalize(store, "duplicate")
    with pytest.raises(BackupAlreadyExistsError):
        store.begin_set("duplicate")


def test_imported_set_preserves_exact_backup_json(tmp_path: Path) -> None:
    source = LocalBackupStore(tmp_path / "source")
    _, source_inspection = _finalize(source, "imported")
    destination = LocalBackupStore(tmp_path / "destination")
    builder = destination.begin_set("imported")
    for resource in source_inspection.manifest.resources:
        builder.write_imported_resource(
            resource,
            io.BytesIO((source_inspection.path / resource.path).read_bytes()),
        )
    prepared = builder.prepare()
    imported = destination.finalize_imported_set(
        prepared,
        manifest_bytes=source_inspection.manifest.to_bytes(),
    )

    assert imported.manifest == source_inspection.manifest
    assert (imported.path / "backup.json").read_bytes() == (
        source_inspection.path / "backup.json"
    ).read_bytes()


def test_storage_copy_refuses_symlinks(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "workspace")
    storage = tmp_path / "data" / "storage"
    storage.mkdir(parents=True)
    target = tmp_path / "outside.txt"
    target.write_text("outside")
    (storage / "link.txt").symlink_to(target)
    builder = store.begin_set("unsafe-storage")
    builder.create_database_directory()
    builder.database_schema_path.write_bytes(SCHEMA_PAYLOAD)
    builder.database_copy_path.write_bytes(b"copy")

    with pytest.raises(BackupUnsafeSourceError):
        builder.copy_storage(storage)
    builder.abort()


def test_restore_requires_verified_capability_from_same_store(tmp_path: Path) -> None:
    source = LocalBackupStore(tmp_path / "source")
    _, inspection = _finalize(
        source,
        "restore-files",
        include_storage=True,
        include_secret=True,
    )
    verified = source.verify_set(inspection.manifest.backup_id)
    target = tmp_path / "restored"
    source.restore_files(verified, target)
    source.restore_jwt_secret(verified, target)
    assert next((target / "storage").rglob("document.txt")).read_bytes() == b"document"
    assert (target / ".jwt_secret").read_text().strip() == "S" * 64

    other = LocalBackupStore(tmp_path / "other")
    with pytest.raises(BackupStateError, match="another store"):
        other.restore_files(verified, tmp_path / "other-target")


def test_delete_removes_set_and_missing_delete_is_explicit(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "workspace")
    _, inspection = _finalize(store, "delete-me")
    store.delete_set(inspection.manifest.backup_id)
    assert store.list_sets() == []
    with pytest.raises(BackupNotFoundError):
        store.delete_set(inspection.manifest.backup_id)
