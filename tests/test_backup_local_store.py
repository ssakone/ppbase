from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ppbase.backup.control as control_module
import ppbase.backup.storage as storage_module
from ppbase.backup import (
    JWT_SECRET_RESOURCE,
    AuthenticatedBackupInspection,
    BackupAlreadyExistsError,
    BackupIdentity,
    BackupIdentityError,
    BackupIntegrityError,
    BackupManifest,
    BackupManifestError,
    BackupNotFoundError,
    BackupResource,
    BackupSetBuilder,
    BackupStateError,
    BackupUnsafeSourceError,
    LocalBackupStore,
    PreparedBackupSet,
    canonical_json_bytes,
    verify_manifest_signature,
)
from ppbase.backup.models import parse_canonical_json
from ppbase.backup.control import ControlPlaneRoot
from ppbase.core.storage_safety import local_storage_id_name


FIXED_TIME = datetime(2026, 7, 16, 12, 34, 56, 123456, tzinfo=UTC)


def _write_dump(
    builder: BackupSetBuilder,
    payload: bytes = b"PGDMP test payload",
) -> None:
    destination = builder.database_dump_path
    assert not destination.exists()
    destination.write_bytes(payload)


def _prepare_minimal_set(
    store: LocalBackupStore,
    backup_id: str,
    *,
    payload: bytes = b"PGDMP test payload",
) -> PreparedBackupSet:
    builder = store.begin_set(backup_id)
    _write_dump(builder, payload)
    return builder.prepare()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_cancelled_seal_gate_never_leaves_a_sealed_or_unsealed_set(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "cancelled-seal")
    gate = storage_module.BackupSealGate()
    assert gate.cancel() is True

    with pytest.raises(BackupStateError, match="sealing was cancelled"):
        store.finalize_set(
            prepared,
            created_at=FIXED_TIME,
            seal_gate=gate,
        )

    assert not (store.sets_dir / "cancelled-seal").exists()
    assert not prepared.path.exists()


def test_seal_fsync_failure_rolls_back_the_published_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "seal-fsync-failure")
    final_path = store.sets_dir / prepared.backup_id
    real_fsync_directory = storage_module._fsync_directory

    def fail_after_seal_is_visible(path: Path) -> None:
        selected = Path(path)
        if selected == final_path and (final_path / "SEALED").exists():
            raise OSError("synthetic seal fsync failure")
        real_fsync_directory(selected)

    monkeypatch.setattr(
        storage_module,
        "_fsync_directory",
        fail_after_seal_is_visible,
    )

    with pytest.raises(OSError, match="synthetic seal fsync failure"):
        store.finalize_set(
            prepared,
            created_at=FIXED_TIME,
            seal_gate=storage_module.BackupSealGate(),
        )

    assert not final_path.exists()
    assert not prepared.path.exists()
    assert store.list_sets() == []


def test_generated_manifest_over_size_limit_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "manifest-too-large")
    monkeypatch.setattr(storage_module, "_MAX_MANIFEST_BYTES", 1)

    with pytest.raises(BackupStateError, match="manifest exceeds"):
        store.finalize_set(prepared, created_at=FIXED_TIME)

    assert not prepared.path.exists()
    assert not (store.sets_dir / "manifest-too-large").exists()
    assert store.list_sets() == []


def test_finalize_performs_no_fallible_reinspection_after_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "no-post-seal-read")
    real_read_regular_file = storage_module._read_regular_file

    def fail_read_after_seal(path: Path, *, maximum_size: int) -> bytes:
        selected = Path(path)
        if (selected.parent / storage_module.SEALED_FILENAME).exists():
            raise OSError("synthetic post-seal read failure")
        return real_read_regular_file(selected, maximum_size=maximum_size)

    monkeypatch.setattr(
        storage_module,
        "_read_regular_file",
        fail_read_after_seal,
    )

    inspection = store.finalize_set(prepared, created_at=FIXED_TIME)

    assert inspection.manifest.backup_id == "no-post-seal-read"
    assert (inspection.path / storage_module.SEALED_FILENAME).is_file()


def test_finalize_fully_reinspects_published_set_before_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "pre-seal-corruption")
    real_publish_unsealed = store._publish_unsealed

    def publish_then_corrupt(
        selected: PreparedBackupSet,
    ) -> tuple[Path, tuple[int, int]]:
        final_path, identity = real_publish_unsealed(selected)
        signature_path = final_path / storage_module.SIGNATURE_FILENAME
        signature = signature_path.read_bytes()
        signature_path.write_bytes(bytes([signature[0] ^ 1]) + signature[1:])
        os.chmod(signature_path, 0o600)
        return final_path, identity

    monkeypatch.setattr(store, "_publish_unsealed", publish_then_corrupt)

    with pytest.raises(BackupIntegrityError):
        store.finalize_set(prepared, created_at=FIXED_TIME)

    assert not (store.sets_dir / "pre-seal-corruption").exists()
    assert store.list_sets() == []


