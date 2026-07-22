from __future__ import annotations

import asyncio
import io
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ppbase.backup.operations import BackupInUseError
from ppbase.backup.activation import BackupActivationStore
from ppbase.backup.control import ControlPlaneRoot
from ppbase.backup.service import (
    BackupCutoverGuard,
    BackupServiceError,
    NativeBackupService,
)
from ppbase.backup.storage import BackupDeletionUncertainError
from ppbase.config import Settings


FIXED_TIME = datetime(2026, 7, 18, 12, 0, 0, 123456, tzinfo=UTC)


@pytest.mark.asyncio
async def test_cutover_guard_transfers_release_from_restart_thread() -> None:
    events: list[str] = []

    class Barrier:
        active = True

        async def verify_held(self) -> None:
            return None

        async def close(self) -> None:
            events.append("barrier")
            self.active = False

    class OperationContext:
        def __exit__(self, *_args: object) -> None:
            events.append("operation")

    class RestartReservation:
        def release(self) -> None:
            events.append("reservation")

    guard = BackupCutoverGuard(  # type: ignore[arg-type]
        Barrier(),
        restart_reservation=RestartReservation(),
    )
    guard.retain_operation_context(OperationContext())

    await asyncio.to_thread(guard.close_from_restart_thread)

    assert events == ["barrier", "operation", "reservation"]
    assert guard.active is False


@pytest.mark.asyncio
async def test_activation_rereads_plan_under_global_and_backup_leases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    backup_id = "stale-plan-backup"
    plan_id = "a" * 32
    plan_hash = "b" * 64
    before_lock = SimpleNamespace(
        plan_id=plan_id,
        backup_id=backup_id,
        plan_hash=plan_hash,
        status="validated",
    )
    after_lock = SimpleNamespace(
        plan_id=plan_id,
        backup_id=backup_id,
        plan_hash=plan_hash,
        status="abandoned",
    )
    snapshots = iter((before_lock, after_lock))
    monkeypatch.setattr(service.plans, "inspect", lambda _plan_id: next(snapshots))
    try:
        with pytest.raises(BackupServiceError) as rejected:
            await service.activate_staging_plan(
                plan_id,
                expected_plan_hash=plan_hash,
                actor_id="admin_1",
            )

        assert rejected.value.code == "staging_plan_not_validated"
    finally:
        service.close()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=str(tmp_path / "active-data"),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        backup_target_root=str(tmp_path / "targets"),
        backup_max_upload_bytes=8 * 1024 * 1024,
        backup_max_uncompressed_bytes=16 * 1024 * 1024,
        backup_max_resource_bytes=8 * 1024 * 1024,
        backup_max_archive_entries=128,
        backup_max_compression_ratio=500,
        backup_transport_chunk_size=13,
    )


