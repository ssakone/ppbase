from __future__ import annotations

import hashlib
import io
import stat
import struct
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ppbase.backup.copy_format import COMPRESSION_ZLIB, CopySegment
from ppbase.backup.identity import BackupIdentity
from ppbase.backup.models import canonical_json_bytes, parse_canonical_json
from ppbase.backup.schema_contract import (
    ColumnSpec,
    DatabaseSchema,
    IndexKeySpec,
    IndexSpec,
    MigrationSpec,
    TableSpec,
    ViewSpec,
)
from ppbase.backup.storage import (
    BACKUP_FORMAT_VERSION,
    DATA_COPY_RESOURCE,
    SCHEMA_JSON_RESOURCE,
    LocalBackupStore,
)
from ppbase.backup.transport import (
    BackupTransportError,
    BackupTransportLimits,
    PinnedBackupZip,
    backup_transport_size,
    materialize_backup_zip,
    prepare_backup_zip_import,
)


FIXED_TIME = datetime(2026, 7, 18, 10, 11, 12, 345678, tzinfo=UTC)


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
    identity: BackupIdentity | None = None,
    backup_id: str = "transport-source",
    dump_payload: bytes = b"PGDMP native transport payload",
    business_filename: str = "document.txt",
) -> tuple[LocalBackupStore, object]:
    store = LocalBackupStore(tmp_path / f"{backup_id}-store", identity=identity)
    storage = tmp_path / f"{backup_id}-data" / "storage"
    business_file = storage / "collection" / "record" / business_filename
    business_file.parent.mkdir(parents=True)
    business_file.write_bytes(b"business file payload")
    secret = storage.parent / ".jwt_secret"
    secret.write_bytes(b"D" * 64)

    builder = store.begin_set(backup_id)
    builder.create_database_directory()
    builder.database_schema_path.write_bytes(
        b'{"tables": [], "views": [], "indexes": []}'
    )
    builder.database_copy_path.write_bytes(dump_payload)
    builder.copy_storage(storage)
    builder.copy_jwt_secret(secret)
    inspection = store.finalize_set(
        builder.prepare(),
        metadata={"app_name": "My PPBase App"},
        created_at=FIXED_TIME,
    )
    return store, inspection


def _native_schema_bytes() -> tuple[bytes, bytes]:
    """Return a genuine (schema.json, data.copy) pair for a v2 backup set.

    The COPY payload is a real compressed segment container consistent with the
    single-segment schema, so the artifact that flows through the ZIP is an
    authentic native v2 backup rather than opaque filler.
    """
    data_copy = b"x" * 20
    segment = CopySegment(
        table="posts",
        columns=("id", "title", "tags"),
        offset=0,
        compressed_size=len(data_copy),
        compressed_sha256=hashlib.sha256(data_copy).hexdigest(),
        uncompressed_size=40,
        uncompressed_sha256=hashlib.sha256(b"y" * 40).hexdigest(),
        row_count=0,
        compression=COMPRESSION_ZLIB,
    )
    schema = DatabaseSchema(
        tables=(
            TableSpec(
                name="posts",
                kind="base",
                columns=(
                    ColumnSpec("id", "character varying(15)", False, None),
                    ColumnSpec("title", "text", True, None),
                    ColumnSpec("tags", "text[]", True, "'{}'::text[]"),
                ),
                primary_key=("id",),
                unique_constraints=(("title",),),
            ),
        ),
        views=(
            ViewSpec(
                name="posts_public",
                definition="SELECT id, title FROM posts",
                depends_on=("posts",),
            ),
        ),
        indexes=(
            IndexSpec(
                name="idx_posts_title",
                table="posts",
                method="btree",
                unique=False,
                nulls_not_distinct=False,
                keys=(IndexKeySpec("title", descending=True, nulls_first=None),),
                included=("id",),
                predicate="(\"title\" <> ''::text)",
            ),
        ),
        migrations=(MigrationSpec("0001_init.py", "a" * 64, "2021-01-01T00:00:00Z"),),
        segments=(segment,),
        data_copy_size=len(data_copy),
        source_postgres_version=160014,
    )
    return schema.to_canonical_bytes(), data_copy


