from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path

import pytest

from ppbase.backup import plans as plans_module
from ppbase.backup.plans import StagingPlanError, StagingPlanStore
from ppbase.backup.service import (
    BackupServiceError,
    NativeBackupService,
    _abort_partial_backup_quiescent,
    _finalize_backup_atomically,
    _to_thread_quiescent,
)
from ppbase.backup.storage import BackupSealGate, LocalBackupStore
from ppbase.backup.models import BackupStateError, canonical_json_bytes


_PLAN_DOMAIN = b"PPBASE-RESTORE-STAGING-PLAN-V1\0"
_DESTINATION_FINGERPRINT = "9" * 64


def test_staging_plan_is_server_generated_hashed_and_single_use(tmp_path: Path) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id="admin_1",
    )

    assert plan.status == "planned"
    assert plan.destination_fingerprint_sha256 == _DESTINATION_FINGERPRINT
    assert plan.target_database.startswith("ppbase_stage_")
    assert Path(plan.target_data_dir).parent.parent == (tmp_path / "staging")
    assert not Path(plan.target_data_dir).exists()

    running = store.begin_execution(
        plan.plan_id,
        expected_plan_hash=plan.plan_hash,
    )
    assert running.status == "running"
    with pytest.raises(StagingPlanError, match="executable|already"):
        store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)

    validated = store.finish(
        plan.plan_id,
        status="validated",
        expected_attempt_id=running.status_data["attemptId"],
        data={"validation": {"database": True, "files": True}},
    )
    assert validated.status == "validated"
    assert validated.status_data["validation"]["files"] is True
    with pytest.raises(StagingPlanError, match="finish|terminal"):
        store.finish(
            plan.plan_id,
            status="quarantined",
            expected_attempt_id=running.status_data["attemptId"],
        )


def test_staging_plan_hash_tamper_is_rejected(tmp_path: Path) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="b" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="disaster_recovery",
        actor_id=None,
    )
    plan_path = tmp_path / "control" / "plans" / plan.plan_id / "plan.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["targetDatabase"] = "attacker_database"
    plan_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(StagingPlanError, match="hash"):
        store.inspect(plan.plan_id)


def test_staging_plan_rejects_invalid_mode_and_plan_hash(tmp_path: Path) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    with pytest.raises(StagingPlanError, match="jwtSecretMode"):
        store.create(
            backup_id="backup_1",
            manifest_sha256="c" * 64,
            destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
            jwt_secret_mode="active_cutover",
            actor_id=None,
        )

    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="c" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    with pytest.raises(StagingPlanError, match="planHash"):
        store.begin_execution(plan.plan_id, expected_plan_hash="0" * 64)


def test_rehashed_plan_cannot_redirect_generated_targets(tmp_path: Path) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="d" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    plan_path = tmp_path / "control" / "plans" / plan.plan_id / "plan.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["targetDataDir"] = str(
        tmp_path / "staging" / plan.plan_id / "data" / ".." / ".." / "active"
    )
    immutable = dict(payload)
    immutable.pop("planHash")
    payload["planHash"] = hashlib.sha256(
        _PLAN_DOMAIN + canonical_json_bytes(immutable)
    ).hexdigest()
    plan_path.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(StagingPlanError, match="data target"):
        store.inspect(plan.plan_id)


def test_running_marker_binds_the_hash_checked_for_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="e" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    plan_dir = tmp_path / "control" / "plans" / plan.plan_id
    plan_path = plan_dir / "plan.json"
    real_fsync_directory = plans_module._fsync_directory
    mutated = False

    def mutate_after_running_marker(path: Path) -> None:
        nonlocal mutated
        if Path(path) == plan_dir and (plan_dir / "RUNNING").exists() and not mutated:
            mutated = True
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            payload["jwtSecretMode"] = "disaster_recovery"
            immutable = dict(payload)
            immutable.pop("planHash")
            payload["planHash"] = hashlib.sha256(
                _PLAN_DOMAIN + canonical_json_bytes(immutable)
            ).hexdigest()
            plan_path.write_bytes(canonical_json_bytes(payload))
        real_fsync_directory(Path(path))

    monkeypatch.setattr(
        plans_module,
        "_fsync_directory",
        mutate_after_running_marker,
    )

    with pytest.raises(StagingPlanError, match="running marker"):
        store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)


