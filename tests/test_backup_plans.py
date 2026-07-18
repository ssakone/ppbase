from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import ppbase.backup.control as control_module
import ppbase.backup.identity as identity_module
import ppbase.backup.service as backup_service_module
from ppbase.backup import plans as plans_module
from ppbase.backup.control import ControlPlaneRoot, ControlPlaneSafetyError
from ppbase.backup.identity import (
    PRIVATE_KEY_FILENAME,
    BackupIdentity,
    BackupIdentityError,
)
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


def test_plan_store_rejects_non_private_control_root_without_repairing_it(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o755)
    os.chmod(control, 0o755)

    with pytest.raises(StagingPlanError, match="control.*0700|private"):
        StagingPlanStore(control, tmp_path / "staging")

    assert stat.S_IMODE(control.stat().st_mode) == 0o755
    assert not (control / "plans").exists()


def test_plan_store_rejects_symlinked_control_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-control"
    outside.mkdir(mode=0o700)
    control = tmp_path / "control"
    control.symlink_to(outside, target_is_directory=True)

    with pytest.raises(StagingPlanError, match="control.*symlink|safe|private"):
        StagingPlanStore(control, tmp_path / "staging")

    assert list(outside.iterdir()) == []


def test_control_root_refuses_creation_below_nonsticky_writable_ancestor(
    tmp_path: Path,
) -> None:
    writable_parent = tmp_path / "world-writable"
    writable_parent.mkdir(mode=0o777)
    os.chmod(writable_parent, 0o777)
    control = writable_parent / "control"

    with pytest.raises(ControlPlaneSafetyError, match="non-sticky writable"):
        ControlPlaneRoot.open(control)

    assert not control.exists()


def test_control_root_refuses_foreign_owned_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor = tmp_path / "foreign-owned"
    ancestor.mkdir(mode=0o755)
    os.chmod(ancestor, 0o755)
    ancestor_info = ancestor.stat()
    ancestor_identity = (ancestor_info.st_dev, ancestor_info.st_ino)
    foreign_uid = 1 if os.geteuid() != 1 else 2
    real_fstat = control_module.os.fstat

    def foreign_owner(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        if (info.st_dev, info.st_ino) != ancestor_identity:
            return info
        values = list(info)
        values[stat.ST_UID] = foreign_uid
        return os.stat_result(values)

    monkeypatch.setattr(control_module.os, "fstat", foreign_owner)

    with pytest.raises(ControlPlaneSafetyError, match="root|service user"):
        ControlPlaneRoot.open(ancestor / "control")

    assert not (ancestor / "control").exists()


def test_control_root_detects_ancestor_made_writable(
    tmp_path: Path,
) -> None:
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o700)
    control = ancestor / "control"
    control.mkdir(mode=0o700)
    root = ControlPlaneRoot.open(control)

    os.chmod(ancestor, 0o777)
    try:
        with pytest.raises(ControlPlaneSafetyError, match="non-sticky writable"):
            root.verify_attached()
    finally:
        os.chmod(ancestor, 0o700)
        root.close()


def test_control_root_detects_detached_ancestor_chain(
    tmp_path: Path,
) -> None:
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o700)
    control = ancestor / "control"
    control.mkdir(mode=0o700)
    root = ControlPlaneRoot.open(control)
    detached = tmp_path / "detached-ancestor"
    ancestor.rename(detached)
    ancestor.mkdir(mode=0o700)
    (ancestor / "control").mkdir(mode=0o700)

    try:
        with pytest.raises(ControlPlaneSafetyError, match="detached|substituted"):
            root.verify_attached()
    finally:
        root.close()