def _create_native_v2_backup(
    tmp_path: Path,
    *,
    identity: BackupIdentity | None = None,
    backup_id: str = "native-v2-source",
) -> tuple[LocalBackupStore, object, bytes, bytes]:
    store = LocalBackupStore(tmp_path / f"{backup_id}-store", identity=identity)
    storage = tmp_path / f"{backup_id}-data" / "storage"
    business_file = storage / "collection" / "record" / "document.txt"
    business_file.parent.mkdir(parents=True)
    business_file.write_bytes(b"business file payload")
    secret = storage.parent / ".jwt_secret"
    secret.write_bytes(b"D" * 64)

    schema_bytes, data_copy = _native_schema_bytes()
    builder = store.begin_set(backup_id, format_version=BACKUP_FORMAT_VERSION)
    builder.create_database_directory()
    builder.database_schema_path.write_bytes(schema_bytes)
    builder.database_copy_path.write_bytes(data_copy)
    builder.copy_storage(storage)
    builder.copy_jwt_secret(secret)
    inspection = store.finalize_set(
        builder.prepare(),
        metadata={"app_name": "My PPBase App"},
        created_at=FIXED_TIME,
    )
    return store, inspection, schema_bytes, data_copy


def test_native_v2_zip_round_trip_preserves_schema_and_copy(tmp_path: Path) -> None:
    identity = BackupIdentity.load_or_create(tmp_path / "control")
    source, inspection, schema_bytes, data_copy = _create_native_v2_backup(
        tmp_path, identity=identity
    )
    assert inspection.manifest.format_version == BACKUP_FORMAT_VERSION

    payload = _download_bytes(source, inspection.manifest.backup_id)
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        names = archive.namelist()
        assert names[:3] == ["manifest.json", "manifest.sig", "signer.pub"]
        assert SCHEMA_JSON_RESOURCE in names
        assert DATA_COPY_RESOURCE in names
        assert "resources/database.dump" not in names
        assert archive.read(SCHEMA_JSON_RESOURCE) == schema_bytes
        assert archive.read(DATA_COPY_RESOURCE) == data_copy

    target = LocalBackupStore(tmp_path / "target-store", identity=identity)
    prepared = prepare_backup_zip_import(
        target,
        io.BytesIO(payload),
        expected_public_key=identity.public_key_bytes,
        limits=_limits(),
    )
    imported = target.finalize_imported_set(
        prepared.prepared,
        manifest_bytes=prepared.manifest_bytes,
        signature=prepared.signature,
        signer_public_key=prepared.signer_public_key,
        expected_public_key=identity.public_key_bytes,
    )

    assert imported.manifest == inspection.manifest
    assert imported.manifest.format_version == BACKUP_FORMAT_VERSION
    assert (imported.path / SCHEMA_JSON_RESOURCE).read_bytes() == schema_bytes
    assert (imported.path / DATA_COPY_RESOURCE).read_bytes() == data_copy


def _download_bytes(store: LocalBackupStore, backup_id: str) -> bytes:
    pinned = materialize_backup_zip(
        store,
        backup_id,
        expected_public_key=store.identity.public_key_bytes,
        chunk_size=5,
    )
    assert pinned.filename.startswith(
        "ppbase_backup_my_ppbase_app_20260718T101112Z_"
    )
    assert pinned.filename.endswith(".zip")
    payload = b"".join(pinned.iter_bytes(11))
    assert pinned.closed is True
    assert len(payload) == pinned.size
    return payload


def _rewrite_zip(
    payload: bytes,
    transform,
) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(payload), "r")
    output = io.BytesIO()
    with source, zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as destination:
        for info in source.infolist():
            name, data, replacement_info = transform(
                info.filename,
                source.read(info),
                info,
            )
            if name is None:
                continue
            selected = replacement_info or zipfile.ZipInfo(name)
            selected.filename = name
            selected.compress_type = zipfile.ZIP_DEFLATED
            destination.writestr(selected, data)
    return output.getvalue()