def test_running_marker_fsync_failure_releases_lease_for_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="1" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    plan_dir = tmp_path / "control" / "plans" / plan.plan_id

    real_fsync_directory = plans_module._fsync_directory

    def fail_once(path: Path) -> None:
        if Path(path) == plan_dir and (plan_dir / "RUNNING").exists():
            raise OSError("synthetic fsync failure")
        real_fsync_directory(Path(path))

    monkeypatch.setattr(plans_module, "_fsync_directory", fail_once)
    with pytest.raises(OSError, match="synthetic"):
        store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    monkeypatch.setattr(plans_module, "_fsync_directory", real_fsync_directory)

    assert plan.plan_id not in store._execution_leases
    reconciled = store.inspect(plan.plan_id)
    assert reconciled.status == "quarantined"
    assert reconciled.status_data["failureCode"] == "staging_owner_lost"
    with pytest.raises(StagingPlanError, match="executable|already"):
        store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)


@pytest.mark.asyncio
async def test_cancelled_partial_abort_surfaces_cleanup_failure() -> None:
    started = threading.Event()
    release = threading.Event()

    class FailingBuilder:
        def abort(self) -> None:
            started.set()
            release.wait(timeout=5)
            raise BackupStateError("synthetic partial cleanup failure")

    task = asyncio.create_task(
        _abort_partial_backup_quiescent(FailingBuilder())
    )
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()

    with pytest.raises(BackupServiceError) as error:
        await task
    assert error.value.code == "backup_partial_cleanup_failed"


