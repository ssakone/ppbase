"""Phase 4 strict-baseline version guards for the native (v2) backup.

The native COPY backup is the first official PPBase baseline:

* ``format_version = 2`` (recorded in the signed manifest),
* ``contract_version = 1`` and ``system_schema_version = 1`` (recorded in
  ``schema.json``),
* every applied application migration keeps its filename + SHA-256 in the
  contract.

Restore accepts *only* an exact match on all three versions and refuses every
mismatch **before** any destructive action (DROP SCHEMA / storage swap). These
tests exercise the fail-closed loaders and the wired restore preflight without a
live PostgreSQL server; the end-to-end destructive round trip lives in
``test_backup_service_native_v2.py``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ppbase.backup.models import (
    BACKUP_FORMAT_VERSION,
    BackupManifest,
    BackupManifestError,
    canonical_json_bytes,
)
from ppbase.backup.native import NativeRestoreError
from ppbase.backup.schema_contract import (
    SCHEMA_CONTRACT_VERSION,
    SYSTEM_SCHEMA_VERSION,
    DatabaseSchema,
    MigrationSpec,
    SchemaContractError,
)
from ppbase.backup.service import NativeBackupService


def _baseline_schema_dict() -> dict:
    """A minimal but complete + valid v2 archive contract (no tables)."""
    schema = DatabaseSchema(
        tables=(),
        views=(),
        indexes=(),
        migrations=(),
        segments=(),
        data_copy_size=0,
    )
    return schema.to_dict()


# 1. Exact supported versions restore successfully -------------------------


def test_exact_baseline_versions_accepted() -> None:
    payload = canonical_json_bytes(_baseline_schema_dict())
    schema = DatabaseSchema.from_archive_bytes(payload)
    assert schema.contract_version == SCHEMA_CONTRACT_VERSION == 1
    assert schema.system_schema_version == SYSTEM_SCHEMA_VERSION == 1


# 2. Unknown format_version is refused before destructive work -------------


def test_unknown_format_version_refused() -> None:
    with pytest.raises(BackupManifestError):
        BackupManifest(
            backup_id="b1",
            created_at="2024-01-01T00:00:00.000000Z",
            signer_fingerprint_sha256="0" * 64,
            resources=(),
            metadata={},
            format_version=3,
        )


def test_v2_manifest_rejects_unknown_resource() -> None:
    # A v2 manifest may only carry the schema + copy (+ optional secret/files).
    from ppbase.backup.models import (
        DATA_COPY_RESOURCE,
        SCHEMA_JSON_RESOURCE,
        BackupResource,
    )

    resources = (
        BackupResource(
            path="resources/database/legacy.dump", size=1, sha256="a" * 64
        ),
        BackupResource(path=DATA_COPY_RESOURCE, size=1, sha256="b" * 64),
        BackupResource(path=SCHEMA_JSON_RESOURCE, size=1, sha256="c" * 64),
    )
    with pytest.raises(BackupManifestError):
        BackupManifest(
            backup_id="b1",
            created_at="2024-01-01T00:00:00.000000Z",
            signer_fingerprint_sha256="0" * 64,
            resources=resources,
            metadata={},
            format_version=BACKUP_FORMAT_VERSION,
        )


# 3. Unknown contract_version is refused before destructive work -----------


def test_unknown_contract_version_refused() -> None:
    payload = _baseline_schema_dict()
    payload["contract_version"] = 2
    with pytest.raises(SchemaContractError):
        DatabaseSchema.from_dict(payload)


# 4. Unknown system_schema_version is refused before destructive work ------


def test_unknown_system_schema_version_refused() -> None:
    payload = _baseline_schema_dict()
    payload["system_schema_version"] = 2
    with pytest.raises(SchemaContractError):
        DatabaseSchema.from_dict(payload)


# 5. Modified or missing application migration hashes are refused ----------


def test_modified_migration_hash_refused() -> None:
    # A tampered (non-hex) SHA-256 is rejected by the migration spec.
    with pytest.raises(SchemaContractError):
        MigrationSpec(file="0001.py", sha256="not-a-sha", applied="2024-01-01")


def test_missing_migration_field_refused() -> None:
    with pytest.raises(SchemaContractError):
        # ``applied`` is missing — the strict loader refuses unknown/missing keys.
        MigrationSpec.from_dict({"file": "0001.py", "sha256": "a" * 64})


def test_archive_with_tampered_migration_hash_refused() -> None:
    payload = _baseline_schema_dict()
    payload["migrations"] = [
        {"file": "0001.py", "sha256": "ZZ" + "a" * 62, "applied": "2024-01-01"}
    ]
    with pytest.raises(SchemaContractError):
        DatabaseSchema.from_dict(payload)


# Restore preflight: the wired guard refuses before any destructive action -


def test_preflight_accepts_baseline_contract(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_bytes(canonical_json_bytes(_baseline_schema_dict()))
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    # Does not raise for the exact baseline versions (no migrations to verify).
    NativeBackupService._validate_native_contract_preflight(
        schema_path, migrations_dir
    )


@pytest.mark.parametrize("field", ["contract_version", "system_schema_version"])
def test_preflight_refuses_version_mismatch(tmp_path: Path, field: str) -> None:
    payload = _baseline_schema_dict()
    payload[field] = 2
    schema_path = tmp_path / "schema.json"
    schema_path.write_bytes(canonical_json_bytes(payload))
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    with pytest.raises(SchemaContractError):
        NativeBackupService._validate_native_contract_preflight(
            schema_path, migrations_dir
        )


def _schema_with_migration(sha256: str | None) -> dict:
    payload = _baseline_schema_dict()
    payload["migrations"] = [
        {"file": "0001_init.py", "sha256": sha256, "applied": "2024-01-01"}
    ]
    return payload


def test_preflight_accepts_matching_migration_hash(tmp_path: Path) -> None:
    body = b"# migration bytes\n"
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_init.py").write_bytes(body)
    schema_path = tmp_path / "schema.json"
    schema_path.write_bytes(
        canonical_json_bytes(
            _schema_with_migration(hashlib.sha256(body).hexdigest())
        )
    )
    # A migration whose on-disk bytes hash to the archived value is accepted.
    NativeBackupService._validate_native_contract_preflight(
        schema_path, migrations_dir
    )


def test_preflight_refuses_mismatched_migration_hash(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    # The file exists but its bytes differ from the archived hash. The archived
    # value is a different, validly shaped 64-hex string.
    (migrations_dir / "0001_init.py").write_bytes(b"# tampered bytes\n")
    schema_path = tmp_path / "schema.json"
    schema_path.write_bytes(
        canonical_json_bytes(_schema_with_migration("a" * 64))
    )
    with pytest.raises(NativeRestoreError):
        NativeBackupService._validate_native_contract_preflight(
            schema_path, migrations_dir
        )


def test_preflight_refuses_missing_migration_file(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    schema_path = tmp_path / "schema.json"
    schema_path.write_bytes(
        canonical_json_bytes(_schema_with_migration("a" * 64))
    )
    with pytest.raises(NativeRestoreError):
        NativeBackupService._validate_native_contract_preflight(
            schema_path, migrations_dir
        )


def test_preflight_refuses_null_archived_migration_hash(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_init.py").write_bytes(b"# migration bytes\n")
    schema_path = tmp_path / "schema.json"
    # An archived null hash cannot be verified and is refused pre-restore.
    schema_path.write_bytes(canonical_json_bytes(_schema_with_migration(None)))
    with pytest.raises(NativeRestoreError):
        NativeBackupService._validate_native_contract_preflight(
            schema_path, migrations_dir
        )


@pytest.mark.parametrize(
    "file_name",
    ["migration.py", "0001_init.txt", "../0001_init.py", "nested/0001_init.py"],
)
def test_preflight_refuses_invalid_migration_filename(
    tmp_path: Path,
    file_name: str,
) -> None:
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    payload = _baseline_schema_dict()
    payload["migrations"] = [
        {"file": file_name, "sha256": "a" * 64, "applied": "2024-01-01"}
    ]
    schema_path = tmp_path / "schema.json"
    schema_path.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(NativeRestoreError, match="migration filename"):
        NativeBackupService._validate_native_contract_preflight(
            schema_path, migrations_dir
        )


def test_preflight_refuses_symlinked_migrations_directory(tmp_path: Path) -> None:
    body = b"# migration bytes\n"
    real_dir = tmp_path / "real_migrations"
    real_dir.mkdir()
    (real_dir / "0001_init.py").write_bytes(body)
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.symlink_to(real_dir, target_is_directory=True)
    schema_path = tmp_path / "schema.json"
    schema_path.write_bytes(
        canonical_json_bytes(
            _schema_with_migration(hashlib.sha256(body).hexdigest())
        )
    )

    with pytest.raises(NativeRestoreError, match="directory must not be a symlink"):
        NativeBackupService._validate_native_contract_preflight(
            schema_path, migrations_dir
        )


def test_preflight_ignores_extra_unapplied_migration(tmp_path: Path) -> None:
    body = b"# migration bytes\n"
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_init.py").write_bytes(body)
    # A newer, unapplied local migration not present in the archive is allowed.
    (migrations_dir / "0002_new.py").write_bytes(b"# newer local migration\n")
    schema_path = tmp_path / "schema.json"
    schema_path.write_bytes(
        canonical_json_bytes(
            _schema_with_migration(hashlib.sha256(body).hexdigest())
        )
    )
    NativeBackupService._validate_native_contract_preflight(
        schema_path, migrations_dir
    )


# 6. No legacy database.dump archive is accepted after cutover -------------
# The v2 required-resource set is exactly the COPY-based schema + data pair; no
# legacy pg_dump archive resource is recognised.


def test_v2_required_resources_are_copy_pair_only() -> None:
    from ppbase.backup.models import DATA_COPY_RESOURCE, SCHEMA_JSON_RESOURCE
    from ppbase.backup.storage import _required_database_resources

    required = _required_database_resources()
    assert required == frozenset({SCHEMA_JSON_RESOURCE, DATA_COPY_RESOURCE})