def _mutate_first_zip_header(
    payload: bytes,
    *,
    local_offset: int,
    central_offset: int,
    transform,
) -> bytes:
    mutated = bytearray(payload)
    local = mutated.index(b"PK\x03\x04")
    central = mutated.index(b"PK\x01\x02")
    local_value = struct.unpack_from("<H", mutated, local + local_offset)[0]
    central_value = struct.unpack_from("<H", mutated, central + central_offset)[0]
    struct.pack_into("<H", mutated, local + local_offset, transform(local_value))
    struct.pack_into("<H", mutated, central + central_offset, transform(central_value))
    return bytes(mutated)


def test_standard_zip_round_trip_preserves_signed_envelope_and_resources(
    tmp_path: Path,
) -> None:
    identity = BackupIdentity.load_or_create(tmp_path / "control")
    source, source_inspection = _create_backup(tmp_path, identity=identity)

    payload = _download_bytes(source, source_inspection.manifest.backup_id)
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        names = archive.namelist()
        assert names[:3] == ["manifest.json", "manifest.sig", "signer.pub"]
        assert names[3:] == sorted(
            resource.path for resource in source_inspection.manifest.resources
        )
        assert "SEALED" not in names
        assert "signing-ed25519.key" not in names
        assert all(
            info.compress_type == zipfile.ZIP_STORED
            for info in archive.infolist()
        )
        assert archive.read("manifest.json") == (
            source_inspection.path / "manifest.json"
        ).read_bytes()

    target = LocalBackupStore(tmp_path / "target-store", identity=identity)
    prepared = prepare_backup_zip_import(
        target,
        io.BytesIO(payload),
        expected_public_key=identity.public_key_bytes,
        limits=_limits(),
    )
    imported = target.finalize_imported_set(
        prepared.prepared,
        manifest_bytes=prepared.manifest_bytes,
        signature=prepared.signature,
        signer_public_key=prepared.signer_public_key,
        expected_public_key=identity.public_key_bytes,
    )

    assert imported.manifest == source_inspection.manifest
    assert (imported.path / "manifest.json").read_bytes() == (
        source_inspection.path / "manifest.json"
    ).read_bytes()
    assert (imported.path / DATA_COPY_RESOURCE).read_bytes() == (
        source_inspection.path / DATA_COPY_RESOURCE
    ).read_bytes()


def test_highly_compressible_native_zip_can_be_reuploaded_with_default_ratio(
    tmp_path: Path,
) -> None:
    source, inspection = _create_backup(
        tmp_path,
        backup_id="compressible-native",
        dump_payload=b"0" * (1024 * 1024),
    )
    payload = _download_bytes(source, inspection.manifest.backup_id)
    target = LocalBackupStore(tmp_path / "compressible-target", identity=source.identity)

    prepared = prepare_backup_zip_import(
        target,
        io.BytesIO(payload),
        expected_public_key=source.identity.public_key_bytes,
        limits=_limits(max_upload_bytes=2 * 1024 * 1024),
    )
    imported = target.finalize_imported_set(
        prepared.prepared,
        manifest_bytes=prepared.manifest_bytes,
        signature=prepared.signature,
        signer_public_key=prepared.signer_public_key,
        expected_public_key=source.identity.public_key_bytes,
    )

    assert imported.manifest == inspection.manifest


def test_transport_size_matches_materialized_zip_including_zip64_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, inspection = _create_backup(tmp_path, backup_id="transport-size")

    ordinary = materialize_backup_zip(
        source,
        inspection.manifest.backup_id,
        expected_public_key=source.identity.public_key_bytes,
        chunk_size=11,
    )
    try:
        assert backup_transport_size(inspection.manifest) == ordinary.size
    finally:
        ordinary.close()

    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 64)
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 2)
    zip64 = materialize_backup_zip(
        source,
        inspection.manifest.backup_id,
        expected_public_key=source.identity.public_key_bytes,
        chunk_size=11,
    )
    try:
        assert backup_transport_size(inspection.manifest) == zip64.size
    finally:
        zip64.close()