def test_new_staging_plans_allocate_their_data_dir_under_durable_target_root(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    try:
        plan = service.plans.create(
            backup_id="durable-target-plan",
            manifest_sha256="a" * 64,
            destination_fingerprint_sha256="b" * 64,
            jwt_secret_mode="clone",
            actor_id="admin_1",
        )

        target = Path(plan.target_data_dir)
        assert target.is_relative_to(Path(settings.backup_target_root))
        assert not target.is_relative_to(Path(settings.backup_staging_root))
    finally:
        service.close()


def test_active_data_under_target_root_requires_exact_activation_journal(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    active = Path(settings.backup_target_root) / ("a" * 32) / "data"
    active.mkdir(parents=True, mode=0o700)
    active.parent.chmod(0o700)
    settings.data_dir = str(active)

    with pytest.raises(BackupServiceError) as rejected:
        NativeBackupService(object(), settings)  # type: ignore[arg-type]
    assert rejected.value.code == "unsafe_backup_root"

    control = Path(settings.backup_control_dir)
    control.mkdir(mode=0o700, exist_ok=True)
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    info = active.stat()
    try:
        store.prepare(
            activation_id="b" * 32,
            plan_id="a" * 32,
            backup_id="journaled-target",
            plan_hash="c" * 64,
            manifest_sha256="d" * 64,
            signer_fingerprint_sha256="e" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://runtime@db/source",
            previous_data_dir=str(tmp_path / "source-data"),
            previous_restart_command=["serve"],
            target_database_url="postgresql+asyncpg://runtime@db/target",
            target_data_dir=str(active),
            target_restart_command=["serve"],
            expected_jwt_sha256="f" * 64,
            actor_id=None,
            target_data_identity=(info.st_dev, info.st_ino),
        )
        store.mark_starting("b" * 32)
        store.mark_healthy("b" * 32)
    finally:
        store.close()
        root.close()

    service = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    service.close()


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


def _seed_validated_plan(
    service: NativeBackupService,
    backup_id: str,
):
    plan = service.plans.create(
        backup_id=backup_id,
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256="b" * 64,
        jwt_secret_mode="clone",
        actor_id="admin_1",
    )
    running = service.plans.begin_execution(
        plan.plan_id,
        expected_plan_hash=plan.plan_hash,
    )
    return service.plans.finish(
        plan.plan_id,
        status="validated",
        expected_attempt_id=running.status_data["attemptId"],
    )


def _seed_activation_status(
    service: NativeBackupService,
    plan,
    status: str,
) -> str:
    activation_id = "c" * 32
    service.activations.prepare(
        activation_id=activation_id,
        plan_id=plan.plan_id,
        backup_id=plan.backup_id,
        plan_hash=plan.plan_hash,
        manifest_sha256=plan.manifest_sha256,
        signer_fingerprint_sha256="d" * 64,
        jwt_secret_mode=plan.jwt_secret_mode,
        previous_database_url="postgresql+asyncpg://runtime@db/previous",
        previous_data_dir="/srv/previous-data",
        previous_restart_command=["serve"],
        target_database_url="postgresql+asyncpg://runtime@db/target",
        target_data_dir=plan.target_data_dir,
        target_restart_command=["serve"],
        expected_jwt_sha256="e" * 64,
        actor_id="admin_1",
    )
    if status == "starting":
        service.activations.mark_starting(activation_id)
    elif status == "healthy":
        service.activations.mark_starting(activation_id)
        service.activations.mark_healthy(activation_id)
    elif status == "rollback_pending":
        service.activations.mark_rollback_pending(
            activation_id,
            error_code="synthetic_health_failure",
        )
    elif status == "rolled_back":
        service.activations.mark_rollback_pending(
            activation_id,
            error_code="synthetic_health_failure",
        )
        service.activations.mark_rolled_back(activation_id)
    elif status != "restart_scheduled":
        raise AssertionError(f"unsupported activation status: {status}")
    return activation_id


@pytest.mark.asyncio
async def test_postgres_server_identity_reads_database_oid_and_marker() -> None:
    statements: list[str] = []
    marker = "ppbase-restore-marker:" + ("f" * 64)

    class MappingResult:
        def mappings(self):
            return self

        def one(self):
            return {
                "role": "ppbase_runtime",
                "database": "ppbase_stage_123",
                "server_address": "127.0.0.1",
                "server_port": 5432,
                "postmaster_started_at": "1784376000.000000",
                "server_version_num": "160004",
                "backend_pid": 4321,
                "database_oid": 16391,
                "database_marker": marker,
            }

    class Connection:
        async def execute(self, statement):
            statements.append(str(statement))
            if len(statements) == 1:
                return object()
            return MappingResult()

    identity = await NativeBackupService._postgres_server_identity(Connection())

    assert identity == {
        "role": "ppbase_runtime",
        "database": "ppbase_stage_123",
        "server_address": "127.0.0.1",
        "server_port": 5432,
        "postmaster_started_at": "1784376000.000000",
        "server_version_num": "160004",
        "backend_pid": 4321,
        "database_oid": 16391,
        "database_marker": marker,
    }
    assert len(statements) == 2
    assert "d.oid::bigint AS database_oid" in statements[1]
    assert "pg_catalog.shobj_description(d.oid, 'pg_database')" in statements[1]
    assert "FROM pg_catalog.pg_database AS d" in statements[1]


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

        assert uploaded["id"] == original.manifest.backup_id
        assert uploaded["key"] == requested_filename
        assert uploaded["trustStatus"] == "trusted_local"
        assert uploaded["filename"] == requested_filename
        assert uploaded["size"] == transport_size
        listed = await service.list_local_backups()
        assert listed[0]["id"] == original.manifest.backup_id
        assert listed[0]["key"] == requested_filename
        assert listed[0]["modified"] == original.manifest.created_at
        assert listed[0]["size"] == transport_size
        assert listed[0]["totalSize"] == original.manifest.total_size
        assert listed[0]["filename"] == requested_filename
    finally:
        service.close()


@pytest.mark.asyncio
async def test_foreign_upload_is_quarantined_until_exact_signer_is_approved(
    tmp_path: Path,
) -> None:
    source = NativeBackupService(  # type: ignore[arg-type]
        object(),
        _settings(tmp_path / "source"),
    )
    destination = NativeBackupService(  # type: ignore[arg-type]
        object(),
        _settings(tmp_path / "destination"),
    )
    downloaded = None
    try:
        sdk_key = "foreign-service-backup.zip"
        original = _seed_backup(
            source,
            "foreign-service-backup",
            transport_filename=sdk_key,
        )
        archive = await source.materialize_local_backup_zip(
            original.manifest.backup_id
        )
        payload = b"".join(archive.iter_bytes(17))

        with destination.mutation_operation() as lease:
            uploaded = await destination.upload_local_backup(
                io.BytesIO(payload),
                operation_lease=lease,
            )

        assert uploaded["status"] == "quarantined"
        assert uploaded["authenticated"] is False
        assert uploaded["trustStatus"] == "authenticated_untrusted"
        assert uploaded["signerFingerprintSha256"] == (
            source.identity.fingerprint_sha256
        )
        assert (destination.store.sets_dir / original.manifest.backup_id).is_dir()

        listed = await destination.list_local_backups()
        assert listed[0]["status"] == "quarantined"
        assert listed[0]["trustStatus"] == "authenticated_untrusted"
        inspected = await destination.inspect_local_backup(sdk_key)
        assert inspected["resourcesVerified"] is True
        assert inspected["trustStatus"] == "authenticated_untrusted"

        downloaded = await destination.materialize_local_backup_zip(sdk_key)
        assert b"".join(downloaded.iter_bytes(19)) == payload

        with pytest.raises(BackupServiceError) as untrusted:
            await destination.create_staging_plan(
                sdk_key,
                jwt_secret_mode="clone",
                actor_id="admin-b",
            )
        assert untrusted.value.code == "backup_signer_untrusted"

        assert destination.list_trusted_signers() == []
        approved = await destination.approve_backup_signer(
            sdk_key,
            expected_fingerprint_sha256=source.identity.fingerprint_sha256,
            actor_id="admin-b",
        )
        assert approved["fingerprintSha256"] == source.identity.fingerprint_sha256
        assert approved["trustStatus"] == "trusted_external"
        listed_signers = destination.list_trusted_signers()
        assert len(listed_signers) == 1
        assert listed_signers[0] == {
            key: value for key, value in approved.items() if key != "backupId"
        }

        trusted = await destination.inspect_local_backup(sdk_key)
        assert trusted["authenticated"] is True
        assert trusted["trustStatus"] == "trusted_external"
        capability = await destination._trusted_inspection(
            original.manifest.backup_id
        )
        assert capability.approved_public_key == source.identity.public_key_bytes

        destination.revoke_backup_signer(source.identity.fingerprint_sha256)
        assert destination.list_trusted_signers() == []
        with pytest.raises(BackupServiceError) as revoked:
            await destination.create_staging_plan(
                sdk_key,
                jwt_secret_mode="clone",
                actor_id="admin-b",
            )
        assert revoked.value.code == "backup_signer_untrusted"
    finally:
        if downloaded is not None:
            downloaded.close()
        source.close()
        destination.close()


@pytest.mark.asyncio
async def test_sdk_backup_key_is_zip_name_and_routes_resolve_it_to_internal_id(
    tmp_path: Path,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    archive = None
    try:
        requested_filename = "Sauvegarde été.zip"
        original = _seed_backup(
            service,
            "internal-backup-id",
            transport_filename=requested_filename,
        )

        listed = await service.list_local_backups()
        assert listed[0]["id"] == original.manifest.backup_id
        assert listed[0]["key"] == requested_filename
        assert listed[0]["filename"] == requested_filename

        inspected = await service.inspect_local_backup(requested_filename)
        assert inspected["id"] == original.manifest.backup_id
        assert inspected["key"] == requested_filename

        archive = await service.materialize_local_backup_zip(requested_filename)
        assert archive.filename == requested_filename
        assert b"".join(archive.iter_bytes(17)).startswith(b"PK")
        archive.close()
        archive = None

        await service.delete_local_backup(requested_filename)
        assert await service.list_local_backups() == []
    finally:
        if archive is not None:
            archive.close()
        service.close()


@pytest.mark.asyncio
async def test_inspection_paginates_stable_manifest_order_after_full_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    try:
        builder = service.store.begin_set("paginated-inspection")
        builder.database_dump_path.write_bytes(b"PGDMP paginated inspection")
        for index in reversed(range(122)):
            (builder.files_path / f"asset-{index:03d}.txt").write_text(
                f"asset {index}",
                encoding="utf-8",
            )
        sealed = service.store.finalize_set(
            builder.prepare(),
            metadata={"app_name": "Pagination Test"},
            created_at=FIXED_TIME,
            identity_guard=service._require_control_identity_attached,
        )
        expected_paths = [resource.path for resource in sealed.manifest.resources]
        verification_flags: list[bool | None] = []
        real_inspect = service.store.inspect_set

        def track_inspection(*args, **kwargs):
            verification_flags.append(kwargs.get("verify_resources"))
            return real_inspect(*args, **kwargs)

        monkeypatch.setattr(service.store, "inspect_set", track_inspection)
        pages = [
            await service.inspect_local_backup(
                sealed.manifest.backup_id,
                resource_offset=offset,
                resource_limit=50,
            )
            for offset in (0, 50, 100)
        ]
        default_page = await service.inspect_local_backup(
            sealed.manifest.backup_id
        )

        assert verification_flags.count(True) == len(pages) + 1
        assert verification_flags[-1] is True
        assert expected_paths == sorted(expected_paths)
        assert [
            resource["path"]
            for page in pages
            for resource in page["resources"]
        ] == expected_paths
        assert [page["resourceOffset"] for page in pages] == [0, 50, 100]
        assert [page["resourcesReturned"] for page in pages] == [50, 50, 23]
        assert [page["hasMoreResources"] for page in pages] == [True, True, False]
        assert {page["resourceCount"] for page in pages} == {len(expected_paths)}
        assert all(len(page["resources"]) <= 50 for page in pages)
        assert default_page["resourceOffset"] == 0
        assert default_page["resourceLimit"] == 100
        assert default_page["resourcesReturned"] == 100
        assert default_page["hasMoreResources"] is True
    finally:
        service.close()


@pytest.mark.asyncio
async def test_ambiguous_legacy_zip_name_fails_closed_but_ids_remain_available(
    tmp_path: Path,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    try:
        duplicate_name = "duplicate-visible-name.zip"
        first = _seed_backup(
            service,
            "legacy-first",
            transport_filename=duplicate_name,
        )
        second = _seed_backup(
            service,
            "legacy-second",
            transport_filename=duplicate_name,
        )

        with pytest.raises(BackupServiceError) as ambiguous:
            await service.inspect_local_backup(duplicate_name)
        assert ambiguous.value.status_code == 409
        assert ambiguous.value.code == "backup_reference_ambiguous"

        assert (await service.inspect_local_backup(first.manifest.backup_id))["id"] == (
            first.manifest.backup_id
        )
        assert (await service.inspect_local_backup(second.manifest.backup_id))["id"] == (
            second.manifest.backup_id
        )
    finally:
        service.close()


@pytest.mark.asyncio
async def test_upload_rejects_duplicate_zip_key_without_overwriting_existing_set(
    tmp_path: Path,
) -> None:
    source_one = NativeBackupService(  # type: ignore[arg-type]
        object(),
        _settings(tmp_path / "source-one"),
    )
    source_two = NativeBackupService(  # type: ignore[arg-type]
        object(),
        _settings(tmp_path / "source-two"),
    )
    destination = NativeBackupService(  # type: ignore[arg-type]
        object(),
        _settings(tmp_path / "destination"),
    )
    first_archive = None
    second_archive = None
    try:
        duplicate_name = "same-sdk-backup-key.zip"
        first = _seed_backup(
            source_one,
            "source-backup-one",
            transport_filename=duplicate_name,
        )
        second = _seed_backup(
            source_two,
            "source-backup-two",
            transport_filename=duplicate_name,
        )
        first_archive = await source_one.materialize_local_backup_zip(
            first.manifest.backup_id
        )
        second_archive = await source_two.materialize_local_backup_zip(
            second.manifest.backup_id
        )
        first_payload = b"".join(first_archive.iter_bytes(17))
        second_payload = b"".join(second_archive.iter_bytes(17))

        with destination.mutation_operation() as lease:
            uploaded = await destination.upload_local_backup(
                io.BytesIO(first_payload),
                operation_lease=lease,
            )
        assert uploaded["id"] == first.manifest.backup_id
        assert uploaded["key"] == duplicate_name

        with destination.mutation_operation() as lease:
            with pytest.raises(BackupServiceError) as duplicate:
                await destination.upload_local_backup(
                    io.BytesIO(second_payload),
                    operation_lease=lease,
                )
        assert duplicate.value.status_code == 409
        assert duplicate.value.code == "backup_reference_conflict"

        listed = await destination.list_local_backups()
        assert [(item["id"], item["key"]) for item in listed] == [
            (first.manifest.backup_id, duplicate_name)
        ]
    finally:
        if first_archive is not None:
            first_archive.close()
        if second_archive is not None:
            second_archive.close()
        source_one.close()
        source_two.close()
        destination.close()


@pytest.mark.asyncio
async def test_create_rejects_duplicate_zip_key_before_database_backup_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    try:
        duplicate_name = "existing-sdk-backup-key.zip"
        _seed_backup(
            service,
            "existing-internal-id",
            transport_filename=duplicate_name,
        )

        def unexpected_dump_configuration():
            raise AssertionError("duplicate ZIP key must fail before pg_dump setup")

        monkeypatch.setattr(
            service,
            "_dump_configuration",
            unexpected_dump_configuration,
        )
        with pytest.raises(BackupServiceError) as duplicate:
            await service.create_local_backup(
                transport_filename=duplicate_name,
            )
        assert duplicate.value.status_code == 409
        assert duplicate.value.code == "backup_reference_conflict"
        assert [item["id"] for item in await service.list_local_backups()] == [
            "existing-internal-id"
        ]
    finally:
        service.close()


@pytest.mark.asyncio
async def test_sdk_restore_reuses_one_global_and_backup_lease_for_all_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    service = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    contender = NativeBackupService(object(), settings)  # type: ignore[arg-type]
    sdk_key = "retained-sdk-restore.zip"
    backup_id = "retained-sdk-restore"
    _seed_backup(
        service,
        backup_id,
        transport_filename=sdk_key,
    )
    phases: list[str] = []
    operation_lease = None
    plan = SimpleNamespace(plan_id="a" * 32, backup_id=backup_id)

    async def fake_create(
        selected_backup_id,
        *,
        jwt_secret_mode,
        actor_id,
        backup_lease,
    ):
        phases.append("plan")
        assert selected_backup_id == backup_id
        assert jwt_secret_mode == "disaster_recovery"
        assert actor_id == "admin_1"
        backup_lease.verify_attached()
        with pytest.raises(BackupInUseError):
            with contender.operations.backup_exclusive(backup_id):
                pass
        return {
            "id": plan.plan_id,
            "planHash": "b" * 64,
            "status": "planned",
        }

    async def fake_execute(
        plan_id,
        *,
        expected_plan_hash,
        operation_leases,
    ):
        phases.append("execute")
        assert plan_id == plan.plan_id
        assert expected_plan_hash == "b" * 64
        assert operation_leases[0] is operation_lease
        operation_leases[0].verify_attached()
        operation_leases[1].verify_attached()
        return {"status": "validated"}

    async def fake_activate(
        selected_plan,
        *,
        expected_plan_hash,
        actor_id,
        operation_leases,
    ):
        phases.append("activate")
        assert selected_plan is plan
        assert expected_plan_hash == "b" * 64
        assert actor_id == "admin_1"
        assert operation_leases[0] is operation_lease
        operation_leases[0].verify_attached()
        operation_leases[1].verify_attached()
        return {"activationId": "c" * 32, "status": "restart_scheduled"}

    monkeypatch.setattr(service, "_create_staging_plan_under_lease", fake_create)
    monkeypatch.setattr(service, "_execute_staging_plan_under_lease", fake_execute)
    monkeypatch.setattr(service, "_activate_staging_plan_under_lease", fake_activate)
    monkeypatch.setattr(service.plans, "inspect", lambda _plan_id: plan)
    try:
        with service.mutation_operation() as retained_lease:
            operation_lease = retained_lease
            result = await service.restore_local_backup(
                sdk_key,
                actor_id="admin_1",
                operation_lease=retained_lease,
            )
        assert result == {
            "activationId": "c" * 32,
            "status": "restart_scheduled",
        }
        assert phases == ["plan", "execute", "activate"]
    finally:
        contender.close()
        service.close()


@pytest.mark.asyncio
async def test_validated_plan_blocks_delete_until_explicit_abandonment(
    tmp_path: Path,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    try:
        inspection = _seed_backup(service, "validated-plan-backup")
        plan = _seed_validated_plan(service, inspection.manifest.backup_id)

        with pytest.raises(BackupServiceError) as in_use:
            await service.delete_local_backup(inspection.manifest.backup_id)
        assert in_use.value.status_code == 409
        assert in_use.value.code == "backup_in_use"
        assert (inspection.path / "SEALED").is_file()

        abandoned = await service.abandon_staging_plan(
            plan.plan_id,
            expected_plan_hash=plan.plan_hash,
            actor_id="admin_2",
        )
        assert abandoned["status"] == "abandoned"
        assert abandoned["previousStatus"] == "validated"
        assert service.plans.inspect(plan.plan_id).status_data["actorId"] == (
            "admin_2"
        )

        await service.delete_local_backup(inspection.manifest.backup_id)
        assert not inspection.path.exists()
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("activation_status", ["healthy", "rolled_back"])
async def test_terminal_activation_releases_validated_plan_backup_for_delete(
    tmp_path: Path,
    activation_status: str,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    try:
        inspection = _seed_backup(
            service,
            f"terminal-activation-{activation_status}",
        )
        plan = _seed_validated_plan(service, inspection.manifest.backup_id)
        activation_id = _seed_activation_status(service, plan, activation_status)
        assert service.activations.inspect(activation_id)["status"] == activation_status

        await service.delete_local_backup(inspection.manifest.backup_id)

        assert not inspection.path.exists()
        assert service.plans.inspect(plan.plan_id).status == "validated"
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activation_status",
    ["restart_scheduled", "starting", "rollback_pending"],
)
async def test_in_progress_activation_blocks_backup_delete_and_plan_abandonment(
    tmp_path: Path,
    activation_status: str,
) -> None:
    service = NativeBackupService(object(), _settings(tmp_path))  # type: ignore[arg-type]
    try:
        inspection = _seed_backup(
            service,
            f"in-progress-activation-{activation_status}",
        )
        plan = _seed_validated_plan(service, inspection.manifest.backup_id)
        activation_id = _seed_activation_status(service, plan, activation_status)

        with pytest.raises(BackupServiceError) as in_use:
            await service.delete_local_backup(inspection.manifest.backup_id)
        assert in_use.value.status_code == 409
        assert in_use.value.code == "backup_in_use"

        with pytest.raises(BackupServiceError) as active:
            await service.abandon_staging_plan(
                plan.plan_id,
                expected_plan_hash=plan.plan_hash,
                actor_id="admin_2",
            )
        assert active.value.status_code == 409
        assert active.value.code == "backup_activation_in_progress"

        assert service.activations.inspect(activation_id)["status"] == (
            activation_status
        )
        assert service.plans.inspect(plan.plan_id).status == "validated"
        assert (inspection.path / "SEALED").is_file()
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

    inspection = _seed_backup(service, "uncertain-delete")
    monkeypatch.setattr(service.store, "delete_set", fail_delete)
    try:
        with pytest.raises(BackupServiceError) as failure:
            await service.delete_local_backup(inspection.manifest.backup_id)

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
            if kwargs.get("verify_resources") is True:
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