def test_finalize_identity_guard_refuses_detachment_before_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    control_root = ControlPlaneRoot.open(control)
    identity = BackupIdentity.load_or_create_at(control_root)
    store = LocalBackupStore(
        tmp_path / "backups",
        identity=identity,
        identity_guard=identity.verify_attached,
    )
    prepared = _prepare_minimal_set(store, "identity-detached-before-seal")
    final_path = store.sets_dir / prepared.backup_id
    identity_path = control / "identity"
    detached_identity = tmp_path / "detached-identity"
    real_publish_unsealed = store._publish_unsealed

    def publish_then_detach(
        selected: PreparedBackupSet,
    ) -> tuple[Path, tuple[int, int]]:
        published = real_publish_unsealed(selected)
        identity_path.rename(detached_identity)
        identity_path.mkdir(mode=0o700)
        return published

    monkeypatch.setattr(store, "_publish_unsealed", publish_then_detach)
    try:
        with pytest.raises(BackupIdentityError, match="detached|substituted"):
            store.finalize_set(prepared, created_at=FIXED_TIME)
    finally:
        identity.close()
        control_root.close()

    assert not final_path.exists()
    assert not prepared.path.exists()
    assert not (detached_identity / storage_module.SEALED_FILENAME).exists()


def test_finalize_rechecks_identity_immediately_before_seal_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    control_root = ControlPlaneRoot.open(control)
    identity = BackupIdentity.load_or_create_at(control_root)
    store = LocalBackupStore(
        tmp_path / "backups",
        identity=identity,
        identity_guard=identity.verify_attached,
    )
    prepared = _prepare_minimal_set(store, "identity-race-at-seal")
    final_path = store.sets_dir / prepared.backup_id
    identity_path = control / "identity"
    detached_identity = tmp_path / "detached-identity-at-seal"
    real_fsync_directory = storage_module._fsync_directory
    detached = False

    def detach_during_seal_preparation(path: Path) -> None:
        nonlocal detached
        selected = Path(path)
        if (
            not detached
            and selected == final_path
            and tuple(final_path.glob(".SEALED-*.tmp"))
            and not (final_path / storage_module.SEALED_FILENAME).exists()
        ):
            detached = True
            identity_path.rename(detached_identity)
            identity_path.mkdir(mode=0o700)
        real_fsync_directory(selected)

    monkeypatch.setattr(
        storage_module,
        "_fsync_directory",
        detach_during_seal_preparation,
    )
    try:
        with pytest.raises(BackupIdentityError, match="detached|substituted"):
            store.finalize_set(prepared, created_at=FIXED_TIME)
    finally:
        identity.close()
        control_root.close()

    assert detached is True
    assert not final_path.exists()
    assert not prepared.path.exists()


def test_default_store_refuses_detached_control_identity(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    stale_store = LocalBackupStore(backup_root)
    prepared = _prepare_minimal_set(stale_store, "detached-default-control")
    original_fingerprint = stale_store.identity.fingerprint_sha256
    control = backup_root / "control"
    detached_control = backup_root / "detached-control"
    control.rename(detached_control)
    replacement_store = LocalBackupStore(backup_root)

    try:
        with pytest.raises(BackupIdentityError, match="detached|substituted"):
            stale_store.finalize_set(prepared, created_at=FIXED_TIME)
        replacement_fingerprint = replacement_store.identity.fingerprint_sha256
    finally:
        stale_store.close()
        replacement_store.close()

    assert replacement_fingerprint != original_fingerprint
    assert not (stale_store.sets_dir / prepared.backup_id).exists()
    assert not prepared.path.exists()


def test_default_store_close_is_terminal_and_closes_identity(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    identity = store.identity

    store.close()
    store.close()

    with pytest.raises(BackupStateError, match="closed"):
        store.begin_set("after-close")
    with pytest.raises(BackupStateError, match="closed"):
        store.list_sets()
    with pytest.raises(BackupIdentityError, match="closed"):
        identity.sign_manifest(b"manifest")


def test_signature_write_failure_removes_the_full_partial_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "signature-write-failure")
    real_write_exclusive = storage_module._write_exclusive

    def fail_signature(path: Path, payload: bytes) -> None:
        if Path(path).name == storage_module.SIGNATURE_FILENAME:
            raise OSError("synthetic signature write failure")
        real_write_exclusive(Path(path), payload)

    monkeypatch.setattr(storage_module, "_write_exclusive", fail_signature)

    with pytest.raises(OSError, match="synthetic signature write failure"):
        store.finalize_set(prepared, created_at=FIXED_TIME)

    assert not prepared.path.exists()
    assert not (store.sets_dir / prepared.backup_id).exists()
    assert store.list_sets() == []


def test_mid_publication_failure_removes_partial_and_unsealed_final_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "mid-publication-failure")
    final_path = store.sets_dir / prepared.backup_id
    real_rename = storage_module.os.rename
    publication_renames = 0

    def fail_second_publication_rename(source: Path, destination: Path) -> None:
        nonlocal publication_renames
        if Path(source).parent == prepared.path:
            publication_renames += 1
            if publication_renames == 2:
                raise OSError("synthetic publication rename failure")
        real_rename(source, destination)

    monkeypatch.setattr(
        storage_module.os,
        "rename",
        fail_second_publication_rename,
    )

    with pytest.raises(OSError, match="synthetic publication rename failure"):
        store.finalize_set(prepared, created_at=FIXED_TIME)

    assert not prepared.path.exists()
    assert not final_path.exists()
    assert store.list_sets() == []