def test_transport_size_accounts_for_utf8_resource_names(tmp_path: Path) -> None:
    source, inspection = _create_backup(
        tmp_path,
        backup_id="transport-unicode-size",
        business_filename="pièce-été.txt",
    )

    pinned = materialize_backup_zip(
        source,
        inspection.manifest.backup_id,
        expected_public_key=source.identity.public_key_bytes,
        chunk_size=11,
    )
    try:
        assert pinned.size == backup_transport_size(inspection.manifest)
        payload = b"".join(pinned.iter_bytes(13))
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            assert any("pièce-été.txt" in name for name in archive.namelist())
    finally:
        pinned.close()


def test_valid_foreign_signer_is_refused_without_any_published_residue(
    tmp_path: Path,
) -> None:
    foreign_identity = BackupIdentity.load_or_create(tmp_path / "foreign-control")
    foreign, inspection = _create_backup(
        tmp_path,
        identity=foreign_identity,
        backup_id="foreign-transport",
    )
    payload = _download_bytes(foreign, inspection.manifest.backup_id)
    local = LocalBackupStore(tmp_path / "local-store")

    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            local,
            io.BytesIO(payload),
            expected_public_key=local.identity.public_key_bytes,
            limits=_limits(),
        )

    assert error.value.code == "backup_signer_untrusted"
    assert local.list_sets() == []
    assert not list(local.sets_dir.glob(".partial-*"))


def test_foreign_signer_is_fully_checksum_verified_before_untrusted_result(
    tmp_path: Path,
) -> None:
    foreign_identity = BackupIdentity.load_or_create(tmp_path / "foreign-control")
    foreign, inspection = _create_backup(
        tmp_path,
        identity=foreign_identity,
        backup_id="foreign-tampered",
    )
    payload = _download_bytes(foreign, inspection.manifest.backup_id)

    def alter_same_size(name: str, data: bytes, info: zipfile.ZipInfo):
        if name == DATA_COPY_RESOURCE:
            data = bytes([data[0] ^ 1]) + data[1:]
        return name, data, info

    local = LocalBackupStore(tmp_path / "local-store")
    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            local,
            io.BytesIO(_rewrite_zip(payload, alter_same_size)),
            expected_public_key=local.identity.public_key_bytes,
            limits=_limits(),
        )

    assert error.value.code == "backup_resource_checksum_mismatch"
    assert local.list_sets() == []


def test_signature_and_resource_tampering_are_rejected(tmp_path: Path) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _download_bytes(source, inspection.manifest.backup_id)

    def alter_signature(name: str, data: bytes, info: zipfile.ZipInfo):
        if name == "manifest.sig":
            data = bytes([data[0] ^ 1]) + data[1:]
        return name, data, info

    def alter_dump(name: str, data: bytes, info: zipfile.ZipInfo):
        if name == DATA_COPY_RESOURCE:
            data = b"tampered dump payload"
        return name, data, info

    for mutated, expected_code in (
        (_rewrite_zip(payload, alter_signature), "backup_signature_invalid"),
        (_rewrite_zip(payload, alter_dump), "backup_resource_size_mismatch"),
    ):
        target = LocalBackupStore(
            tmp_path / f"target-{expected_code}",
            identity=source.identity,
        )
        with pytest.raises(BackupTransportError) as error:
            prepare_backup_zip_import(
                target,
                io.BytesIO(mutated),
                expected_public_key=target.identity.public_key_bytes,
                limits=_limits(),
            )
        assert error.value.code == expected_code
        assert target.list_sets() == []