def test_control_root_detects_rename_and_same_mode_substitution(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    root = ControlPlaneRoot.open(control)
    detached = tmp_path / "detached-control"
    control.rename(detached)
    control.mkdir(mode=0o700)

    try:
        with pytest.raises(ControlPlaneSafetyError, match="detached|substituted"):
            root.verify_attached()
    finally:
        root.close()


def test_native_backup_service_does_not_resolve_symlinked_control_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-control"
    outside.mkdir(mode=0o700)
    control = tmp_path / "control"
    control.symlink_to(outside, target_is_directory=True)
    settings = SimpleNamespace(
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(control),
        backup_staging_root=str(tmp_path / "staging"),
        data_dir=str(tmp_path / "active-data"),
    )

    with pytest.raises(BackupServiceError) as error:
        NativeBackupService(object(), settings)

    assert error.value.code == "backup_control_invalid"
    assert list(outside.iterdir()) == []


def test_native_backup_service_rejects_detached_root_before_identity_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    detached = tmp_path / "detached-control"
    outside = tmp_path / "outside-control"
    outside.mkdir(mode=0o700)
    settings = SimpleNamespace(
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(control),
        backup_staging_root=str(tmp_path / "staging"),
        data_dir=str(tmp_path / "active-data"),
    )
    real_load_or_create_at = BackupIdentity.load_or_create_at.__func__
    substituted = False

    def substitute_root_before_identity(
        cls,
        control_root,
        directory_name: str = "identity",
    ):
        nonlocal substituted
        if not substituted:
            substituted = True
            control.rename(detached)
            control.symlink_to(outside, target_is_directory=True)
        return real_load_or_create_at(cls, control_root, directory_name)

    monkeypatch.setattr(
        BackupIdentity,
        "load_or_create_at",
        classmethod(substitute_root_before_identity),
    )

    with pytest.raises(BackupServiceError) as error:
        NativeBackupService(object(), settings)

    assert error.value.code == "backup_identity_invalid"
    assert substituted is True
    assert list(outside.iterdir()) == []
    assert (detached / "plans").is_dir()
    assert not (detached / "identity").exists()

    control.unlink()
    control.mkdir(mode=0o700)
    canonical_service = NativeBackupService(object(), settings)
    try:
        assert (control / "identity" / PRIVATE_KEY_FILENAME).is_file()
        assert canonical_service.get_identity()["fingerprintSha256"]
        assert not (detached / "identity").exists()
    finally:
        canonical_service.close()


@pytest.mark.asyncio
async def test_native_backup_service_rejects_detached_root_after_construction(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    detached = tmp_path / "detached-control"
    settings = SimpleNamespace(
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(control),
        backup_staging_root=str(tmp_path / "staging"),
        data_dir=str(tmp_path / "active-data"),
    )
    stale_service = NativeBackupService(object(), settings)
    original_fingerprint = stale_service.get_identity()["fingerprintSha256"]
    control.rename(detached)
    control.mkdir(mode=0o700)
    replacement_service: NativeBackupService | None = None

    try:
        with pytest.raises(BackupServiceError) as identity_error:
            stale_service.get_identity()
        with pytest.raises(BackupServiceError) as listing_error:
            await stale_service.list_local_backups()

        replacement_service = NativeBackupService(object(), settings)
        replacement_fingerprint = replacement_service.get_identity()[
            "fingerprintSha256"
        ]
    finally:
        stale_service.close()
        if replacement_service is not None:
            replacement_service.close()

    assert identity_error.value.code == "backup_control_detached"
    assert listing_error.value.code == "backup_control_detached"
    assert replacement_fingerprint != original_fingerprint


def test_native_backup_service_rejects_detached_identity_after_construction(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    settings = SimpleNamespace(
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(control),
        backup_staging_root=str(tmp_path / "staging"),
        data_dir=str(tmp_path / "active-data"),
    )
    stale_service = NativeBackupService(object(), settings)
    original_fingerprint = stale_service.get_identity()["fingerprintSha256"]
    identity_dir = control / "identity"
    detached_identity = tmp_path / "detached-identity"
    identity_dir.rename(detached_identity)
    identity_dir.mkdir(mode=0o700)
    replacement_service: NativeBackupService | None = None

    try:
        with pytest.raises(BackupServiceError) as error:
            stale_service.get_identity()
        replacement_service = NativeBackupService(object(), settings)
        replacement_fingerprint = replacement_service.get_identity()[
            "fingerprintSha256"
        ]
    finally:
        stale_service.close()
        if replacement_service is not None:
            replacement_service.close()

    assert error.value.code == "backup_identity_invalid"
    assert replacement_fingerprint != original_fingerprint


def test_native_backup_service_rejects_substituted_identity_key(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    settings = SimpleNamespace(
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(control),
        backup_staging_root=str(tmp_path / "staging"),
        data_dir=str(tmp_path / "active-data"),
    )
    stale_service = NativeBackupService(object(), settings)
    original_fingerprint = stale_service.get_identity()["fingerprintSha256"]
    key_path = control / "identity" / PRIVATE_KEY_FILENAME
    key_path.rename(control / "identity" / "detached-signing-ed25519.key")
    replacement_service: NativeBackupService | None = None

    try:
        replacement_service = NativeBackupService(object(), settings)
        replacement_fingerprint = replacement_service.get_identity()[
            "fingerprintSha256"
        ]
        with pytest.raises(BackupServiceError) as error:
            stale_service.get_identity()
    finally:
        stale_service.close()
        if replacement_service is not None:
            replacement_service.close()

    assert error.value.code == "backup_identity_invalid"
    assert replacement_fingerprint != original_fingerprint


def test_service_never_recreates_missing_identity_for_sealed_backups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    backup_root = tmp_path / "backups"
    settings = SimpleNamespace(
        backup_root=str(backup_root),
        backup_control_dir=str(control),
        backup_staging_root=str(tmp_path / "staging"),
        data_dir=str(tmp_path / "active-data"),
    )
    initial_service = NativeBackupService(object(), settings)
    initial_service.close()
    sealed = backup_root / "sets" / "existing-sealed"
    sealed.mkdir(mode=0o700)
    (sealed / "SEALED").write_bytes(b"")
    key_path = control / "identity" / PRIVATE_KEY_FILENAME
    detached_key = control / "identity" / "detached-signing-ed25519.key"
    real_load_existing = BackupIdentity.load_existing_at.__func__
    detached = False

    def detach_before_strict_load(
        cls,
        control_root,
        directory_name: str = "identity",
    ):
        nonlocal detached
        if not detached:
            detached = True
            key_path.rename(detached_key)
        return real_load_existing(cls, control_root, directory_name)

    monkeypatch.setattr(
        BackupIdentity,
        "load_existing_at",
        classmethod(detach_before_strict_load),
    )

    with pytest.raises(BackupServiceError) as error:
        NativeBackupService(object(), settings)

    assert detached is True
    assert error.value.code == "backup_identity_missing"
    assert detached_key.is_file()
    assert not key_path.exists()


def test_identity_creation_rejects_detached_identity_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    root = ControlPlaneRoot.open(control)
    detached_identity = tmp_path / "detached-identity"
    identity_path = control / "identity"
    real_load_private_key = identity_module._load_or_create_private_key_at

    def detach_after_key_write(directory_fd: int):
        key = real_load_private_key(directory_fd)
        identity_path.rename(detached_identity)
        identity_path.mkdir(mode=0o700)
        return key

    monkeypatch.setattr(
        identity_module,
        "_load_or_create_private_key_at",
        detach_after_key_write,
    )
    try:
        with pytest.raises(BackupIdentityError, match="detached|substituted"):
            BackupIdentity.load_or_create_at(root)
    finally:
        monkeypatch.setattr(
            identity_module,
            "_load_or_create_private_key_at",
            real_load_private_key,
        )

    detached = BackupIdentity.load_or_create(detached_identity)
    canonical = BackupIdentity.load_or_create_at(root)
    detached_fingerprint = detached.fingerprint_sha256
    canonical_fingerprint = canonical.fingerprint_sha256
    detached.close()
    canonical.close()
    root.close()

    assert (detached_identity / PRIVATE_KEY_FILENAME).is_file()
    assert (identity_path / PRIVATE_KEY_FILENAME).is_file()
    assert detached_fingerprint != canonical_fingerprint


def test_attached_identity_refuses_to_sign_after_detachment(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    root = ControlPlaneRoot.open(control)
    identity = BackupIdentity.load_or_create_at(root)
    identity_path = control / "identity"
    detached_identity = tmp_path / "detached-identity"
    identity_path.rename(detached_identity)
    identity_path.mkdir(mode=0o700)

    try:
        with pytest.raises(BackupIdentityError, match="detached|substituted"):
            identity.sign_manifest(b"manifest")
    finally:
        identity.close()
        root.close()


def test_native_backup_service_close_is_idempotent_and_closes_control_fds(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        data_dir=str(tmp_path / "active-data"),
    )
    service = NativeBackupService(object(), settings)
    root = service.control_root
    identity = service.identity

    service.close()
    service.close()

    with pytest.raises(ControlPlaneSafetyError, match="closed"):
        root.fileno()
    with pytest.raises(BackupIdentityError, match="closed|no live"):
        identity.verify_attached()
    with pytest.raises(BackupServiceError) as error:
        service.get_identity()
    assert error.value.code == "backup_service_closed"


def test_native_backup_service_constructor_failure_closes_control_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        data_dir=str(tmp_path / "active-data"),
    )
    captured: dict[str, ControlPlaneRoot] = {}

    class FailingPlanStore:
        def __init__(self, control_root, _staging_root):
            captured["root"] = control_root
            raise RuntimeError("synthetic plan-store failure")

    monkeypatch.setattr(
        backup_service_module,
        "StagingPlanStore",
        FailingPlanStore,
    )

    with pytest.raises(RuntimeError, match="synthetic plan-store"):
        NativeBackupService(object(), settings)

    with pytest.raises(ControlPlaneSafetyError, match="closed"):
        captured["root"].fileno()


def test_plan_store_rejects_symlinked_plans_without_touching_target(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    outside = tmp_path / "outside-plans"
    outside.mkdir(mode=0o755)
    os.chmod(outside, 0o755)
    (control / "plans").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StagingPlanError, match="plans.*symlink|safe|private"):
        StagingPlanStore(control, tmp_path / "staging")

    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert list(outside.iterdir()) == []


def test_plan_store_rejects_plans_symlink_substituted_after_initialization(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    original_plans = control / "original-plans"
    (control / "plans").rename(original_plans)
    outside = tmp_path / "outside-plans"
    outside.mkdir(mode=0o700)
    (control / "plans").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StagingPlanError, match="plans.*safe|private"):
        store.create(
            backup_id="backup_1",
            manifest_sha256="a" * 64,
            destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
            jwt_secret_mode="clone",
            actor_id=None,
        )

    assert list(outside.iterdir()) == []
    assert list(original_plans.iterdir()) == []


def test_plan_creation_race_writes_only_to_pinned_plan_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    outside = tmp_path / "outside-plan"
    outside.mkdir(mode=0o700)
    detached = control / "detached-plan"
    real_write_exclusive_at = plans_module._write_exclusive_at
    swapped = False

    def substitute_before_first_write(
        parent_fd: int,
        name: str,
        payload: bytes,
        *,
        keep_open: bool = False,
    ) -> int | None:
        nonlocal swapped
        if name == "plan.json" and not swapped:
            swapped = True
            entries = list((control / "plans").iterdir())
            assert len(entries) == 1
            visible_plan = entries[0]
            visible_plan.rename(detached)
            visible_plan.symlink_to(outside, target_is_directory=True)
        return real_write_exclusive_at(
            parent_fd,
            name,
            payload,
            keep_open=keep_open,
        )

    monkeypatch.setattr(
        plans_module,
        "_write_exclusive_at",
        substitute_before_first_write,
    )

    with pytest.raises(StagingPlanError, match="detached|substituted"):
        store.create(
            backup_id="backup_1",
            manifest_sha256="a" * 64,
            destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
            jwt_secret_mode="clone",
            actor_id=None,
        )

    assert list(outside.iterdir()) == []
    assert (detached / "plan.json").is_file()
    assert not (detached / "SEALED").exists()
    assert not tuple(detached.glob(".SEALED-*.tmp"))


def test_plan_inspection_rejects_plan_directory_symlink(tmp_path: Path) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    visible_plan = control / "plans" / plan.plan_id
    detached = control / "detached-plan"
    visible_plan.rename(detached)
    outside = tmp_path / "outside-plan"
    outside.mkdir(mode=0o700)
    visible_plan.symlink_to(outside, target_is_directory=True)

    with pytest.raises(StagingPlanError, match="not found|unsafe"):
        store.inspect(plan.plan_id)

    assert list(outside.iterdir()) == []


def test_plan_inspection_rejects_symlinked_internal_file(tmp_path: Path) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    plan_path = control / "plans" / plan.plan_id / "plan.json"
    outside = tmp_path / "outside-plan.json"
    outside.write_bytes(plan_path.read_bytes())
    os.chmod(outside, 0o600)
    expected = outside.read_bytes()
    plan_path.unlink()
    plan_path.symlink_to(outside)

    with pytest.raises(StagingPlanError, match="unreadable|private regular"):
        store.inspect(plan.plan_id)

    assert outside.read_bytes() == expected


def test_finish_rejects_substituted_plan_and_releases_pinned_descriptors(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    visible_plan = control / "plans" / plan.plan_id
    detached = control / "detached-running-plan"
    visible_plan.rename(detached)
    outside = tmp_path / "outside-running-plan"
    outside.mkdir(mode=0o700)
    visible_plan.symlink_to(outside, target_is_directory=True)

    try:
        with pytest.raises(StagingPlanError, match="detached|substituted"):
            store.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
            )
    finally:
        store.abandon_execution(
            plan.plan_id,
            expected_attempt_id=attempt_id,
        )

    assert plan.plan_id not in store._execution_leases
    assert list(outside.iterdir()) == []
    assert not (detached / "terminal.json").exists()


def test_finish_rejects_plan_detached_during_terminal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    visible_plan = control / "plans" / plan.plan_id
    detached = control / "detached-published-plan"
    real_publish = plans_module._publish_terminal_at

    def detach_then_publish(parent_fd: int, payload: bytes, **kwargs) -> None:
        visible_plan.rename(detached)
        visible_plan.mkdir(mode=0o700)
        real_publish(parent_fd, payload, **kwargs)

    monkeypatch.setattr(plans_module, "_publish_terminal_at", detach_then_publish)
    try:
        with pytest.raises(StagingPlanError, match="detached|substituted"):
            store.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
            )
    finally:
        if plan.plan_id in store._execution_leases:
            store.abandon_execution(
                plan.plan_id,
                expected_attempt_id=attempt_id,
            )

    assert not (detached / "terminal.json").exists()
    assert not tuple(detached.glob(".terminal-*.tmp"))
    assert not (visible_plan / "terminal.json").exists()


def test_finish_rejects_plans_root_detached_during_terminal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    visible_plans = control / "plans"
    detached_plans = control / "detached-plans"
    real_publish = plans_module._publish_terminal_at

    def detach_then_publish(parent_fd: int, payload: bytes, **kwargs) -> None:
        visible_plans.rename(detached_plans)
        visible_plans.mkdir(mode=0o700)
        real_publish(parent_fd, payload, **kwargs)

    monkeypatch.setattr(plans_module, "_publish_terminal_at", detach_then_publish)
    try:
        with pytest.raises(StagingPlanError, match="detached|substituted"):
            store.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
            )
    finally:
        if plan.plan_id in store._execution_leases:
            store.abandon_execution(
                plan.plan_id,
                expected_attempt_id=attempt_id,
            )

    assert not (detached_plans / plan.plan_id / "terminal.json").exists()
    assert not tuple(
        (detached_plans / plan.plan_id).glob(".terminal-*.tmp")
    )
    assert not (visible_plans / plan.plan_id / "terminal.json").exists()


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


def test_plan_create_guard_runs_at_seal_commit_point(tmp_path: Path) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    guard_called = False

    def fail_at_commit() -> None:
        nonlocal guard_called
        guard_called = True
        plan_dirs = tuple((control / "plans").iterdir())
        assert len(plan_dirs) == 1
        assert tuple(plan_dirs[0].glob(".SEALED-*.tmp"))
        assert not (plan_dirs[0] / "SEALED").exists()
        raise StagingPlanError("synthetic plan seal guard failure")

    with pytest.raises(StagingPlanError, match="synthetic plan seal"):
        store.create(
            backup_id="backup_1",
            manifest_sha256="a" * 64,
            destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
            jwt_secret_mode="clone",
            actor_id=None,
            pre_commit_guard=fail_at_commit,
        )

    plan_dirs = tuple((control / "plans").iterdir())
    assert guard_called is True
    assert len(plan_dirs) == 1
    assert not (plan_dirs[0] / "SEALED").exists()
    assert not tuple(plan_dirs[0].glob(".SEALED-*.tmp"))


def test_finish_guard_runs_at_terminal_commit_point(tmp_path: Path) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    plan_dir = control / "plans" / plan.plan_id
    guard_called = False

    def fail_at_commit() -> None:
        nonlocal guard_called
        guard_called = True
        assert tuple(plan_dir.glob(".terminal-*.tmp"))
        assert not (plan_dir / "terminal.json").exists()
        raise StagingPlanError("synthetic terminal guard failure")

    try:
        with pytest.raises(StagingPlanError, match="synthetic terminal guard"):
            store.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
                pre_commit_guard=fail_at_commit,
            )
    finally:
        if plan.plan_id in store._execution_leases:
            store.abandon_execution(
                plan.plan_id,
                expected_attempt_id=attempt_id,
            )

    assert guard_called is True
    assert not (plan_dir / "terminal.json").exists()
    assert not tuple(plan_dir.glob(".terminal-*.tmp"))


def test_oversized_terminal_status_is_rejected_before_publication(
    tmp_path: Path,
) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    plan_dir = tmp_path / "control" / "plans" / plan.plan_id

    try:
        with pytest.raises(StagingPlanError, match="size limit"):
            store.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
                data={"validation": "x" * (64 * 1024)},
            )
    finally:
        if plan.plan_id in store._execution_leases:
            store.abandon_execution(
                plan.plan_id,
                expected_attempt_id=attempt_id,
            )

    assert not (plan_dir / "terminal.json").exists()


