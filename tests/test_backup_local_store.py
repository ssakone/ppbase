from __future__ import annotations

import io
import hashlib
import os
import stat
from collections.abc import Callable
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
from ppbase.backup.storage import DATA_COPY_RESOURCE, SCHEMA_JSON_RESOURCE
from ppbase.core.storage_safety import local_storage_id_name


FIXED_TIME = datetime(2026, 7, 16, 12, 34, 56, 123456, tzinfo=UTC)

_SCHEMA_PAYLOAD = b'{"tables": [], "views": [], "indexes": []}'


def _write_dump(
    builder: BackupSetBuilder,
    payload: bytes = b"PGDMP test payload",
) -> None:
    builder.create_database_directory()
    schema = builder.database_schema_path
    data = builder.database_copy_path
    assert not schema.exists()
    assert not data.exists()
    schema.write_bytes(_SCHEMA_PAYLOAD)
    data.write_bytes(payload)


def _prepare_minimal_set(
    store: LocalBackupStore,
    backup_id: str,
    *,
    payload: bytes = b"PGDMP test payload",
) -> PreparedBackupSet:
    builder = store.begin_set(backup_id)
    _write_dump(builder, payload)
    return builder.prepare()


def _import_database_resources(
    builder: BackupSetBuilder,
    source_inspection: AuthenticatedBackupInspection,
    payload: bytes,
    *,
    chunk_size: int | None = None,
) -> None:
    """Copy every signed database resource from a source into ``builder``."""
    contents = {SCHEMA_JSON_RESOURCE: _SCHEMA_PAYLOAD, DATA_COPY_RESOURCE: payload}
    for resource in source_inspection.manifest.resources:
        kwargs = {} if chunk_size is None else {"chunk_size": chunk_size}
        builder.write_imported_resource(
            resource,
            io.BytesIO(contents[resource.path]),
            **kwargs,
        )


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
                path=DATA_COPY_RESOURCE,
                size=4,
                sha256="b" * 64,
            ),
            BackupResource(
                path=SCHEMA_JSON_RESOURCE,
                size=4,
                sha256="d" * 64,
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
    with pytest.raises(BackupManifestError, match="unsupported backup resource"):
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
        SCHEMA_JSON_RESOURCE,
        DATA_COPY_RESOURCE,
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
    assert summaries[0].resource_count == 4
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
    dump_path = inspection.path / DATA_COPY_RESOURCE

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
    prepared_dump = prepared.path / DATA_COPY_RESOURCE

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
        first_inspection.path / DATA_COPY_RESOURCE
    ).read_bytes() == b"first dump"
    assert [item.backup_id for item in store.list_sets()] == ["same-id"]
    with pytest.raises(BackupAlreadyExistsError):
        store.begin_set("same-id")


def test_imported_set_preserves_the_exact_signed_envelope(tmp_path: Path) -> None:
    identity = BackupIdentity.load_or_create(tmp_path / "control")
    source = LocalBackupStore(tmp_path / "source", identity=identity)
    source_builder = source.begin_set("transport-roundtrip")
    _write_dump(source_builder, b"PGDMP imported payload")
    source_inspection = source.finalize_set(
        source_builder.prepare(),
        metadata={"app_name": "Transport Tests"},
        created_at=FIXED_TIME,
    )
    manifest_bytes = (source_inspection.path / "manifest.json").read_bytes()
    signature = (source_inspection.path / "manifest.sig").read_bytes()
    signer_public_key = (source_inspection.path / "signer.pub").read_bytes()

    target = LocalBackupStore(tmp_path / "target", identity=identity)
    target_builder = target.begin_set("transport-roundtrip")
    _import_database_resources(
        target_builder,
        source_inspection,
        b"PGDMP imported payload",
        chunk_size=3,
    )
    imported = target.finalize_imported_set(
        target_builder.prepare(),
        manifest_bytes=manifest_bytes,
        signature=signature,
        signer_public_key=signer_public_key,
        expected_public_key=identity.public_key_bytes,
    )

    assert imported.manifest == source_inspection.manifest
    assert (imported.path / "manifest.json").read_bytes() == manifest_bytes
    assert (imported.path / "manifest.sig").read_bytes() == signature
    assert (imported.path / "signer.pub").read_bytes() == signer_public_key
    assert (imported.path / "SEALED").is_file()