def test_corrupt_deflate_stream_is_mapped_and_partial_is_removed(
    tmp_path: Path,
) -> None:
    source, inspection = _create_backup(
        tmp_path,
        backup_id="corrupt-deflate",
        dump_payload=b"compressible database payload" * 4096,
    )
    stored = _download_bytes(source, inspection.manifest.backup_id)
    compressed = bytearray(
        _rewrite_zip(stored, lambda name, data, info: (name, data, info))
    )
    with zipfile.ZipFile(io.BytesIO(compressed), "r") as archive:
        info = archive.getinfo(DATA_COPY_RESOURCE)
        local_offset = info.header_offset
        filename_size = struct.unpack_from("<H", compressed, local_offset + 26)[0]
        extra_size = struct.unpack_from("<H", compressed, local_offset + 28)[0]
        data_offset = local_offset + zipfile.sizeFileHeader + filename_size + extra_size
        compressed[data_offset : data_offset + info.compress_size] = (
            b"\xff" * info.compress_size
        )

    target = LocalBackupStore(tmp_path / "corrupt-deflate-target", identity=source.identity)
    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            target,
            io.BytesIO(compressed),
            expected_public_key=source.identity.public_key_bytes,
            limits=_limits(),
        )

    assert error.value.code == "backup_resource_invalid"
    assert not list(target.sets_dir.glob(".partial-*"))


@pytest.mark.parametrize("unsafe_name", ("../outside", "/absolute", "a\\b"))
def test_zip_slip_and_backslash_paths_are_rejected(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _download_bytes(source, inspection.manifest.backup_id)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as original, zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for info in original.infolist():
            archive.writestr(info.filename, original.read(info))
        archive.writestr(unsafe_name, b"escape")
    target = LocalBackupStore(tmp_path / "target")

    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            target,
            io.BytesIO(output.getvalue()),
            expected_public_key=target.identity.public_key_bytes,
            limits=_limits(),
        )

    assert error.value.code == "backup_zip_unsafe_path"


def test_duplicate_and_symlink_members_are_rejected(tmp_path: Path) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _download_bytes(source, inspection.manifest.backup_id)

    duplicate = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as original, zipfile.ZipFile(
        duplicate,
        "w",
    ) as archive:
        for info in original.infolist():
            archive.writestr(info.filename, original.read(info))
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("manifest.json", b"duplicate")

    symlink = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as original, zipfile.ZipFile(
        symlink,
        "w",
    ) as archive:
        for info in original.infolist():
            archive.writestr(info.filename, original.read(info))
        link = zipfile.ZipInfo("resources/files/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"../../outside")

    for candidate, expected_code in (
        (duplicate.getvalue(), "backup_zip_duplicate_entry"),
        (symlink.getvalue(), "backup_zip_special_entry"),
    ):
        target = LocalBackupStore(tmp_path / f"target-{expected_code}")
        with pytest.raises(BackupTransportError) as error:
            prepare_backup_zip_import(
                target,
                io.BytesIO(candidate),
                expected_public_key=target.identity.public_key_bytes,
                limits=_limits(),
            )
        assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("entry_name", "external_mode", "expected_code"),
    (
        ("resources/files/directory/", stat.S_IFDIR | 0o700, "backup_zip_special_entry"),
        ("resources/files/fifo", stat.S_IFIFO | 0o600, "backup_zip_special_entry"),
        ("resources/files/device", stat.S_IFCHR | 0o600, "backup_zip_special_entry"),
        ("resources/files/socket", stat.S_IFSOCK | 0o600, "backup_zip_special_entry"),
        ("resources/files/e\u0301", stat.S_IFREG | 0o600, "backup_zip_unsafe_path"),
    ),
)
def test_directory_special_and_non_nfc_members_are_rejected(
    tmp_path: Path,
    entry_name: str,
    external_mode: int,
    expected_code: str,
) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _download_bytes(source, inspection.manifest.backup_id)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as original, zipfile.ZipFile(
        output,
        "w",
    ) as archive:
        for info in original.infolist():
            archive.writestr(info.filename, original.read(info))
        special = zipfile.ZipInfo(entry_name)
        special.create_system = 3
        special.external_attr = external_mode << 16
        archive.writestr(special, b"payload")
    target = LocalBackupStore(tmp_path / "target")

    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            target,
            io.BytesIO(output.getvalue()),
            expected_public_key=target.identity.public_key_bytes,
            limits=_limits(),
        )

    assert error.value.code == expected_code