def test_terminal_directory_fsync_failure_rolls_back_before_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StagingPlanStore(tmp_path / "control", tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    plan_dir = tmp_path / "control" / "plans" / plan.plan_id
    plan_identity = (plan_dir.stat().st_dev, plan_dir.stat().st_ino)
    real_fsync_directory = plans_module._fsync_directory
    fsync_entered = threading.Event()
    release_fsync = threading.Event()
    outcome: list[BaseException | object] = []
    failed = False

    def fail_terminal_publication_once(descriptor: int) -> None:
        nonlocal failed
        opened = os.fstat(descriptor)
        if (
            not failed
            and (opened.st_dev, opened.st_ino) == plan_identity
            and (plan_dir / "terminal.json").exists()
        ):
            failed = True
            fsync_entered.set()
            assert release_fsync.wait(timeout=5)
            raise OSError("synthetic terminal directory fsync failure")
        real_fsync_directory(descriptor)

    def finish_plan() -> None:
        try:
            outcome.append(
                store.finish(
                    plan.plan_id,
                    status="validated",
                    expected_attempt_id=attempt_id,
                )
            )
        except BaseException as exc:  # pragma: no cover - thread diagnostics
            outcome.append(exc)

    monkeypatch.setattr(
        plans_module,
        "_fsync_directory",
        fail_terminal_publication_once,
    )
    worker = threading.Thread(target=finish_plan)
    worker.start()
    try:
        assert fsync_entered.wait(timeout=5)
        assert (plan_dir / "terminal.json").exists()
        concurrent = StagingPlanStore(
            tmp_path / "control",
            tmp_path / "staging",
        ).inspect(plan.plan_id)
        assert concurrent.status == "running"
    finally:
        release_fsync.set()
        worker.join(timeout=5)
        if plan.plan_id in store._execution_leases:
            store.abandon_execution(
                plan.plan_id,
                expected_attempt_id=attempt_id,
            )

    assert failed is True
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], OSError)
    assert "synthetic terminal" in str(outcome[0])
    assert not (plan_dir / "terminal.json").exists()