def _authenticate_local(
    store: LocalBackupStore,
    backup_id: str,
) -> AuthenticatedBackupInspection:
    return store.authenticate_set(
        backup_id,
        approved_public_key=store.identity.public_key_bytes,
    )


def _prepare_restore_source(
    store: LocalBackupStore,
    tmp_path: Path,
    backup_id: str,
    *,
    include_jwt_secret: bool = True,
) -> AuthenticatedBackupInspection:
    source_storage = tmp_path / f"{backup_id}-source" / "storage"
    source_file = source_storage / "collection" / "record" / "asset.bin"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"anchored asset")
    builder = store.begin_set(backup_id)
    _write_dump(builder, b"PGDMP anchored restore")
    builder.copy_storage(source_storage)
    if include_jwt_secret:
        source_secret = source_storage.parent / ".jwt_secret"
        source_secret.write_bytes(b"preserved anchored secret\n")
        builder.copy_jwt_secret(source_secret)
    store.finalize_set(builder.prepare(), created_at=FIXED_TIME)
    return _authenticate_local(store, backup_id)


def test_canonical_json_is_nfc_sorted_and_rejects_floats() -> None:
    payload = canonical_json_bytes({"z": [1, True, None], "e\u0301": "e\u0301"})

    assert payload == '{"z":[1,true,null],"é":"é"}'.encode()
    assert parse_canonical_json(payload) == {"z": [1, True, None], "é": "é"}

    with pytest.raises(BackupManifestError, match="floating-point"):
        canonical_json_bytes({"value": 1.5})
    with pytest.raises(BackupManifestError, match="canonical"):
        parse_canonical_json(b'{"z":1,"a":2}')
    with pytest.raises(BackupManifestError, match="duplicate"):
        parse_canonical_json(b'{"a":1,"a":2}')
    with pytest.raises(BackupManifestError, match="floating-point"):
        parse_canonical_json(b'{"value":1.0}')
    with pytest.raises(BackupManifestError, match="64-bit"):
        parse_canonical_json(b'{"value":' + (b"9" * 5000) + b"}")


def test_manifest_roundtrip_has_a_strict_schema() -> None:
    manifest = BackupManifest(
        backup_id="backup-001",
        created_at="2026-07-16T12:34:56.123456Z",
        signer_fingerprint_sha256="a" * 64,
        resources=(
            BackupResource(
                path="resources/database.dump",
                size=4,
                sha256="b" * 64,
            ),
        ),
        metadata={
            "jwt_secret": {"mode": "included_resource"},
            "postgresql": {"server_version_num": 160000},
        },
    )

    encoded = manifest.to_bytes()

    assert BackupManifest.from_bytes(encoded) == manifest
    with pytest.raises(TypeError, match="immutable"):
        manifest.metadata["new"] = "value"  # type: ignore[index]
    postgres_metadata = manifest.metadata["postgresql"]
    assert isinstance(postgres_metadata, dict)
    with pytest.raises(TypeError, match="immutable"):
        postgres_metadata["server_version_num"] = 1
    manifest_with_extra = manifest.to_dict()
    manifest_with_extra["extra"] = True
    with pytest.raises(BackupManifestError, match="unknown or missing"):
        BackupManifest.from_bytes(canonical_json_bytes(manifest_with_extra))
    with pytest.raises(BackupManifestError, match="sensitive field"):
        BackupManifest(
            backup_id="backup-secret",
            created_at="2026-07-16T12:34:56.123456Z",
            signer_fingerprint_sha256="a" * 64,
            resources=manifest.resources,
            metadata={"runtime": {"jwt_secret": "must-not-leak"}},
        )
    with pytest.raises(BackupManifestError, match="unsupported v1"):
        BackupManifest(
            backup_id="backup-extra",
            created_at="2026-07-16T12:34:56.123456Z",
            signer_fingerprint_sha256="a" * 64,
            resources=(
                BackupResource(
                    path="resources/arbitrary-secret",
                    size=1,
                    sha256="c" * 64,
                ),
            )
            + manifest.resources,
            metadata={},
        )