def test_imported_set_never_re_signs_an_unapproved_envelope(tmp_path: Path) -> None:
    foreign_identity = BackupIdentity.load_or_create(tmp_path / "foreign-control")
    foreign = LocalBackupStore(tmp_path / "foreign", identity=foreign_identity)
    foreign_builder = foreign.begin_set("foreign-transport")
    _write_dump(foreign_builder, b"PGDMP foreign payload")
    foreign_inspection = foreign.finalize_set(
        foreign_builder.prepare(),
        created_at=FIXED_TIME,
    )

    local = LocalBackupStore(tmp_path / "local")
    local_builder = local.begin_set("foreign-transport")
    _import_database_resources(
        local_builder,
        foreign_inspection,
        b"PGDMP foreign payload",
    )

    with pytest.raises(BackupIntegrityError, match="expected key"):
        local.finalize_imported_set(
            local_builder.prepare(),
            manifest_bytes=(foreign_inspection.path / "manifest.json").read_bytes(),
            signature=(foreign_inspection.path / "manifest.sig").read_bytes(),
            signer_public_key=(foreign_inspection.path / "signer.pub").read_bytes(),
            expected_public_key=local.identity.public_key_bytes,
        )

    assert not (local.sets_dir / "foreign-transport").exists()
    assert local.list_sets() == []


def test_delete_commit_is_not_ambiguous_when_tombstone_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    store.finalize_set(
        _prepare_minimal_set(store, "delete-cleanup"),
        created_at=FIXED_TIME,
    )
    real_remove = store._remove_owned_directory_at
    cleanup_attempts = 0

    def fail_once(
        parent_fd: int,
        name: str,
        expected_identity: tuple[int, int],
        *,
        attachment_guard: Callable[[], None],
    ) -> None:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise OSError("synthetic tombstone cleanup failure")
        real_remove(
            parent_fd,
            name,
            expected_identity,
            attachment_guard=attachment_guard,
        )

    monkeypatch.setattr(store, "_remove_owned_directory_at", fail_once)

    store.delete_set("delete-cleanup")

    assert not (store.sets_dir / "delete-cleanup").exists()
    tombstones = list(store.sets_dir.glob(".deleting-delete-cleanup-*"))
    assert len(tombstones) == 1
    assert store.list_sets() == []
    assert not list(store.sets_dir.glob(".deleting-delete-cleanup-*"))


@pytest.mark.parametrize("operation", ["list", "delete"])
def test_deletion_reconciliation_never_follows_a_substituted_sets_directory(
    tmp_path: Path,
    operation: str,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    detached_sets = tmp_path / "detached-sets"
    store.sets_dir.rename(detached_sets)

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    external_tombstone = outside / ".deleting-external"
    external_tombstone.mkdir(mode=0o700)
    sentinel = external_tombstone / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    store.sets_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BackupStateError, match="sets directory"):
        if operation == "list":
            store.list_sets()
        else:
            store.delete_set("missing-backup")

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert external_tombstone.is_dir()