def test_finish_rejects_terminal_temp_changed_before_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    store = StagingPlanStore(control, staging)
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    plan_dir = control / "plans" / plan.plan_id
    plan_identity = (plan_dir.stat().st_dev, plan_dir.stat().st_ino)
    quarantined_payload = canonical_json_bytes(
        {"attemptId": attempt_id, "status": "quarantined"}
    )
    real_fsync_directory = plans_module._fsync_directory
    injected = False

    def substitute_temporary_before_link(descriptor: int) -> None:
        nonlocal injected
        opened = os.fstat(descriptor)
        temporary_names = tuple(
            name
            for name in os.listdir(descriptor)
            if name.startswith(plans_module._TERMINAL_TEMP_PREFIX)
            and name.endswith(plans_module._TERMINAL_TEMP_SUFFIX)
        )
        if (
            not injected
            and (opened.st_dev, opened.st_ino) == plan_identity
            and temporary_names
            and not (plan_dir / "terminal.json").exists()
        ):
            injected = True
            temporary_fd = os.open(
                temporary_names[0],
                plans_module._open_flags(os.O_WRONLY),
                dir_fd=descriptor,
            )
            try:
                os.ftruncate(temporary_fd, 0)
                plans_module._write_all(temporary_fd, quarantined_payload)
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
        real_fsync_directory(descriptor)

    monkeypatch.setattr(
        plans_module,
        "_fsync_directory",
        substitute_temporary_before_link,
    )
    try:
        with pytest.raises(StagingPlanError, match="Persisted terminal"):
            store.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
            )
    finally:
        if plan.plan_id in store._execution_leases:
            store.abandon_execution(
                plan.plan_id,
                expected_attempt_id=attempt_id,
            )

    assert injected is True
    assert (plan_dir / "terminal.json").read_bytes() == quarantined_payload
    persisted = StagingPlanStore(control, staging).inspect(plan.plan_id)
    assert persisted.status == "quarantined"
    assert persisted.status_data["attemptId"] == attempt_id