@pytest.mark.asyncio
async def test_cancelled_execution_is_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="f" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    service = object.__new__(NativeBackupService)
    service.plans = store
    started = asyncio.Event()

    async def wait_until_cancelled(_plan):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_execute_started_plan", wait_until_cancelled)
    task = asyncio.create_task(
        service.execute_staging_plan(
            plan.plan_id,
            expected_plan_hash=plan.plan_hash,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    terminal = store.inspect(plan.plan_id)
    assert terminal.status == "quarantined"
    assert terminal.status_data["failureCode"] == "staging_cancelled"


@pytest.mark.asyncio
async def test_cancelled_quarantine_write_failure_releases_execution_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="0" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    service = object.__new__(NativeBackupService)
    service.plans = store
    started = asyncio.Event()

    async def wait_until_cancelled(_plan):
        started.set()
        await asyncio.Event().wait()

    real_finish = store.finish

    def fail_quarantine(*args, **kwargs):
        if kwargs.get("status") == "quarantined":
            raise OSError("synthetic quarantine persistence failure")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(service, "_execute_started_plan", wait_until_cancelled)
    monkeypatch.setattr(store, "finish", fail_quarantine)
    task = asyncio.create_task(
        service.execute_staging_plan(
            plan.plan_id,
            expected_plan_hash=plan.plan_hash,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert plan.plan_id not in store._execution_leases
    reconciled = store.inspect(plan.plan_id)
    assert reconciled.status == "quarantined"
    assert reconciled.status_data["failureCode"] == "staging_owner_lost"


@pytest.mark.asyncio
async def test_quarantine_persistence_failure_is_not_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="2" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    service = object.__new__(NativeBackupService)
    service.plans = store

    async def fail_execution(_plan):
        raise RuntimeError("synthetic staging failure")

    real_finish = store.finish

    def fail_quarantine(*args, **kwargs):
        if kwargs.get("status") == "quarantined":
            raise OSError("synthetic persistence failure")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(service, "_execute_started_plan", fail_execution)
    monkeypatch.setattr(store, "finish", fail_quarantine)

    with pytest.raises(BackupServiceError) as error:
        await service.execute_staging_plan(
            plan.plan_id,
            expected_plan_hash=plan.plan_hash,
        )
    assert getattr(error.value, "code", None) == (
        "staging_quarantine_persistence_failed"
    )
    assert plan.plan_id not in store._execution_leases
    reconciled = store.inspect(plan.plan_id)
    assert reconciled.status == "quarantined"
    assert reconciled.status_data["failureCode"] == "staging_owner_lost"


def test_orphaned_running_attempt_is_quarantined_on_reconciliation(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    first_store = StagingPlanStore(control, staging)
    plan = first_store.create(
        backup_id="backup_1",
        manifest_sha256="3" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = first_store.begin_execution(
        plan.plan_id,
        expected_plan_hash=plan.plan_hash,
    )
    attempt_id = running.status_data["attemptId"]

    # Closing the process-owned flock without a terminal status simulates a
    # crash after RUNNING became durable.
    first_store._release_execution_lease(plan.plan_id, attempt_id)
    recovered = StagingPlanStore(control, staging).inspect(plan.plan_id)

    assert recovered.status == "quarantined"
    assert recovered.status_data["failureCode"] == "staging_owner_lost"
    assert recovered.status_data["attemptId"] == attempt_id


@pytest.mark.asyncio
async def test_blocking_worker_reaches_quiescence_before_cancellation_propagates() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def worker() -> None:
        started.set()
        release.wait(timeout=5)
        finished.set()

    task = asyncio.create_task(_to_thread_quiescent(worker))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert not finished.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_seal_commit_point_wins_or_loses_cancellation_atomically() -> None:
    before_commit = threading.Event()
    allow_commit = threading.Event()

    def cancellable_worker(*, seal_gate: BackupSealGate) -> str:
        before_commit.set()
        allow_commit.wait(timeout=5)
        seal_gate.publish(lambda: None)
        return "sealed"

    first_gate = BackupSealGate()
    cancelled = asyncio.create_task(
        _finalize_backup_atomically(
            cancellable_worker,
            seal_gate=first_gate,
        )
    )
    await asyncio.to_thread(before_commit.wait, 2)
    cancelled.cancel()
    await asyncio.sleep(0.05)
    assert not cancelled.done()
    allow_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    committed = threading.Event()
    allow_return = threading.Event()

    def committed_worker(*, seal_gate: BackupSealGate) -> str:
        seal_gate.publish(committed.set)
        allow_return.wait(timeout=5)
        return "sealed"

    second_gate = BackupSealGate()
    completed = asyncio.create_task(
        _finalize_backup_atomically(
            committed_worker,
            seal_gate=second_gate,
        )
    )
    await asyncio.to_thread(committed.wait, 2)
    completed.cancel()
    allow_return.set()
    assert await completed == "sealed"


@pytest.mark.asyncio
async def test_cancelled_finalize_does_not_hide_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    builder = store.begin_set("cancelled-cleanup-failure")
    builder.database_dump_path.write_bytes(b"PGDMP cleanup failure")
    prepared = builder.prepare()
    gate = BackupSealGate()
    publish_entered = threading.Event()
    allow_publish = threading.Event()
    real_publish = gate.publish

    def blocked_publish(callback) -> None:
        publish_entered.set()
        allow_publish.wait(timeout=5)
        real_publish(callback)

    def fail_cleanup(_path: Path, _identity: tuple[int, int]) -> None:
        raise BackupStateError("synthetic finalization cleanup failure")

    monkeypatch.setattr(gate, "publish", blocked_publish)
    monkeypatch.setattr(store, "_remove_owned_directory", fail_cleanup)
    task = asyncio.create_task(
        _finalize_backup_atomically(
            store.finalize_set,
            prepared,
            seal_gate=gate,
        )
    )
    await asyncio.to_thread(publish_entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0.05)
    assert gate._cancelled is True
    allow_publish.set()

    with pytest.raises(
        BackupStateError,
        match="could not be cleaned safely",
    ):
        await task