def test_deletion_reconciliation_survives_substitution_before_recursive_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    backup_id = "reconcile-substitution"
    canonical = store.finalize_set(
        _prepare_minimal_set(store, backup_id),
        created_at=FIXED_TIME,
    ).path
    tombstone = store.sets_dir / f".deleting-{backup_id}-synthetic"
    canonical.rename(tombstone)

    detached_sets = tmp_path / "detached-sets"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    external_tombstone = outside / tombstone.name
    external_tombstone.mkdir(mode=0o700)
    sentinel = external_tombstone / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    substituted = False
    real_open = os.open
    real_rmtree = storage_module.shutil.rmtree

    def substitute_sets_directory() -> None:
        nonlocal substituted
        if substituted:
            return
        store.sets_dir.rename(detached_sets)
        store.sets_dir.symlink_to(outside, target_is_directory=True)
        substituted = True

    def open_after_substitution(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if (
            str(path) == tombstone.name
            and dir_fd is not None
            and flags & getattr(os, "O_DIRECTORY", 0)
        ):
            substitute_sets_directory()
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def path_based_remove_after_substitution(path: str | Path) -> None:
        if Path(path) == tombstone:
            substitute_sets_directory()
            real_rmtree(path)
            return
        real_rmtree(path)

    monkeypatch.setattr(os, "open", open_after_substitution)
    monkeypatch.setattr(
        os,
        "supports_dir_fd",
        os.supports_dir_fd | {open_after_substitution},
    )
    monkeypatch.setattr(
        storage_module.shutil,
        "rmtree",
        path_based_remove_after_substitution,
    )

    with pytest.raises(BackupStateError, match="sets directory"):
        store.list_sets()

    assert substituted is True
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert external_tombstone.is_dir()


def test_deletion_reconciliation_removes_a_real_local_tombstone_by_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    backup_id = "reconcile-local"
    canonical = store.finalize_set(
        _prepare_minimal_set(store, backup_id),
        created_at=FIXED_TIME,
    ).path
    tombstone = store.sets_dir / f".deleting-{backup_id}-synthetic"
    canonical.rename(tombstone)

    def reject_path_based_remove(_path: str | Path) -> None:
        raise AssertionError("reconciliation must not use path-based rmtree")

    monkeypatch.setattr(storage_module.shutil, "rmtree", reject_path_based_remove)

    assert store.list_sets() == []
    assert not tombstone.exists()


def test_delete_commit_is_not_ambiguous_when_descriptor_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    canonical = store.finalize_set(
        _prepare_minimal_set(store, "delete-close"),
        created_at=FIXED_TIME,
    ).path
    real_close = os.close
    detached_closes: list[int] = []
    injected = False

    def close_with_one_post_commit_error(descriptor: int) -> None:
        nonlocal injected
        if not canonical.exists():
            detached_closes.append(descriptor)
            real_close(descriptor)
            if not injected:
                injected = True
                raise OSError("synthetic close after deletion commit")
            return
        real_close(descriptor)

    monkeypatch.setattr(os, "close", close_with_one_post_commit_error)

    store.delete_set("delete-close")

    assert injected is True
    assert not canonical.exists()
    assert len(detached_closes) >= 2
    for descriptor in detached_closes:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_delete_post_rename_error_commits_when_durable_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    canonical = store.finalize_set(
        _prepare_minimal_set(store, "delete-post-rename"),
        created_at=FIXED_TIME,
    ).path
    real_fsync = os.fsync
    real_rename = os.rename
    post_rename_fsync_failed = False
    rollback_attempted = False

    def fail_first_post_rename_fsync(descriptor: int) -> None:
        nonlocal post_rename_fsync_failed
        if not canonical.exists() and not post_rename_fsync_failed:
            post_rename_fsync_failed = True
            raise OSError("synthetic post-rename fsync failure")
        real_fsync(descriptor)

    def fail_rollback_rename(
        source: str | bytes | Path,
        destination: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal rollback_attempted
        if str(source).startswith(
            (
                ".deleting-delete-post-rename-",
                ".delete-pending-delete-post-rename-",
            )
        ) and str(destination) == "delete-post-rename":
            rollback_attempted = True
            raise OSError("synthetic rollback rename failure")
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "fsync", fail_first_post_rename_fsync)
    monkeypatch.setattr(os, "rename", fail_rollback_rename)

    store.delete_set("delete-post-rename")

    assert post_rename_fsync_failed is True
    assert rollback_attempted is True
    assert not canonical.exists()
    assert store.list_sets() == []
    assert not list(store.sets_dir.glob(".deleting-delete-post-rename-*"))


def test_delete_post_rename_error_is_reported_after_durable_restoration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    canonical = store.finalize_set(
        _prepare_minimal_set(store, "delete-restored"),
        created_at=FIXED_TIME,
    ).path
    real_fsync = os.fsync
    post_rename_fsync_failed = False

    def fail_first_post_rename_fsync(descriptor: int) -> None:
        nonlocal post_rename_fsync_failed
        if not canonical.exists() and not post_rename_fsync_failed:
            post_rename_fsync_failed = True
            raise OSError("synthetic post-rename fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_first_post_rename_fsync)

    with pytest.raises(BackupStateError, match="durably restored"):
        store.delete_set("delete-restored")

    assert post_rename_fsync_failed is True
    assert (canonical / "SEALED").is_file()
    assert [item.backup_id for item in store.list_sets()] == ["delete-restored"]
    assert not list(store.sets_dir.glob(".delete-pending-delete-restored-*"))
    assert not list(store.sets_dir.glob(".deleting-delete-restored-*"))
    assert not list(store.sets_dir.glob(".delete-uncertain-delete-restored-*"))


def test_delete_rename_error_after_effect_is_durably_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    canonical = store.finalize_set(
        _prepare_minimal_set(store, "delete-rename-effect"),
        created_at=FIXED_TIME,
    ).path
    real_rename = os.rename
    injected = False

    def rename_then_fail(
        source: str | bytes | Path,
        destination: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal injected
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if str(source) == "delete-rename-effect" and not injected:
            injected = True
            raise OSError("synthetic rename error after effect")

    monkeypatch.setattr(os, "rename", rename_then_fail)

    with pytest.raises(BackupStateError, match="durably restored"):
        store.delete_set("delete-rename-effect")

    assert injected is True
    assert (canonical / "SEALED").is_file()
    assert [item.backup_id for item in store.list_sets()] == [
        "delete-rename-effect"
    ]
    assert not list(store.sets_dir.glob(".delete-pending-delete-rename-effect-*"))
    assert not list(store.sets_dir.glob(".deleting-delete-rename-effect-*"))
    assert not list(
        store.sets_dir.glob(".delete-uncertain-delete-rename-effect-*")
    )


def test_delete_unresolved_post_rename_error_is_never_reconciled_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    canonical = store.finalize_set(
        _prepare_minimal_set(store, "delete-uncertain"),
        created_at=FIXED_TIME,
    ).path
    real_fsync = os.fsync
    real_rename = os.rename

    def fail_every_post_rename_fsync(descriptor: int) -> None:
        if not canonical.exists():
            raise OSError("synthetic persistent post-rename fsync failure")
        real_fsync(descriptor)

    def fail_rollback_rename(
        source: str | bytes | Path,
        destination: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if str(source).startswith(
            (
                ".deleting-delete-uncertain-",
                ".delete-pending-delete-uncertain-",
            )
        ) and str(destination) == "delete-uncertain":
            raise OSError("synthetic persistent rollback failure")
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "fsync", fail_every_post_rename_fsync)
    monkeypatch.setattr(os, "rename", fail_rollback_rename)

    with pytest.raises(
        storage_module.BackupDeletionUncertainError,
        match="manual recovery",
    ):
        store.delete_set("delete-uncertain")

    preserved = list(store.sets_dir.glob(".delete-uncertain-delete-uncertain-*"))
    assert len(preserved) == 1
    assert not canonical.exists()
    assert not list(store.sets_dir.glob(".deleting-delete-uncertain-*"))
    assert store.list_sets() == []
    assert preserved[0].is_dir()


def test_delete_recovery_never_replaces_a_substituted_canonical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    canonical = store.finalize_set(
        _prepare_minimal_set(store, "delete-substituted"),
        created_at=FIXED_TIME,
    ).path
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("keep", encoding="utf-8")
    real_fsync = os.fsync
    substituted = False

    def substitute_before_post_rename_error(descriptor: int) -> None:
        nonlocal substituted
        if not canonical.exists() and not substituted:
            canonical.symlink_to(outside, target_is_directory=True)
            substituted = True
            raise OSError("synthetic post-rename fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", substitute_before_post_rename_error)

    with pytest.raises(storage_module.BackupDeletionUncertainError):
        store.delete_set("delete-substituted")

    assert substituted is True
    assert canonical.is_symlink()
    assert canonical.resolve() == outside.resolve()
    assert (outside / "sentinel").read_text(encoding="utf-8") == "keep"
    preserved = list(
        store.sets_dir.glob(".delete-uncertain-delete-substituted-*")
    )
    assert len(preserved) == 1
    assert store.list_sets() == []
    assert preserved[0].is_dir()


def test_delete_precommit_guard_failure_keeps_the_canonical_set(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    inspection = store.finalize_set(
        _prepare_minimal_set(store, "delete-guard"),
        created_at=FIXED_TIME,
    )

    def reject_commit() -> None:
        raise BackupStateError("synthetic detached operation lease")

    with pytest.raises(BackupStateError, match="detached operation"):
        store.delete_set(
            inspection.manifest.backup_id,
            pre_commit_guard=reject_commit,
        )

    assert (inspection.path / "SEALED").is_file()
    assert [item.backup_id for item in store.list_sets()] == ["delete-guard"]
    assert not list(store.sets_dir.glob(".deleting-delete-guard-*"))


def test_delete_fails_closed_when_sets_directory_is_detached_before_rename(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    backup_id = "delete-detached-sets"
    store.finalize_set(
        _prepare_minimal_set(store, backup_id),
        created_at=FIXED_TIME,
    )
    detached_sets = tmp_path / "detached-sets"
    replacement_marker = store.sets_dir / backup_id / "replacement"

    def detach_sets() -> None:
        store.sets_dir.rename(detached_sets)
        store.sets_dir.mkdir(mode=0o700)
        replacement_marker.parent.mkdir(mode=0o700)
        replacement_marker.write_text("keep", encoding="utf-8")

    with pytest.raises(BackupStateError, match="sets directory"):
        store.delete_set(backup_id, pre_commit_guard=detach_sets)

    assert replacement_marker.read_text(encoding="utf-8") == "keep"
    assert (detached_sets / backup_id / "SEALED").is_file()
    assert not list(detached_sets.glob(f".deleting-{backup_id}-*"))


def test_delete_preserves_uncertain_tombstone_when_sets_detaches_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    backup_id = "delete-detached-after-rename"
    store.finalize_set(
        _prepare_minimal_set(store, backup_id),
        created_at=FIXED_TIME,
    )
    detached_sets = tmp_path / "detached-after-rename"
    replacement_marker = store.sets_dir / backup_id / "replacement"
    real_fsync = os.fsync
    detached = False

    def detach_sets_after_rename(descriptor: int) -> None:
        nonlocal detached
        if not (store.sets_dir / backup_id).exists() and not detached:
            store.sets_dir.rename(detached_sets)
            store.sets_dir.mkdir(mode=0o700)
            replacement_marker.parent.mkdir(mode=0o700)
            replacement_marker.write_text("keep", encoding="utf-8")
            detached = True
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", detach_sets_after_rename)

    with pytest.raises(storage_module.BackupDeletionUncertainError):
        store.delete_set(backup_id)

    assert detached is True
    assert replacement_marker.read_text(encoding="utf-8") == "keep"
    uncertain = list(
        detached_sets.glob(f".delete-uncertain-{backup_id}-*")
    )
    assert len(uncertain) == 1
    assert not list(detached_sets.glob(f".deleting-{backup_id}-*"))


def test_delete_quarantines_restored_backup_when_fsync_detaches_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    backup_id = "delete-detached-fsync-error"
    store.finalize_set(
        _prepare_minimal_set(store, backup_id),
        created_at=FIXED_TIME,
    )
    detached_sets = tmp_path / "detached-fsync-error"
    replacement_marker = store.sets_dir / backup_id / "replacement"
    real_fsync = os.fsync
    detached = False

    def detach_sets_and_fail_fsync(descriptor: int) -> None:
        nonlocal detached
        if not (store.sets_dir / backup_id).exists() and not detached:
            store.sets_dir.rename(detached_sets)
            store.sets_dir.mkdir(mode=0o700)
            replacement_marker.parent.mkdir(mode=0o700)
            replacement_marker.write_text("keep", encoding="utf-8")
            detached = True
            raise OSError("synthetic fsync error after sets detachment")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", detach_sets_and_fail_fsync)

    with pytest.raises(storage_module.BackupDeletionUncertainError):
        store.delete_set(backup_id)

    assert detached is True
    assert replacement_marker.read_text(encoding="utf-8") == "keep"
    uncertain = list(
        detached_sets.glob(f".delete-uncertain-{backup_id}-*")
    )
    assert len(uncertain) == 1
    assert not (detached_sets / backup_id).exists()


def test_delete_never_follows_a_set_symlink(tmp_path: Path) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("keep", encoding="utf-8")
    (store.sets_dir / "symlink-set").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BackupIntegrityError, match="unsafe"):
        store.delete_set("symlink-set")

    assert (outside / "sentinel").read_text(encoding="utf-8") == "keep"


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


def test_explicit_jwt_secret_is_written_as_a_private_signed_resource(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    builder = store.begin_set("explicit-jwt-secret")
    _write_dump(builder)

    builder.write_jwt_secret("explicit-runtime-secret")
    inspection = store.finalize_set(
        builder.prepare(),
        metadata={"jwt_secret": {"mode": "included_resource"}},
        created_at=FIXED_TIME,
    )

    resource = next(
        item for item in inspection.manifest.resources
        if item.path == JWT_SECRET_RESOURCE
    )
    secret_path = inspection.path / JWT_SECRET_RESOURCE
    assert secret_path.read_bytes() == b"explicit-runtime-secret\n"
    assert resource.size == len(b"explicit-runtime-secret\n")
    assert _mode(secret_path) == 0o600

    with pytest.raises(BackupStateError, match="already copied"):
        builder.write_jwt_secret("another-secret")


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
    _write_dump(builder, original)
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

    pinned = store.pin_database_resource(
        authenticated,
        resource_path=DATA_COPY_RESOURCE,
        directory=private_directory,
    )
    try:
        canonical = authenticated.path / DATA_COPY_RESOURCE
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

        pinned = store.pin_database_resource(
            authenticated,
            resource_path=DATA_COPY_RESOURCE,
            directory=target,
        )
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


def test_existing_staging_target_is_reopened_and_secret_hashed_by_descriptor(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)
    relative = Path("plan-reopen") / "data"
    secret = b"validated staged secret\n"

    with store.open_staging_data_dir(staging_root, relative) as created:
        (created.path / "storage").mkdir(mode=0o700)
        os.chmod(created.path / "storage", 0o700)
        created.write_secret(secret)

    with store.open_existing_staging_data_dir(staging_root, relative) as reopened:
        assert reopened.path == staging_root / relative
        assert reopened.read_secret_sha256() == hashlib.sha256(
            secret.strip()
        ).hexdigest()
        reopened.verify_attached()


def test_existing_staging_target_never_follows_substituted_symlink(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    os.chmod(staging_root, 0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    os.chmod(outside, 0o700)
    (outside / ".jwt_secret").write_text("outside-secret\n", encoding="utf-8")
    os.chmod(outside / ".jwt_secret", 0o600)
    plan = staging_root / "plan-substituted"
    plan.mkdir(mode=0o700)
    os.chmod(plan, 0o700)
    (plan / "data").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BackupStateError):
        store.open_existing_staging_data_dir(
            staging_root,
            Path("plan-substituted") / "data",
        )

    assert (outside / ".jwt_secret").read_text(encoding="utf-8") == (
        "outside-secret\n"
    )


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