def test_finish_rejects_plan_detached_during_terminal_reinspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    store = StagingPlanStore(control, tmp_path / "staging")
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    visible_plan = control / "plans" / plan.plan_id
    detached_plan = control / "detached-terminal-read-plan"
    real_read_json = plans_module._read_json_at
    detached = False

    def read_then_detach(parent_fd: int, name: str, **kwargs):
        nonlocal detached
        result = real_read_json(parent_fd, name, **kwargs)
        if name == plans_module._TERMINAL_STATUS_FILENAME and not detached:
            detached = True
            visible_plan.rename(detached_plan)
            visible_plan.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(plans_module, "_read_json_at", read_then_detach)
    try:
        with pytest.raises(StagingPlanError, match="detached|substituted"):
            store.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
            )
    finally:
        if plan.plan_id in store._execution_leases:
            store.abandon_execution(
                plan.plan_id,
                expected_attempt_id=attempt_id,
            )

    assert detached is True
    assert (detached_plan / "terminal.json").is_file()
    assert not (visible_plan / "terminal.json").exists()


def test_finish_rejects_terminal_name_substituted_while_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    store = StagingPlanStore(control, staging)
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    plan_dir = control / "plans" / plan.plan_id
    terminal_path = plan_dir / plans_module._TERMINAL_STATUS_FILENAME
    detached_terminal = plan_dir / "detached-terminal.json"
    quarantined_payload = canonical_json_bytes(
        {"attemptId": attempt_id, "status": "quarantined"}
    )
    real_read = plans_module.os.read
    substituted = False

    def substitute_terminal_name(descriptor: int, size: int) -> bytes:
        nonlocal substituted
        if not substituted and terminal_path.exists():
            opened = os.fstat(descriptor)
            visible = terminal_path.stat(follow_symlinks=False)
            if (
                opened.st_dev == visible.st_dev
                and opened.st_ino == visible.st_ino
            ):
                substituted = True
                terminal_path.rename(detached_terminal)
                terminal_path.write_bytes(quarantined_payload)
                os.chmod(terminal_path, 0o600)
        return real_read(descriptor, size)

    monkeypatch.setattr(plans_module.os, "read", substitute_terminal_name)
    try:
        with pytest.raises(StagingPlanError, match="changed while it was read"):
            store.finish(
                plan.plan_id,
                status="validated",
                expected_attempt_id=attempt_id,
            )
    finally:
        if plan.plan_id in store._execution_leases:
            store.abandon_execution(
                plan.plan_id,
                expected_attempt_id=attempt_id,
            )

    assert substituted is True
    assert detached_terminal.is_file()
    assert terminal_path.read_bytes() == quarantined_payload
    persisted = StagingPlanStore(control, staging).inspect(plan.plan_id)
    assert persisted.status == "quarantined"