def test_nul_encryption_and_unknown_compression_are_rejected(tmp_path: Path) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _download_bytes(source, inspection.manifest.backup_id)

    # Replace the final byte of the first five-byte member name with NUL in
    # both the local and central headers without changing archive offsets.
    nul_payload = payload
    first_name = b"manifest.json"
    nul_name = b"manifest.jso\x00"
    nul_payload = nul_payload.replace(first_name, nul_name)
    encrypted = _mutate_first_zip_header(
        payload,
        local_offset=6,
        central_offset=8,
        transform=lambda value: value | 0x1,
    )
    unsupported = _mutate_first_zip_header(
        payload,
        local_offset=8,
        central_offset=10,
        transform=lambda _value: 99,
    )
    unsupported_version = bytearray(payload)
    central = unsupported_version.index(b"PK\x01\x02")
    unsupported_version[central + 6] = 255

    for candidate, expected_code in (
        (nul_payload, "backup_zip_unsafe_path"),
        (encrypted, "backup_zip_encrypted"),
        (unsupported, "backup_zip_compression_unsupported"),
        (bytes(unsupported_version), "backup_zip_invalid"),
    ):
        target = LocalBackupStore(tmp_path / f"target-{expected_code}")
        with pytest.raises(BackupTransportError) as error:
            prepare_backup_zip_import(
                target,
                io.BytesIO(candidate),
                expected_public_key=target.identity.public_key_bytes,
                limits=_limits(),
            )
        assert error.value.code == expected_code