def test_identity_is_created_exclusively_with_private_permissions(tmp_path: Path) -> None:
    control_dir = tmp_path / "control"

    identity = BackupIdentity.load_or_create(control_dir)
    same_identity = BackupIdentity.load_or_create(control_dir)
    payload = canonical_json_bytes({"manifest": 1})
    signature = identity.sign_manifest(payload)

    assert _mode(control_dir) == 0o700
    assert _mode(control_dir / "signing-ed25519.key") == 0o600
    assert same_identity.public_key_bytes == identity.public_key_bytes
    assert verify_manifest_signature(
        payload,
        signature,
        identity.public_key_bytes,
    ) == identity.fingerprint_sha256

    os.chmod(control_dir / "signing-ed25519.key", 0o644)
    with pytest.raises(BackupIdentityError, match="0600"):
        BackupIdentity.load_or_create(control_dir)


def test_prepare_then_finalize_creates_one_signed_sealed_set(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    storage_source = tmp_path / "data" / "storage"
    nested = storage_source / "collection" / "record"
    nested.mkdir(parents=True)
    (nested / "document.txt").write_bytes(b"document body")
    jwt_secret = tmp_path / "data" / ".jwt_secret"
    jwt_secret.write_bytes(b"high-entropy-test-secret")

    builder = store.begin_set("backup-001")
    _write_dump(builder)
    builder.copy_storage(storage_source)
    builder.copy_jwt_secret(jwt_secret)
    prepared = builder.prepare()

    assert store.list_sets() == []
    assert not (prepared.path / "manifest.json").exists()
    assert not (prepared.path / "SEALED").exists()

    inspection = store.finalize_set(
        prepared,
        metadata={
            "jwt_mode": "disaster_recovery",
            "postgresql": {"server_version_num": 160000},
        },
        created_at=FIXED_TIME,
    )

    resource_paths = {item.path for item in inspection.manifest.resources}
    assert resource_paths == {
        "resources/database.dump",
        "resources/files/collection/record/document.txt",
        JWT_SECRET_RESOURCE,
    }
    assert inspection.resources_verified is True
    assert inspection.signer_public_key == store.identity.public_key_bytes
    assert inspection.manifest.created_at == "2026-07-16T12:34:56.123456Z"
    assert (inspection.path / "SEALED").read_bytes() == b""
    assert not prepared.path.exists()

    summaries = store.list_sets()
    assert [summary.backup_id for summary in summaries] == ["backup-001"]
    assert summaries[0].integrity_status == "valid"
    assert summaries[0].resource_count == 3
    assert summaries[0].total_size == inspection.manifest.total_size

    public_key = (inspection.path / "signer.pub").read_bytes()
    assert store.inspect_set(
        "backup-001",
        expected_public_key=public_key,
    ).manifest == inspection.manifest
    with pytest.raises(BackupIntegrityError, match="expected key"):
        store.inspect_set("backup-001", expected_public_key=b"x" * 32)

    for root, directories, files in os.walk(inspection.path):
        root_path = Path(root)
        assert _mode(root_path) == 0o700
        for directory in directories:
            assert _mode(root_path / directory) == 0o700
        for filename in files:
            assert _mode(root_path / filename) == 0o600


def test_resource_tampering_is_detected_but_listing_stays_cheap(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "backup-tamper")
    inspection = store.finalize_set(prepared, created_at=FIXED_TIME)
    dump_path = inspection.path / "resources" / "database.dump"

    dump_path.write_bytes(b"tampered dump")

    assert [item.backup_id for item in store.list_sets()] == ["backup-tamper"]
    with pytest.raises(BackupIntegrityError, match="checksum"):
        store.inspect_set("backup-tamper")


def test_listing_isolates_a_corrupt_sealed_set(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    valid = _prepare_minimal_set(store, "valid-set")
    corrupt = _prepare_minimal_set(store, "corrupt-set")
    store.finalize_set(valid, created_at=FIXED_TIME)
    corrupt_inspection = store.finalize_set(corrupt, created_at=FIXED_TIME)
    signature_path = corrupt_inspection.path / "manifest.sig"
    signature = signature_path.read_bytes()
    signature_path.write_bytes(bytes([signature[0] ^ 1]) + signature[1:])

    summaries = {summary.backup_id: summary for summary in store.list_sets()}

    assert summaries["valid-set"].integrity_status == "valid"
    assert summaries["valid-set"].error_code is None
    assert summaries["corrupt-set"].integrity_status == "invalid"
    assert summaries["corrupt-set"].error_code == "integrity_failed"
    assert summaries["corrupt-set"].created_at is None


def test_finalize_revalidates_resources_after_prepare(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "backup-race")
    prepared_dump = prepared.path / "resources" / "database.dump"

    prepared_dump.write_bytes(b"changed after the barrier")

    with pytest.raises(BackupIntegrityError, match="changed"):
        store.finalize_set(prepared, created_at=FIXED_TIME)
    assert store.list_sets() == []
    assert not (prepared.path / "SEALED").exists()


def test_publication_never_overwrites_an_existing_set(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    first = _prepare_minimal_set(store, "same-id", payload=b"first dump")
    second = _prepare_minimal_set(store, "same-id", payload=b"second dump")

    first_inspection = store.finalize_set(first, created_at=FIXED_TIME)
    with pytest.raises(BackupAlreadyExistsError):
        store.finalize_set(second, created_at=FIXED_TIME)

    assert (
        first_inspection.path / "resources" / "database.dump"
    ).read_bytes() == b"first dump"
    assert [item.backup_id for item in store.list_sets()] == ["same-id"]
    with pytest.raises(BackupAlreadyExistsError):
        store.begin_set("same-id")


def test_listing_ignores_partial_and_unsealed_directories(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    builder = store.begin_set("still-partial")
    _write_dump(builder)
    builder.prepare()
    unsealed = store.sets_dir / "published-but-unsealed"
    unsealed.mkdir(mode=0o700)
    os.chmod(unsealed, 0o700)

    assert store.list_sets() == []


@pytest.mark.parametrize("special_kind", ["symlink", "fifo"])
def test_storage_copy_refuses_links_and_special_files(
    tmp_path: Path,
    special_kind: str,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    source = tmp_path / "storage"
    source.mkdir()
    target = source / "unsafe"
    if special_kind == "symlink":
        target.symlink_to(tmp_path / "outside")
    else:
        os.mkfifo(target)
    builder = store.begin_set(f"unsafe-{special_kind}")
    _write_dump(builder)

    with pytest.raises(BackupUnsafeSourceError):
        builder.copy_storage(source)
    assert store.list_sets() == []


def test_storage_copy_refuses_overlapping_source_and_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "data"
    source.mkdir()
    (source / "business-file").write_bytes(b"payload")
    store = LocalBackupStore(source / "backups")
    builder = store.begin_set("overlap")
    _write_dump(builder)

    with pytest.raises(BackupUnsafeSourceError, match="overlap"):
        builder.copy_storage(source)


def test_jwt_secret_copy_refuses_a_symlink(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    actual_secret = tmp_path / "actual-secret"
    actual_secret.write_bytes(b"secret")
    linked_secret = tmp_path / ".jwt_secret"
    linked_secret.symlink_to(actual_secret)
    builder = store.begin_set("jwt-link")
    _write_dump(builder)

    with pytest.raises(BackupUnsafeSourceError):
        builder.copy_jwt_secret(linked_secret)


def test_absent_storage_source_produces_an_empty_files_resource(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    missing_storage = tmp_path / "data" / "storage"
    builder = store.begin_set("empty-storage")
    _write_dump(builder)

    builder.copy_storage(missing_storage)
    inspection = store.finalize_set(builder.prepare(), created_at=FIXED_TIME)

    assert not missing_storage.exists()
    assert [
        resource.path
        for resource in inspection.manifest.resources
        if resource.path.startswith("resources/files/")
    ] == []
    restore_target = tmp_path / "restored-data"
    store.restore_files(_authenticate_local(store, "empty-storage"), restore_target)
    assert (restore_target / "storage").is_dir()
    assert list((restore_target / "storage").iterdir()) == []


def test_restore_files_and_disaster_recovery_secret_use_only_new_targets(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    source_storage = tmp_path / "source-data" / "storage"
    source_file = source_storage / "collection" / "record" / "asset.bin"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"asset")
    source_secret = tmp_path / "source-data" / ".jwt_secret"
    source_secret.write_bytes(b"preserved-jwt-secret")
    builder = store.begin_set("restore-source")
    _write_dump(builder)
    builder.copy_storage(source_storage)
    builder.copy_jwt_secret(source_secret)
    inspection = store.finalize_set(builder.prepare(), created_at=FIXED_TIME)
    authenticated = _authenticate_local(store, "restore-source")

    target = tmp_path / "new-data"
    restored_data_dir = store.restore_files(authenticated, target)
    restored_secret = store.restore_jwt_secret(authenticated, target)

    assert restored_data_dir == target
    assert (target / "storage" / "collection" / "record" / "asset.bin").read_bytes() == b"asset"
    assert restored_secret.read_bytes() == b"preserved-jwt-secret"
    assert _mode(target) == 0o700
    assert _mode(target / "storage") == 0o700
    assert _mode(restored_secret) == 0o600
    with pytest.raises(BackupAlreadyExistsError):
        store.restore_files(authenticated, target)
    with pytest.raises(BackupAlreadyExistsError):
        store.restore_jwt_secret(authenticated, target)


def test_restore_reinspects_resources_instead_of_trusting_stale_inspection(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    source_storage = tmp_path / "storage"
    source_storage.mkdir()
    (source_storage / "asset.txt").write_text("original")
    builder = store.begin_set("stale-inspection")
    _write_dump(builder)
    builder.copy_storage(source_storage)
    inspection = store.finalize_set(builder.prepare(), created_at=FIXED_TIME)
    authenticated = _authenticate_local(store, "stale-inspection")
    backed_up_file = inspection.path / "resources" / "files" / "asset.txt"
    backed_up_file.write_text("tampered")
    target = tmp_path / "must-not-be-created"

    with pytest.raises(BackupIntegrityError, match="checksum"):
        store.restore_files(authenticated, target)
    assert not target.exists()


def test_clone_backup_without_jwt_secret_cannot_restore_one(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "clone-no-secret")
    inspection = store.finalize_set(prepared, created_at=FIXED_TIME)
    authenticated = _authenticate_local(store, "clone-no-secret")
    target = tmp_path / "clone-data"
    store.restore_files(authenticated, target)

    with pytest.raises(BackupNotFoundError, match="no disaster-recovery"):
        store.restore_jwt_secret(authenticated, target)


def test_restore_requires_the_local_or_an_explicitly_approved_signer(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "shared-backups"
    foreign_identity = BackupIdentity.load_or_create(tmp_path / "foreign-control")
    foreign_store = LocalBackupStore(store_root, identity=foreign_identity)
    prepared = _prepare_minimal_set(foreign_store, "foreign-set")
    inspection = foreign_store.finalize_set(prepared, created_at=FIXED_TIME)

    local_identity = BackupIdentity.load_or_create(tmp_path / "local-control")
    local_store = LocalBackupStore(store_root, identity=local_identity)
    rejected_target = tmp_path / "unapproved-restore"
    with pytest.raises(BackupIntegrityError, match="expected key"):
        local_store.authenticate_set(
            "foreign-set",
            approved_public_key=local_identity.public_key_bytes,
        )
    assert not rejected_target.exists()
    with pytest.raises(BackupStateError, match="authenticated"):
        local_store.restore_files(inspection, rejected_target)  # type: ignore[arg-type]

    approved_target = tmp_path / "approved-restore"
    authenticated = local_store.authenticate_set(
        "foreign-set",
        approved_public_key=foreign_identity.public_key_bytes,
    )
    local_store.restore_files(
        authenticated,
        approved_target,
    )
    assert (approved_target / "storage").is_dir()


def test_pinned_database_dump_is_independent_from_canonical_path(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    builder = store.begin_set("pinned-dump")
    original = b"PGDMP\x01\x10authenticated-archive"
    builder.database_dump_path.write_bytes(original)
    builder.copy_storage(tmp_path / "missing-storage")
    prepared = builder.prepare()
    store.finalize_set(
        prepared,
        metadata={"jwt_secret": {"mode": "external_required"}},
        created_at=FIXED_TIME,
    )
    authenticated = _authenticate_local(store, "pinned-dump")
    private_directory = tmp_path / "pinned"
    private_directory.mkdir(mode=0o700)

    pinned = store.pin_database_dump(
        authenticated,
        directory=private_directory,
    )
    try:
        canonical = authenticated.path / "resources" / "database.dump"
        canonical.write_bytes(b"PGDMP\x01\x10substituted")
        os.lseek(pinned.fileno(), 0, os.SEEK_SET)
        assert os.read(pinned.fileno(), len(original) + 1) == original
    finally:
        pinned.close()


def test_new_control_and_restore_directories_fsync_their_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_fsyncs: list[tuple[int, int]] = []
    monkeypatch.setattr(
        control_module,
        "fsync_directory",
        lambda descriptor: identity_fsyncs.append(
            (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
        ),
    )
    control_dir = tmp_path / "new-control-parent" / "identity"
    BackupIdentity.load_or_create(control_dir)
    assert (control_dir.parent.stat().st_dev, control_dir.parent.stat().st_ino) in (
        identity_fsyncs
    )
    assert (
        control_dir.parent.parent.stat().st_dev,
        control_dir.parent.parent.stat().st_ino,
    ) in identity_fsyncs

    monkeypatch.undo()
    store = LocalBackupStore(tmp_path / "backups")
    prepared = _prepare_minimal_set(store, "fsync-restore")
    store.finalize_set(prepared, created_at=FIXED_TIME)
    authenticated = _authenticate_local(store, "fsync-restore")
    storage_fsyncs: list[Path] = []
    monkeypatch.setattr(
        storage_module,
        "_fsync_directory",
        lambda path: storage_fsyncs.append(Path(path)),
    )
    target = tmp_path / "new-staging" / "plan" / "data"
    store.restore_files(authenticated, target)
    assert target.parent in storage_fsyncs
    assert target.parent.parent in storage_fsyncs


def test_anchored_staging_capability_restores_files_jwt_and_pinned_dump(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    authenticated = _prepare_restore_source(
        store,
        tmp_path,
        "anchored-success",
    )
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)

    target = store.open_staging_data_dir(
        staging_root,
        Path("plan-001") / "data",
    )
    with target:
        assert target.restore_files(authenticated) == target.path
        assert target.install_jwt(authenticated) == target.path / ".jwt_secret"
        target.verify_attached()
        assert stat.S_ISDIR(os.fstat(target.fileno()).st_mode)
        assert stat.S_ISDIR(os.fstat(target.parent_fileno()).st_mode)

        temporary = target.temporary_file()
        try:
            temporary.write(b"anonymous")
            temporary.seek(0)
            assert temporary.read() == b"anonymous"
            assert sorted(path.name for path in target.path.parent.iterdir()) == [
                "data"
            ]
        finally:
            temporary.close()

        pinned = store.pin_database_dump(authenticated, directory=target)
        try:
            assert os.read(pinned.fileno(), 64) == b"PGDMP anchored restore"
        finally:
            pinned.close()

    assert (
        staging_root
        / "plan-001"
        / "data"
        / "storage"
        / "collection"
        / "record"
        / "asset.bin"
    ).read_bytes() == b"anchored asset"
    assert (target.path / ".jwt_secret").read_bytes() == (
        b"preserved anchored secret\n"
    )
    with pytest.raises(BackupStateError, match="closed"):
        target.fileno()


def test_anchored_staging_verifies_canonical_file_reference_mapping(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    authenticated = _prepare_restore_source(
        store,
        tmp_path,
        "anchored-reference-check",
        include_jwt_secret=False,
    )
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)
    references = (("collection", "record", "asset.bin"),)

    with store.open_staging_data_dir(
        staging_root,
        Path("plan-reference-check") / "data",
    ) as target:
        target.restore_files(authenticated)
        target.verify_local_file_references(references)
        (target.path / "storage" / "collection" / "record" / "asset.bin").unlink()
        with pytest.raises(BackupIntegrityError, match="missing"):
            target.verify_local_file_references(references)


def test_anchored_reference_validation_rejects_storage_inode_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    authenticated = _prepare_restore_source(
        store,
        tmp_path,
        "anchored-storage-replacement",
        include_jwt_secret=False,
    )
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)

    with store.open_staging_data_dir(
        staging_root,
        Path("plan-storage-replacement") / "data",
    ) as target:
        target.restore_files(authenticated)
        real_verify_file = storage_module._verify_restored_regular_file_at
        replaced = False

        def replace_storage_then_verify(parent_fd: int, name: str) -> None:
            nonlocal replaced
            if not replaced:
                replaced = True
                storage_path = target.path / "storage"
                storage_path.rename(target.path / "detached-storage")
                replacement_record = storage_path / "collection" / "record"
                replacement_record.mkdir(parents=True, mode=0o700)
                os.chmod(storage_path, 0o700)
                os.chmod(storage_path / "collection", 0o700)
                os.chmod(replacement_record, 0o700)
                replacement_file = replacement_record / "asset.bin"
                replacement_file.write_bytes(b"replacement")
                os.chmod(replacement_file, 0o600)
            real_verify_file(parent_fd, name)

        monkeypatch.setattr(
            storage_module,
            "_verify_restored_regular_file_at",
            replace_storage_then_verify,
        )

        with pytest.raises(BackupIntegrityError, match="changed"):
            target.verify_local_file_references(
                (("collection", "record", "asset.bin"),)
            )


@pytest.mark.parametrize("legacy_layout", [False, True])
def test_anchored_reference_verification_matches_custom_and_legacy_id_layouts(
    tmp_path: Path,
    legacy_layout: bool,
) -> None:
    collection_id = "Docs:Mixed"
    record_id = "Rec'42"
    collection_name = (
        collection_id if legacy_layout else local_storage_id_name(collection_id)
    )
    record_name = record_id if legacy_layout else local_storage_id_name(record_id)
    source_storage = tmp_path / "source" / "storage"
    source_file = source_storage / collection_name / record_name / "asset.bin"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"mapped asset")
    store = LocalBackupStore(tmp_path / "backups")
    builder = store.begin_set(f"mapping-{str(legacy_layout).lower()}")
    _write_dump(builder)
    builder.copy_storage(source_storage)
    store.finalize_set(builder.prepare(), created_at=FIXED_TIME)
    authenticated = _authenticate_local(store, builder.backup_id)
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)

    with store.open_staging_data_dir(
        staging_root,
        Path(f"plan-{str(legacy_layout).lower()}") / "data",
    ) as target:
        target.restore_files(authenticated)
        target.verify_local_file_references(
            ((collection_id, record_id, "asset.bin"),)
        )


def test_anchored_staging_clone_secret_is_exclusive_and_never_replaced(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    authenticated = _prepare_restore_source(
        store,
        tmp_path,
        "anchored-clone",
        include_jwt_secret=False,
    )
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)

    with store.open_staging_data_dir(
        staging_root,
        Path("plan-clone") / "data",
    ) as target:
        target.restore_files(authenticated)
        secret_path = target.write_secret(b"first clone secret\n")
        with pytest.raises(BackupAlreadyExistsError, match="already contains"):
            target.write_secret(b"replacement must fail\n")

    assert secret_path.read_bytes() == b"first clone secret\n"
    assert _mode(secret_path) == 0o600


def test_anchored_jwt_install_never_unlinks_an_existing_secret(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    authenticated = _prepare_restore_source(
        store,
        tmp_path,
        "anchored-existing-secret",
    )
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)

    with store.open_staging_data_dir(
        staging_root,
        Path("plan-existing-secret") / "data",
    ) as target:
        target.restore_files(authenticated)
        secret_path = target.write_secret(b"preexisting secret\n")
        with pytest.raises(BackupAlreadyExistsError, match="already contains"):
            target.install_jwt(authenticated)

    assert secret_path.read_bytes() == b"preexisting secret\n"


@pytest.mark.parametrize(
    "relative_target",
    ["", "/absolute/data", "../escape", "plan/../data", "plan/e\u0301"],
)
def test_anchored_staging_rejects_unsafe_relative_components(
    tmp_path: Path,
    relative_target: str,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)

    with pytest.raises(BackupStateError):
        store.open_staging_data_dir(staging_root, relative_target)
    assert list(staging_root.iterdir()) == []


def test_anchored_staging_rejects_root_or_target_parent_symlinks(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    actual_root = tmp_path / "actual-staging"
    actual_root.mkdir(mode=0o700)
    os.chmod(actual_root, 0o700)
    linked_root = tmp_path / "linked-staging"
    linked_root.symlink_to(actual_root, target_is_directory=True)

    with pytest.raises(BackupStateError, match="0700"):
        store.open_staging_data_dir(linked_root, Path("plan") / "data")

    permissive_root = tmp_path / "permissive-staging"
    permissive_root.mkdir(mode=0o755)
    os.chmod(permissive_root, 0o755)
    with pytest.raises(BackupStateError, match="0700"):
        store.open_staging_data_dir(
            permissive_root,
            Path("plan") / "data",
        )

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    os.chmod(outside, 0o700)
    (actual_root / "plan").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BackupAlreadyExistsError, match="already exists"):
        store.open_staging_data_dir(actual_root, Path("plan") / "data")
    assert list(outside.iterdir()) == []


def test_anchored_staging_detects_parent_symlink_creation_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    os.chmod(outside, 0o700)
    detached = staging_root / "detached-plan"
    real_mkdir = os.mkdir

    def racing_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == "data" and dir_fd is not None:
            (staging_root / "plan").rename(detached)
            (staging_root / "plan").symlink_to(
                outside,
                target_is_directory=True,
            )
        real_mkdir(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(storage_module.os, "mkdir", racing_mkdir)

    with pytest.raises(BackupStateError, match="renamed or substituted"):
        store.open_staging_data_dir(staging_root, Path("plan") / "data")
    assert not (outside / "data").exists()
    assert (detached / "data").is_dir()


def test_anchored_restore_race_never_writes_through_replaced_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    authenticated = _prepare_restore_source(
        store,
        tmp_path,
        "anchored-race",
    )
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    os.chmod(outside, 0o700)
    target = store.open_staging_data_dir(
        staging_root,
        Path("plan-race") / "data",
    )
    detached = staging_root / "detached-plan"
    real_copy_directory_fd = storage_module._copy_directory_fd
    raced = False

    def racing_copy_directory_fd(
        source_fd: int,
        destination_fd: int,
        *,
        source_display: Path,
    ) -> None:
        nonlocal raced
        real_copy_directory_fd(
            source_fd,
            destination_fd,
            source_display=source_display,
        )
        if not raced:
            raced = True
            (staging_root / "plan-race").rename(detached)
            (staging_root / "plan-race").symlink_to(
                outside,
                target_is_directory=True,
            )

    monkeypatch.setattr(
        storage_module,
        "_copy_directory_fd",
        racing_copy_directory_fd,
    )
    try:
        with pytest.raises(BackupStateError, match="renamed or substituted"):
            target.restore_files(authenticated)
    finally:
        target.close()

    assert list(outside.iterdir()) == []
    assert (
        detached
        / "data"
        / "storage"
        / "collection"
        / "record"
        / "asset.bin"
    ).read_bytes() == b"anchored asset"