def test_terminal_cleanup_error_after_commit_preserves_validated_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    store = StagingPlanStore(control, staging)
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    plan_dir = control / "plans" / plan.plan_id
    cleanup_called = False
    real_cleanup = plans_module._cleanup_terminal_temporaries_at

    def fail_cleanup_after_commit(parent_fd: int) -> int:
        nonlocal cleanup_called
        cleanup_called = True
        assert os.stat(
            "terminal.json",
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        raise OSError("synthetic post-commit cleanup failure")

    monkeypatch.setattr(
        plans_module,
        "_cleanup_terminal_temporaries_at",
        fail_cleanup_after_commit,
    )
    validated = store.finish(
        plan.plan_id,
        status="validated",
        expected_attempt_id=running.status_data["attemptId"],
    )
    monkeypatch.setattr(
        plans_module,
        "_cleanup_terminal_temporaries_at",
        real_cleanup,
    )

    assert cleanup_called is True
    assert validated.status == "validated"
    assert (plan_dir / "terminal.json").is_file()
    assert StagingPlanStore(control, staging).inspect(plan.plan_id).status == (
        "validated"
    )


def test_finish_result_comes_from_exact_canonical_terminal_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    store = StagingPlanStore(control, staging)
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    nested = {"database": True}
    data = {"note": "e\u0301", "validation": nested}
    real_publish = plans_module._publish_terminal_at

    def publish_then_mutate_input(parent_fd: int, payload: bytes, **kwargs) -> None:
        real_publish(parent_fd, payload, **kwargs)
        nested["database"] = False
        data["note"] = "changed"

    monkeypatch.setattr(
        plans_module,
        "_publish_terminal_at",
        publish_then_mutate_input,
    )
    validated = store.finish(
        plan.plan_id,
        status="validated",
        expected_attempt_id=running.status_data["attemptId"],
        data=data,
    )
    persisted = StagingPlanStore(control, staging).inspect(plan.plan_id)

    assert validated.as_dict() == persisted.as_dict()
    assert validated.status_data["note"] == "é"
    assert validated.status_data["validation"]["database"] is True


def test_terminal_name_is_not_visible_until_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    store = StagingPlanStore(control, staging)
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    plan_dir = control / "plans" / plan.plan_id
    write_started = threading.Event()
    release_write = threading.Event()
    real_write_all = plans_module._write_all
    outcome: list[object] = []

    def block_terminal_write(descriptor: int, payload: bytes) -> None:
        write_started.set()
        assert release_write.wait(timeout=5)
        real_write_all(descriptor, payload)

    def finish_plan() -> None:
        try:
            outcome.append(
                store.finish(
                    plan.plan_id,
                    status="validated",
                    expected_attempt_id=attempt_id,
                )
            )
        except BaseException as exc:  # pragma: no cover - thread diagnostics
            outcome.append(exc)

    monkeypatch.setattr(plans_module, "_write_all", block_terminal_write)
    worker = threading.Thread(target=finish_plan)
    worker.start()
    try:
        assert write_started.wait(timeout=5)
        assert not (plan_dir / "terminal.json").exists()
        assert StagingPlanStore(control, staging).inspect(plan.plan_id).status == (
            "running"
        )
    finally:
        release_write.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(outcome) == 1
    assert not isinstance(outcome[0], BaseException)
    assert outcome[0].status == "validated"


def test_complete_terminal_hardlink_survives_crash_before_temp_unlink(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    store = StagingPlanStore(control, staging)
    plan = store.create(
        backup_id="backup_1",
        manifest_sha256="a" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = store.begin_execution(plan.plan_id, expected_plan_hash=plan.plan_hash)
    attempt_id = running.status_data["attemptId"]
    plan_dir = control / "plans" / plan.plan_id
    temporary = plan_dir / ".terminal-crash.tmp"
    temporary.write_bytes(
        canonical_json_bytes(
            {
                "attemptId": attempt_id,
                "status": "validated",
            }
        )
    )
    os.chmod(temporary, 0o600)
    os.link(temporary, plan_dir / "terminal.json")

    store._release_execution_lease(plan.plan_id, attempt_id)
    recovered = StagingPlanStore(control, staging).inspect(plan.plan_id)

    assert recovered.status == "validated"
    assert recovered.status_data["attemptId"] == attempt_id
    assert not temporary.exists()


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
    plan_identity = (plan_dir.stat().st_dev, plan_dir.stat().st_ino)
    real_fsync_directory = plans_module._fsync_directory
    mutated = False

    def mutate_after_running_marker(descriptor: int) -> None:
        nonlocal mutated
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) == plan_identity
            and (plan_dir / "RUNNING").exists()
            and not mutated
        ):
            mutated = True
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            payload["jwtSecretMode"] = "disaster_recovery"
            immutable = dict(payload)
            immutable.pop("planHash")
            payload["planHash"] = hashlib.sha256(
                _PLAN_DOMAIN + canonical_json_bytes(immutable)
            ).hexdigest()
            plan_path.write_bytes(canonical_json_bytes(payload))
        real_fsync_directory(descriptor)

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
    plan_identity = (plan_dir.stat().st_dev, plan_dir.stat().st_ino)

    real_fsync_directory = plans_module._fsync_directory

    def fail_once(descriptor: int) -> None:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) == plan_identity
            and (plan_dir / "RUNNING").exists()
        ):
            raise OSError("synthetic fsync failure")
        real_fsync_directory(descriptor)

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
    monkeypatch.setattr(
        service,
        "_require_control_identity_attached",
        lambda: None,
    )
    task = asyncio.create_task(
        service._execute_staging_plan_under_lease(
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
    monkeypatch.setattr(
        service,
        "_require_control_identity_attached",
        lambda: None,
    )
    monkeypatch.setattr(store, "finish", fail_quarantine)
    task = asyncio.create_task(
        service._execute_staging_plan_under_lease(
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
    monkeypatch.setattr(
        service,
        "_require_control_identity_attached",
        lambda: None,
    )
    monkeypatch.setattr(store, "finish", fail_quarantine)

    with pytest.raises(BackupServiceError) as error:
        await service._execute_staging_plan_under_lease(
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


def test_orphan_reconciliation_removes_incomplete_terminal_temporary(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    first_store = StagingPlanStore(control, staging)
    plan = first_store.create(
        backup_id="backup_1",
        manifest_sha256="4" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = first_store.begin_execution(
        plan.plan_id,
        expected_plan_hash=plan.plan_hash,
    )
    attempt_id = running.status_data["attemptId"]
    plan_dir = control / "plans" / plan.plan_id
    temporary = plan_dir / ".terminal-incomplete.tmp"
    temporary.write_bytes(b'{"status":')
    os.chmod(temporary, 0o600)

    first_store._release_execution_lease(plan.plan_id, attempt_id)
    recovered = StagingPlanStore(control, staging).inspect(plan.plan_id)

    assert recovered.status == "quarantined"
    assert not temporary.exists()
    assert not tuple(plan_dir.glob(".terminal-*.tmp"))


def test_orphan_reconciliation_makes_bounded_progress_with_many_temporaries(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    first_store = StagingPlanStore(control, staging)
    plan = first_store.create(
        backup_id="backup_1",
        manifest_sha256="6" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = first_store.begin_execution(
        plan.plan_id,
        expected_plan_hash=plan.plan_hash,
    )
    attempt_id = running.status_data["attemptId"]
    plan_dir = control / "plans" / plan.plan_id
    temporary_count = plans_module._MAX_TERMINAL_TEMP_FILES * 2 + 5
    for index in range(temporary_count):
        temporary = plan_dir / f".terminal-{index:016x}.tmp"
        temporary.write_bytes(b"partial")
        os.chmod(temporary, 0o600)

    first_store._release_execution_lease(plan.plan_id, attempt_id)
    recovered = StagingPlanStore(control, staging).inspect(plan.plan_id)
    remaining_after_first = tuple(plan_dir.glob(".terminal-*.tmp"))
    inspected_again = StagingPlanStore(control, staging).inspect(plan.plan_id)

    assert recovered.status == "quarantined"
    assert inspected_again.status == "quarantined"
    assert len(remaining_after_first) < temporary_count
    assert not tuple(plan_dir.glob(".terminal-*.tmp"))


def test_orphan_terminal_is_not_visible_until_reconciliation_payload_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    first_store = StagingPlanStore(control, staging)
    plan = first_store.create(
        backup_id="backup_1",
        manifest_sha256="5" * 64,
        destination_fingerprint_sha256=_DESTINATION_FINGERPRINT,
        jwt_secret_mode="clone",
        actor_id=None,
    )
    running = first_store.begin_execution(
        plan.plan_id,
        expected_plan_hash=plan.plan_hash,
    )
    attempt_id = running.status_data["attemptId"]
    first_store._release_execution_lease(plan.plan_id, attempt_id)
    plan_dir = control / "plans" / plan.plan_id
    write_started = threading.Event()
    release_write = threading.Event()
    real_write_all = plans_module._write_all
    outcome: list[object] = []

    def block_reconciliation_write(descriptor: int, payload: bytes) -> None:
        write_started.set()
        assert release_write.wait(timeout=5)
        real_write_all(descriptor, payload)

    def inspect_orphan() -> None:
        try:
            outcome.append(StagingPlanStore(control, staging).inspect(plan.plan_id))
        except BaseException as exc:  # pragma: no cover - thread diagnostics
            outcome.append(exc)

    monkeypatch.setattr(plans_module, "_write_all", block_reconciliation_write)
    worker = threading.Thread(target=inspect_orphan)
    worker.start()
    try:
        assert write_started.wait(timeout=5)
        assert not (plan_dir / "terminal.json").exists()
    finally:
        release_write.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(outcome) == 1
    assert not isinstance(outcome[0], BaseException)
    assert outcome[0].status == "quarantined"


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