def test_unknown_manifest_version_and_member_set_are_rejected(tmp_path: Path) -> None:
    source, inspection = _create_backup(tmp_path)
    payload = _download_bytes(source, inspection.manifest.backup_id)
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        values = {info.filename: archive.read(info) for info in archive.infolist()}

    manifest = parse_canonical_json(values["manifest.json"])
    assert isinstance(manifest, dict)
    # Native logical format v2 is the only supported manifest version. Legacy
    # v1 archives must fail closed before their resource set is considered.
    manifest["format_version"] = 1
    unsupported_manifest = canonical_json_bytes(manifest)
    values["manifest.json"] = unsupported_manifest
    values["manifest.sig"] = source.identity.sign_manifest(unsupported_manifest)
    unsupported = io.BytesIO()
    with zipfile.ZipFile(unsupported, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in values.items():
            archive.writestr(name, data)

    missing = _rewrite_zip(
        payload,
        lambda name, data, info: (
            (None, data, info)
            if name == "signer.pub"
            else (name, data, info)
        ),
    )
    unexpected = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as original, zipfile.ZipFile(
        unexpected,
        "w",
    ) as archive:
        for info in original.infolist():
            archive.writestr(info.filename, original.read(info))
        archive.writestr("unexpected.txt", b"extra")

    for candidate, expected_code in (
        (unsupported.getvalue(), "backup_format_unsupported"),
        (missing, "backup_zip_envelope_missing"),
        (unexpected.getvalue(), "backup_zip_members_invalid"),
    ):
        target = LocalBackupStore(tmp_path / f"target-{expected_code}")
        with pytest.raises(BackupTransportError) as error:
            prepare_backup_zip_import(
                target,
                io.BytesIO(candidate),
                expected_public_key=source.identity.public_key_bytes,
                limits=_limits(),
            )
        assert error.value.code == expected_code


def test_compression_ratio_and_entry_count_limits_are_enforced(
    tmp_path: Path,
) -> None:
    source, inspection = _create_backup(
        tmp_path,
        dump_payload=b"0" * 64_000,
    )
    payload = _download_bytes(source, inspection.manifest.backup_id)
    compressed_payload = _rewrite_zip(
        payload,
        lambda name, data, info: (name, data, info),
    )
    target = LocalBackupStore(tmp_path / "target-ratio")
    with pytest.raises(BackupTransportError) as ratio_error:
        prepare_backup_zip_import(
            target,
            io.BytesIO(compressed_payload),
            expected_public_key=target.identity.public_key_bytes,
            limits=_limits(max_compression_ratio=2),
        )
    assert ratio_error.value.code == "backup_zip_ratio_exceeded"

    target_count = LocalBackupStore(tmp_path / "target-count")
    with pytest.raises(BackupTransportError) as count_error:
        prepare_backup_zip_import(
            target_count,
            io.BytesIO(payload),
            expected_public_key=target_count.identity.public_key_bytes,
            limits=_limits(max_entries=3),
        )
    assert count_error.value.code == "backup_zip_too_many_entries"

    target_resource = LocalBackupStore(tmp_path / "target-resource")
    with pytest.raises(BackupTransportError) as resource_error:
        prepare_backup_zip_import(
            target_resource,
            io.BytesIO(payload),
            expected_public_key=source.identity.public_key_bytes,
            limits=_limits(max_resource_bytes=32),
        )
    assert resource_error.value.code == "backup_zip_resource_too_large"

    target_total = LocalBackupStore(tmp_path / "target-total")
    with pytest.raises(BackupTransportError) as total_error:
        prepare_backup_zip_import(
            target_total,
            io.BytesIO(payload),
            expected_public_key=source.identity.public_key_bytes,
            limits=_limits(max_uncompressed_bytes=64),
        )
    assert total_error.value.code == "backup_zip_uncompressed_too_large"


def test_central_directory_is_bounded_before_zipfile_allocates_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    central_size = 4096
    eocd = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        1,
        1,
        central_size,
        0,
        0,
    )
    payload = (b"\x00" * central_size) + eocd
    target = LocalBackupStore(tmp_path / "target-central-directory")
    zipfile_called = False

    def fail_if_zipfile_is_opened(*_args, **_kwargs):
        nonlocal zipfile_called
        zipfile_called = True
        raise AssertionError("ZipFile must not read an oversized central directory")

    monkeypatch.setattr(zipfile, "ZipFile", fail_if_zipfile_is_opened)

    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            target,
            io.BytesIO(payload),
            expected_public_key=target.identity.public_key_bytes,
            limits=_limits(max_central_directory_bytes=1024),
        )

    assert error.value.code == "backup_zip_central_directory_too_large"
    assert zipfile_called is False
    assert not list(target.sets_dir.glob(".partial-*"))


def test_actual_central_entry_count_is_bounded_before_zipfile_allocates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    central_entry = struct.pack(
        "<4s4B4HL2L5H2L",
        b"PK\x01\x02",
        20,
        3,
        20,
        0,
        *([0] * 14),
    )
    central = central_entry * 5
    eocd = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        1,
        1,
        len(central),
        0,
        0,
    )
    target = LocalBackupStore(tmp_path / "target-central-count")
    zipfile_called = False

    def fail_if_zipfile_is_opened(*_args, **_kwargs):
        nonlocal zipfile_called
        zipfile_called = True
        raise AssertionError("ZipFile must not allocate entries before the count scan")

    monkeypatch.setattr(zipfile, "ZipFile", fail_if_zipfile_is_opened)

    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            target,
            io.BytesIO(central + eocd),
            expected_public_key=target.identity.public_key_bytes,
            limits=_limits(max_entries=4),
        )

    assert error.value.code == "backup_zip_too_many_entries"
    assert zipfile_called is False


def test_pocketbase_sqlite_zip_is_identified_explicitly(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.db", b"SQLite format 3\x00")
        archive.writestr("storage/file.txt", b"file")
    target = LocalBackupStore(tmp_path / "target")

    with pytest.raises(BackupTransportError) as error:
        prepare_backup_zip_import(
            target,
            io.BytesIO(payload.getvalue()),
            expected_public_key=target.identity.public_key_bytes,
            limits=_limits(),
        )

    assert error.value.code == "pocketbase_backup_unsupported"
