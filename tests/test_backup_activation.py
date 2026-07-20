from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ppbase.backup.activation import (
    ActivationError,
    BackupActivationStore,
    activation_restart_spec,
    apply_activation_runtime_overlay,
    replace_serve_command_targets,
    settle_failed_activation_restart,
    verify_activation_previous,
    verify_activation_target,
    verify_runtime_database_identity,
)
from ppbase.backup.control import ControlPlaneRoot
from ppbase.config import Settings
from ppbase.services import process_control


def _store(tmp_path: Path) -> tuple[ControlPlaneRoot, BackupActivationStore]:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    root = ControlPlaneRoot.open(control, create_missing=False)
    return root, BackupActivationStore(root)


def test_activation_prepare_publishes_target_overlay_and_resume_token(
    tmp_path: Path,
) -> None:
    root, store = _store(tmp_path)
    try:
        prepared = store.prepare(
            activation_id="a" * 32,
            plan_id="b" * 32,
            backup_id="backup-1",
            plan_hash="c" * 64,
            manifest_sha256="d" * 64,
            signer_fingerprint_sha256="e" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://old@db/old",
            previous_data_dir="/srv/old-data",
            previous_restart_command=["python", "-m", "ppbase", "serve", "--db", "old"],
            target_database_url="postgresql+asyncpg://runtime@db/staged",
            target_data_dir="/srv/staged-data",
            target_restart_command=["python", "-m", "ppbase", "serve", "--db", "target"],
            expected_jwt_sha256=hashlib.sha256(b"clone-secret\n").hexdigest(),
            actor_id="admin-1",
        )

        assert prepared["status"] == "restart_scheduled"
        assert prepared["resumeToken"]
        state = store.inspect("a" * 32)
        assert store.authenticate("a" * 32, prepared["resumeToken"]) is True
        assert state["selectedTarget"] == "target"
        assert "previousDatabaseUrl" not in prepared
        assert "targetDatabaseUrl" not in prepared
    finally:
        store.close()
        root.close()


def test_activation_prepare_uses_caller_supplied_resume_token(
    tmp_path: Path,
) -> None:
    root, store = _store(tmp_path)
    resume_token = "dashboard-generated-resume-token-" + ("x" * 48)
    try:
        prepared = store.prepare(
            activation_id="0" * 32,
            plan_id="1" * 32,
            backup_id="backup-client-token",
            plan_hash="2" * 64,
            manifest_sha256="3" * 64,
            signer_fingerprint_sha256="4" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://old@db/old",
            previous_data_dir="/srv/old-data",
            previous_restart_command=["serve", "--dir", "/srv/old-data"],
            target_database_url="postgresql+asyncpg://runtime@db/staged",
            target_data_dir="/srv/staged-data",
            target_restart_command=["serve", "--dir", "/srv/staged-data"],
            expected_jwt_sha256="5" * 64,
            actor_id=None,
            resume_token=resume_token,
        )

        assert prepared["resumeToken"] == resume_token
        assert store.authenticate("0" * 32, resume_token) is True
        assert store.inspect("0" * 32)["resumeTokenSha256"] == hashlib.sha256(
            resume_token.encode("utf-8")
        ).hexdigest()

        with pytest.raises(ActivationError, match="between 32 and 512"):
            store.prepare(
                activation_id="6" * 32,
                plan_id="7" * 32,
                backup_id="backup-short-token",
                plan_hash="8" * 64,
                manifest_sha256="9" * 64,
                signer_fingerprint_sha256="a" * 64,
                jwt_secret_mode="clone",
                previous_database_url="postgresql+asyncpg://old@db/old",
                previous_data_dir="/srv/old-data",
                previous_restart_command=["serve"],
                target_database_url="postgresql+asyncpg://runtime@db/staged-2",
                target_data_dir="/srv/staged-data-2",
                target_restart_command=["serve"],
                expected_jwt_sha256="b" * 64,
                actor_id=None,
                resume_token="too-short",
            )
    finally:
        store.close()
        root.close()


def test_activation_statuses_for_plan_are_descriptor_enumerated(
    tmp_path: Path,
) -> None:
    root, store = _store(tmp_path)
    plan_id = "1" * 32
    try:
        store.prepare(
            activation_id="2" * 32,
            plan_id=plan_id,
            backup_id="backup-plan-reference",
            plan_hash="3" * 64,
            manifest_sha256="4" * 64,
            signer_fingerprint_sha256="5" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://old@db/old",
            previous_data_dir="/old-data",
            previous_restart_command=["serve", "--dir", "/old-data"],
            target_database_url="postgresql+asyncpg://new@db/new",
            target_data_dir="/new-data",
            target_restart_command=["serve", "--dir", "/new-data"],
            expected_jwt_sha256="6" * 64,
            actor_id=None,
        )
        store.mark_starting("2" * 32)
        store.mark_healthy("2" * 32)

        assert store.statuses_for_plan(
            plan_id,
            backup_id="backup-plan-reference",
        ) == ("healthy",)
        assert store.statuses_for_plan(
            "7" * 32,
            backup_id="backup-plan-reference",
        ) == ()
        with pytest.raises(ActivationError, match="another backup"):
            store.statuses_for_plan(plan_id, backup_id="different-backup")
    finally:
        store.close()
        root.close()


def test_activation_overlay_survives_restart_and_rollback(tmp_path: Path) -> None:
    root, store = _store(tmp_path)
    settings = SimpleNamespace(
        backup_control_dir=str(root.path),
        database_url="postgresql+asyncpg://old@db/old",
        data_dir="/srv/old-data",
    )
    try:
        prepared = store.prepare(
            activation_id="1" * 32,
            plan_id="2" * 32,
            backup_id="backup-2",
            plan_hash="3" * 64,
            manifest_sha256="4" * 64,
            signer_fingerprint_sha256="5" * 64,
            jwt_secret_mode="disaster_recovery",
            previous_database_url=settings.database_url,
            previous_data_dir=settings.data_dir,
            previous_restart_command=["python", "-m", "ppbase", "serve"],
            target_database_url="postgresql+asyncpg://runtime@db/target",
            target_data_dir="/srv/target-data",
            target_restart_command=["python", "-m", "ppbase", "serve", "--db", "target"],
            expected_jwt_sha256="6" * 64,
            actor_id=None,
        )

        applied = apply_activation_runtime_overlay(settings)
        assert applied is not None
        assert settings.database_url.endswith("/target")
        assert settings.data_dir == "/srv/target-data"

        store.mark_starting("1" * 32)
        store.mark_rollback_pending("1" * 32, error_code="health_failed")
        rolled_back = apply_activation_runtime_overlay(settings)
        assert rolled_back is not None
        assert settings.database_url.endswith("/old")
        assert settings.data_dir == "/srv/old-data"
        store.mark_rolled_back("1" * 32)
        payload = store.public_payload(store.inspect("1" * 32))
        assert payload["status"] == "rolled_back"
        assert "resumeToken" not in payload
        assert store.authenticate("1" * 32, prepared["resumeToken"]) is True
    finally:
        store.close()
        root.close()


def test_target_overlay_uses_staged_jwt_file_over_deployment_secret(
    tmp_path: Path,
) -> None:
    target_data = tmp_path / "target-data"
    target_data.mkdir(mode=0o700)
    (target_data / "storage").mkdir(mode=0o700)
    target_secret = "clone-target-secret"
    target_secret_file = target_data / ".jwt_secret"
    target_secret_file.write_text(target_secret + "\n", encoding="utf-8")
    target_secret_file.chmod(0o600)

    root, store = _store(tmp_path)
    settings = Settings(
        backup_control_dir=str(root.path),
        database_url="postgresql+asyncpg://runtime@db/old",
        data_dir=str(tmp_path / "old-data"),
        jwt_secret="deployment-secret-that-must-not-override-clone",
    )
    try:
        store.prepare(
            activation_id="f" * 32,
            plan_id="e" * 32,
            backup_id="backup-explicit-jwt-clone",
            plan_hash="d" * 64,
            manifest_sha256="c" * 64,
            signer_fingerprint_sha256="b" * 64,
            jwt_secret_mode="clone",
            previous_database_url=settings.database_url,
            previous_data_dir=settings.data_dir,
            previous_restart_command=["python", "-m", "ppbase", "serve"],
            target_database_url="postgresql+asyncpg://runtime@db/target",
            target_data_dir=str(target_data),
            target_restart_command=["python", "-m", "ppbase", "serve"],
            expected_jwt_sha256=hashlib.sha256(
                target_secret.encode("utf-8")
            ).hexdigest(),
            actor_id=None,
        )

        state = apply_activation_runtime_overlay(settings)

        assert state is not None
        assert settings.jwt_secret == ""
        assert settings.get_jwt_secret() == target_secret
        assert verify_activation_target(settings, state) == {
            "dataDir": "ok",
            "storage": "ok",
            "jwtSecret": "ok",
        }
    finally:
        store.close()
        root.close()


def test_first_activation_rollback_preserves_deployment_jwt_secret(
    tmp_path: Path,
) -> None:
    root, store = _store(tmp_path)
    settings = Settings(
        backup_control_dir=str(root.path),
        database_url="postgresql+asyncpg://runtime@db/original",
        data_dir=str(tmp_path / "original-data"),
        jwt_secret="deployment-secret",
    )
    try:
        store.prepare(
            activation_id="6" * 32,
            plan_id="7" * 32,
            backup_id="backup-first-rollback-secret",
            plan_hash="8" * 64,
            manifest_sha256="9" * 64,
            signer_fingerprint_sha256="a" * 64,
            jwt_secret_mode="clone",
            previous_database_url=settings.database_url,
            previous_data_dir=settings.data_dir,
            previous_restart_command=["serve"],
            target_database_url="postgresql+asyncpg://runtime@db/target",
            target_data_dir=str(tmp_path / "target-data"),
            target_restart_command=["serve"],
            expected_jwt_sha256="b" * 64,
            actor_id=None,
        )
        store.mark_starting("6" * 32)
        store.mark_rollback_pending("6" * 32, error_code="target_failed")

        state = apply_activation_runtime_overlay(settings)

        assert state is not None
        assert state["selectedTarget"] == "previous"
        assert settings.jwt_secret == "deployment-secret"
        assert settings.get_jwt_secret() == "deployment-secret"
    finally:
        store.close()
        root.close()


def test_chained_rollback_uses_previous_activation_jwt_file(
    tmp_path: Path,
) -> None:
    first_data = tmp_path / "first-data"
    first_data.mkdir(mode=0o700)
    (first_data / "storage").mkdir(mode=0o700)
    first_secret = "first-activation-secret"
    (first_data / ".jwt_secret").write_text(
        first_secret + "\n",
        encoding="utf-8",
    )
    (first_data / ".jwt_secret").chmod(0o600)
    first_info = first_data.stat()
    first_identity = {
        "role": "runtime",
        "database": "first",
        "serverAddress": "127.0.0.1",
        "serverPort": 5432,
        "postmasterStartedAt": "2026-07-18T00:00:00Z",
        "serverVersionNum": "160000",
        "databaseOid": 16384,
        "databaseMarker": "ppbase-staging:first",
    }
    first_digest = hashlib.sha256(first_secret.encode("utf-8")).hexdigest()
    root, store = _store(tmp_path)
    try:
        store.prepare(
            activation_id="c" * 32,
            plan_id="d" * 32,
            backup_id="backup-first",
            plan_hash="e" * 64,
            manifest_sha256="f" * 64,
            signer_fingerprint_sha256="0" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://runtime@db/original",
            previous_data_dir=str(tmp_path / "original-data"),
            previous_restart_command=["serve"],
            target_database_url="postgresql+asyncpg://runtime@db/first",
            target_data_dir=str(first_data),
            target_restart_command=["serve"],
            expected_jwt_sha256=first_digest,
            actor_id=None,
            target_data_identity=(first_info.st_dev, first_info.st_ino),
            expected_database_identity=first_identity,
        )
        store.mark_starting("c" * 32)
        store.mark_healthy("c" * 32)
        store.prepare(
            activation_id="1" * 32,
            plan_id="2" * 32,
            backup_id="backup-second",
            plan_hash="3" * 64,
            manifest_sha256="4" * 64,
            signer_fingerprint_sha256="5" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://runtime@db/first",
            previous_data_dir=str(first_data),
            previous_restart_command=["serve"],
            target_database_url="postgresql+asyncpg://runtime@db/second",
            target_data_dir=str(tmp_path / "second-data"),
            target_restart_command=["serve"],
            expected_jwt_sha256="6" * 64,
            actor_id=None,
            previous_data_identity=(first_info.st_dev, first_info.st_ino),
            expected_previous_jwt_sha256=first_digest,
            expected_previous_database_identity=first_identity,
        )
        store.mark_starting("1" * 32)
        store.mark_rollback_pending("1" * 32, error_code="second_failed")
        settings = Settings(
            backup_control_dir=str(root.path),
            database_url="postgresql+asyncpg://runtime@db/original",
            data_dir=str(tmp_path / "original-data"),
            jwt_secret="deployment-secret-must-not-win",
        )

        state = apply_activation_runtime_overlay(settings)

        assert state is not None
        assert state["previousActivationId"] == "c" * 32
        assert settings.database_url.endswith("/first")
        assert settings.data_dir == str(first_data)
        assert settings.jwt_secret == ""
        assert settings.get_jwt_secret() == first_secret
        assert verify_activation_previous(settings, state) == {
            "dataDir": "ok",
            "storage": "ok",
            "jwtSecret": "ok",
        }
    finally:
        store.close()
        root.close()


def test_activation_state_transitions_fail_closed(tmp_path: Path) -> None:
    root, store = _store(tmp_path)
    try:
        store.prepare(
            activation_id="7" * 32,
            plan_id="8" * 32,
            backup_id="backup-3",
            plan_hash="9" * 64,
            manifest_sha256="a" * 64,
            signer_fingerprint_sha256="b" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://old@db/old",
            previous_data_dir="/old",
            previous_restart_command=["old"],
            target_database_url="postgresql+asyncpg://new@db/new",
            target_data_dir="/new",
            target_restart_command=["new"],
            expected_jwt_sha256="c" * 64,
            actor_id=None,
        )
        store.mark_starting("7" * 32)
        store.mark_healthy("7" * 32)
        with pytest.raises(ActivationError):
            store.mark_rollback_pending("7" * 32, error_code="too_late")
    finally:
        store.close()
        root.close()


def test_replace_serve_command_targets_is_deterministic() -> None:
    command = [
        "python",
        "-m",
        "ppbase",
        "serve",
        "--host",
        "127.0.0.1",
        "--db=old",
        "--dir",
        "/old",
    ]

    replaced = replace_serve_command_targets(
        command,
        database_url="postgresql+asyncpg://runtime@db/new",
        data_dir="/new",
    )

    assert "--db" not in replaced
    assert replaced.count("--dir") == 1
    assert "--db=old" not in replaced
    assert "postgresql+asyncpg://runtime@db/new" not in replaced
    assert replaced[-2:] == ["--dir", "/new"]


def test_interrupted_target_startup_selects_previous_target(tmp_path: Path) -> None:
    root, store = _store(tmp_path)
    settings = SimpleNamespace(
        backup_control_dir=str(root.path),
        database_url="postgresql+asyncpg://old@db/old",
        data_dir="/srv/old-data",
    )
    try:
        store.prepare(
            activation_id="d" * 32,
            plan_id="e" * 32,
            backup_id="backup-crash",
            plan_hash="1" * 64,
            manifest_sha256="2" * 64,
            signer_fingerprint_sha256="3" * 64,
            jwt_secret_mode="clone",
            previous_database_url=settings.database_url,
            previous_data_dir=settings.data_dir,
            previous_restart_command=["old"],
            target_database_url="postgresql+asyncpg://runtime@db/target",
            target_data_dir="/srv/target-data",
            target_restart_command=["target"],
            expected_jwt_sha256="4" * 64,
            actor_id=None,
        )
        store.mark_starting("d" * 32)

        applied = apply_activation_runtime_overlay(settings)

        assert applied is not None
        assert applied["status"] == "rollback_pending"
        assert settings.database_url.endswith("/old")
        assert settings.data_dir == "/srv/old-data"
    finally:
        store.close()
        root.close()


def test_activation_health_verifies_anchored_storage_and_effective_secret(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "staged-data"
    data_dir.mkdir(mode=0o700)
    (data_dir / "storage").mkdir(mode=0o700)
    secret = "s" * 64
    secret_file = data_dir / ".jwt_secret"
    secret_file.write_text(secret + "\n", encoding="utf-8")
    secret_file.chmod(0o600)
    settings = SimpleNamespace(
        data_dir=str(data_dir),
        get_jwt_secret=lambda: secret,
    )
    state = {
        "selectedTarget": "target",
        "targetDataDir": str(data_dir),
        "expectedJwtSha256": hashlib.sha256(secret.encode()).hexdigest(),
    }

    assert verify_activation_target(settings, state) == {
        "dataDir": "ok",
        "storage": "ok",
        "jwtSecret": "ok",
    }

    secret_file.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ActivationError):
        verify_activation_target(settings, state)


def test_runtime_database_identity_pins_optional_oid_and_marker() -> None:
    base_identity = {
        "role": "runtime",
        "database": "staged",
        "serverAddress": "127.0.0.1",
        "serverPort": 5432,
        "postmasterStartedAt": "2026-07-18T00:00:00Z",
        "serverVersionNum": "160000",
    }
    runtime_identity = {
        **base_identity,
        "databaseOid": 16384,
        "databaseMarker": "ppbase-staging:plan-1",
    }

    assert verify_runtime_database_identity(
        runtime_identity,
        base_identity,
        label="activated target",
    )["databaseOid"] == 16384
    assert verify_runtime_database_identity(
        runtime_identity,
        runtime_identity,
        label="activated target",
    )["databaseMarker"] == "ppbase-staging:plan-1"
    no_comment_identity = {
        **runtime_identity,
        "databaseMarker": "",
    }
    assert verify_runtime_database_identity(
        no_comment_identity,
        no_comment_identity,
        label="rollback target",
    )["databaseMarker"] == ""

    with pytest.raises(ActivationError, match="identity changed"):
        verify_runtime_database_identity(
            {**runtime_identity, "databaseOid": 16385},
            runtime_identity,
            label="activated target",
        )
    with pytest.raises(ActivationError, match="identity changed"):
        verify_runtime_database_identity(
            {**runtime_identity, "databaseMarker": "foreign"},
            runtime_identity,
            label="activated target",
        )


def test_public_activation_payload_never_contains_runtime_credentials(
    tmp_path: Path,
) -> None:
    root, store = _store(tmp_path)
    try:
        prepared = store.prepare(
            activation_id="5" * 32,
            plan_id="6" * 32,
            backup_id="backup-secret-redaction",
            plan_hash="7" * 64,
            manifest_sha256="8" * 64,
            signer_fingerprint_sha256="9" * 64,
            jwt_secret_mode="disaster_recovery",
            previous_database_url="postgresql+asyncpg://old:OLD_SECRET@db/old",
            previous_data_dir="/old",
            previous_restart_command=["serve", "--db", "OLD_SECRET"],
            target_database_url="postgresql+asyncpg://new:NEW_SECRET@db/new",
            target_data_dir="/new",
            target_restart_command=["serve", "--db", "NEW_SECRET"],
            expected_jwt_sha256="a" * 64,
            actor_id=None,
        )

        serialized = json.dumps(prepared)
        assert "OLD_SECRET" not in serialized
        assert "NEW_SECRET" not in serialized
        assert "DatabaseUrl" not in serialized
        assert "RestartCommand" not in serialized
        state = store.inspect("5" * 32)
        command, environment = activation_restart_spec(state)
        assert "OLD_SECRET" not in " ".join(command)
        assert "NEW_SECRET" not in " ".join(command)
        assert environment["PPBASE_DATABASE_URL"].endswith("@db/new")
    finally:
        store.close()
        root.close()


def test_runtime_overlay_promotes_legacy_staging_target_and_retargets_dump_dsn(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    targets = tmp_path / "targets"
    plan_id = "a" * 32
    legacy_plan_dir = staging / plan_id
    legacy_data_dir = legacy_plan_dir / "data"
    legacy_data_dir.mkdir(parents=True, mode=0o700)
    legacy_plan_dir.chmod(0o700)
    (legacy_data_dir / "storage").mkdir(mode=0o700)
    target_info = legacy_data_dir.stat()

    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    activation_id = "b" * 32
    resume_token = "legacy_resume_token_abcdefghijklmnopqrstuvwxyz0123456789"
    try:
        store.prepare(
            activation_id=activation_id,
            plan_id=plan_id,
            backup_id="legacy-active-backup",
            plan_hash="c" * 64,
            manifest_sha256="d" * 64,
            signer_fingerprint_sha256="e" * 64,
            jwt_secret_mode="disaster_recovery",
            previous_database_url="postgresql+asyncpg://runtime@db/source",
            previous_data_dir=str(tmp_path / "previous"),
            previous_restart_command=["serve", "--dir", str(tmp_path / "previous")],
            target_database_url="postgresql+asyncpg://runtime@db/restored",
            target_data_dir=str(legacy_data_dir),
            target_restart_command=["serve", "--dir", str(legacy_data_dir)],
            expected_jwt_sha256="f" * 64,
            actor_id=None,
            target_data_identity=(target_info.st_dev, target_info.st_ino),
            resume_token=resume_token,
            previous_dump_database_url="postgresql+asyncpg://dump@db/source",
            target_dump_database_url="postgresql+asyncpg://dump@db/restored",
        )
    finally:
        store.close()
        root.close()

    settings = SimpleNamespace(
        backup_control_dir=str(control),
        backup_staging_root=str(staging),
        backup_target_root=str(targets),
        database_url="postgresql+asyncpg://runtime@db/source",
        backup_dump_database_url="postgresql+asyncpg://dump@db/source",
        data_dir=str(tmp_path / "previous"),
    )

    first = apply_activation_runtime_overlay(settings)
    promoted = targets / plan_id / "data"
    assert first is not None
    assert settings.data_dir == str(promoted)
    assert settings.backup_dump_database_url.endswith("@db/restored")
    assert promoted.stat().st_ino == target_info.st_ino
    assert not legacy_plan_dir.exists()

    second = apply_activation_runtime_overlay(settings)
    assert second is not None
    assert settings.data_dir == str(promoted)
    assert promoted.stat().st_ino == target_info.st_ino

    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    try:
        persisted = store.inspect(activation_id)
        assert persisted["targetDataDir"] == str(promoted)
        assert persisted["targetRestartEnvironment"] == {
            "PPBASE_DATABASE_URL": "postgresql+asyncpg://runtime@db/restored",
            "PPBASE_BACKUP_DUMP_DATABASE_URL": "postgresql+asyncpg://dump@db/restored",
        }
        assert store.authenticate(activation_id, resume_token)
    finally:
        store.close()
        root.close()


def test_rollback_rejects_replaced_previous_data_dir(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir(mode=0o700)
    (previous / "storage").mkdir(mode=0o700)
    secret_value = "p" * 64
    (previous / ".jwt_secret").write_text(secret_value + "\n", encoding="utf-8")
    (previous / ".jwt_secret").chmod(0o600)
    previous_info = previous.stat()
    root, store = _store(tmp_path)
    try:
        store.prepare(
            activation_id="0" * 32,
            plan_id="1" * 32,
            backup_id="backup-rollback-identity",
            plan_hash="2" * 64,
            manifest_sha256="3" * 64,
            signer_fingerprint_sha256="4" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://runtime@db/old",
            previous_data_dir=str(previous),
            previous_restart_command=["serve", "--dir", str(previous)],
            target_database_url="postgresql+asyncpg://runtime@db/new",
            target_data_dir=str(tmp_path / "target"),
            target_restart_command=["serve", "--dir", str(tmp_path / "target")],
            expected_jwt_sha256="5" * 64,
            actor_id=None,
            previous_data_identity=(previous_info.st_dev, previous_info.st_ino),
            expected_previous_jwt_sha256=hashlib.sha256(
                secret_value.encode()
            ).hexdigest(),
        )
        store.mark_starting("0" * 32)
        state = store.mark_rollback_pending("0" * 32, error_code="target_failed")
        detached = tmp_path / "previous-detached"
        previous.rename(detached)
        previous.mkdir(mode=0o700)
        (previous / "storage").mkdir(mode=0o700)
        (previous / ".jwt_secret").write_text(secret_value + "\n", encoding="utf-8")
        (previous / ".jwt_secret").chmod(0o600)
        settings = SimpleNamespace(
            data_dir=str(previous),
            get_jwt_secret=lambda: secret_value,
        )

        with pytest.raises(ActivationError, match="identity changed"):
            verify_activation_previous(settings, state)
    finally:
        store.close()
        root.close()


def test_activation_health_timeout_durably_selects_rollback_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store = _store(tmp_path)
    restarted = threading.Event()
    captured: list[tuple[list[str], dict[str, str]]] = []
    try:
        store.prepare(
            activation_id="a" * 32,
            plan_id="b" * 32,
            backup_id="backup-health-timeout",
            plan_hash="c" * 64,
            manifest_sha256="d" * 64,
            signer_fingerprint_sha256="e" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://old:OLD@db/old",
            previous_data_dir="/old-data",
            previous_restart_command=[
                "python",
                "-m",
                "ppbase",
                "serve",
                "--db",
                "postgresql+asyncpg://old:OLD@db/old",
            ],
            target_database_url="postgresql+asyncpg://new:NEW@db/new",
            target_data_dir="/new-data",
            target_restart_command=["python", "-m", "ppbase", "serve"],
            expected_jwt_sha256="f" * 64,
            actor_id=None,
        )
        store.mark_starting("a" * 32)

        def capture_restart(
            _reason: str,
            *,
            command: list[str],
            env_overrides: dict[str, str],
        ) -> None:
            captured.append((list(command), dict(env_overrides)))
            restarted.set()

        monkeypatch.setattr(
            process_control,
            "restart_process_now",
            capture_restart,
        )
        thread = process_control.schedule_backup_activation_watchdog(
            control_dir=str(root.path),
            activation_id="a" * 32,
            timeout_seconds=0.01,
        )

        assert thread is not None
        assert restarted.wait(timeout=2)
        thread.join(timeout=2)
        state = store.inspect("a" * 32)
        assert state["status"] == "rollback_pending"
        assert state["selectedTarget"] == "previous"
        assert state["errorCode"] == "activation_health_timeout"
        assert captured == [
            (
                [
                    "python",
                    "-m",
                    "ppbase",
                    "serve",
                    "--dir",
                    "/old-data",
                ],
                {
                    "PPBASE_DATABASE_URL": (
                        "postgresql+asyncpg://old:OLD@db/old"
                    )
                },
            )
        ]
        assert "OLD" not in " ".join(captured[0][0])
        assert "NEW" not in " ".join(captured[0][0])
    finally:
        store.close()
        root.close()


def test_activation_watchdog_single_flight_is_scoped_to_control_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activation_id = "f" * 32
    opened: list[tuple[ControlPlaneRoot, BackupActivationStore]] = []
    restarted = threading.Event()
    captured: list[str] = []

    def prepare(base: Path, suffix: str) -> tuple[ControlPlaneRoot, BackupActivationStore]:
        base.mkdir(mode=0o700)
        root, store = _store(base)
        store.prepare(
            activation_id=activation_id,
            plan_id=suffix * 32,
            backup_id=f"backup-watchdog-{suffix}",
            plan_hash=suffix * 64,
            manifest_sha256=("a" if suffix != "a" else "b") * 64,
            signer_fingerprint_sha256=("c" if suffix != "c" else "d") * 64,
            jwt_secret_mode="clone",
            previous_database_url=f"postgresql+asyncpg://old@db/old_{suffix}",
            previous_data_dir=f"/old-{suffix}",
            previous_restart_command=["serve", "--dir", f"/old-{suffix}"],
            target_database_url=f"postgresql+asyncpg://new@db/new_{suffix}",
            target_data_dir=f"/new-{suffix}",
            target_restart_command=["serve", "--dir", f"/new-{suffix}"],
            expected_jwt_sha256="e" * 64,
            actor_id=None,
        )
        store.mark_starting(activation_id)
        opened.append((root, store))
        return root, store

    first_root, _first_store = prepare(tmp_path / "first", "1")
    second_root, _second_store = prepare(tmp_path / "second", "2")

    def capture_restart(
        _reason: str,
        *,
        command: list[str],
        env_overrides: dict[str, str],
    ) -> None:
        del command
        captured.append(env_overrides["PPBASE_DATABASE_URL"])
        if len(captured) == 2:
            restarted.set()

    monkeypatch.setattr(process_control, "restart_process_now", capture_restart)
    try:
        first_thread = process_control.schedule_backup_activation_watchdog(
            control_dir=str(first_root.path),
            activation_id=activation_id,
            timeout_seconds=0.01,
        )
        second_thread = process_control.schedule_backup_activation_watchdog(
            control_dir=str(second_root.path),
            activation_id=activation_id,
            timeout_seconds=0.01,
        )

        assert first_thread is not None
        assert second_thread is not None
        assert restarted.wait(timeout=2)
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)
        assert sorted(captured) == [
            "postgresql+asyncpg://old@db/old_1",
            "postgresql+asyncpg://old@db/old_2",
        ]
    finally:
        for root, store in reversed(opened):
            store.close()
            root.close()


def test_failed_scheduled_exec_durably_keeps_previous_target_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store = _store(tmp_path)
    settled = threading.Event()
    try:
        store.prepare(
            activation_id="8" * 32,
            plan_id="9" * 32,
            backup_id="backup-exec-failure",
            plan_hash="a" * 64,
            manifest_sha256="b" * 64,
            signer_fingerprint_sha256="c" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://old@db/old",
            previous_data_dir="/old-data",
            previous_restart_command=["serve", "--dir", "/old-data"],
            target_database_url="postgresql+asyncpg://new@db/new",
            target_data_dir="/new-data",
            target_restart_command=["serve", "--dir", "/new-data"],
            expected_jwt_sha256="d" * 64,
            actor_id=None,
        )

        def fail_exec(*_args: object, **_kwargs: object) -> None:
            raise OSError("synthetic exec failure")

        def persist_failure(_exc: BaseException) -> None:
            settle_failed_activation_restart(
                root.path,
                "8" * 32,
                error_code="restart_exec_failed",
            )
            settled.set()

        monkeypatch.setattr(process_control.os, "execvpe", fail_exec)
        monkeypatch.setattr(process_control.time, "sleep", lambda _seconds: None)

        assert process_control.schedule_process_restart(
            "test failed activation exec",
            command=["serve", "--dir", "/new-data"],
            env_overrides={
                "PPBASE_DATABASE_URL": "postgresql+asyncpg://new@db/new"
            },
            on_failure=persist_failure,
        ) is True
        assert settled.wait(timeout=2)

        state = store.inspect("8" * 32)
        assert state["status"] == "rolled_back"
        assert state["selectedTarget"] == "previous"
        assert state["errorCode"] == "restart_exec_failed"
        assert store.active() is None
        assert process_control.is_restart_scheduled() is False
    finally:
        store.close()
        root.close()


def test_restart_worker_start_failure_is_rejected_without_sticky_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def start(self) -> None:
            raise RuntimeError("synthetic thread start failure")

    monkeypatch.setattr(process_control.threading, "Thread", FailingThread)
    with process_control._restart_lock:
        process_control._restart_scheduled = False
        process_control._restart_reservation = None

    assert process_control.schedule_process_restart(
        "test rejected restart worker",
        command=["python", "-m", "ppbase"],
    ) is False
    assert process_control.is_restart_scheduled() is False


def test_restart_reservation_excludes_competing_restart_until_release() -> None:
    with process_control._restart_lock:
        process_control._restart_scheduled = False
        process_control._restart_reservation = None

    reservation = process_control.reserve_process_restart()
    assert reservation is not None
    assert process_control.is_restart_scheduled() is True
    assert process_control.schedule_process_restart(
        "competing restart",
        command=["python", "-m", "ppbase"],
    ) is False

    reservation.release()
    assert process_control.is_restart_scheduled() is False


def test_reserved_restart_start_failure_keeps_slot_until_cutover_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def start(self) -> None:
            raise RuntimeError("synthetic thread start failure")

    monkeypatch.setattr(process_control.threading, "Thread", FailingThread)
    with process_control._restart_lock:
        process_control._restart_scheduled = False
        process_control._restart_reservation = None

    reservation = process_control.reserve_process_restart()
    assert reservation is not None
    assert process_control.schedule_process_restart(
        "reserved activation restart",
        command=["python", "-m", "ppbase"],
        reservation=reservation,
    ) is False
    # The activation journal has not been rolled back by the caller yet.
    assert process_control.is_restart_scheduled() is True

    reservation.release()
    assert process_control.is_restart_scheduled() is False


@pytest.mark.asyncio
async def test_backup_api_wires_exec_failure_to_the_activation_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.api import backups as backups_api
    from ppbase.backup.service import PreparedBackupActivation

    root, store = _store(tmp_path)
    captured_failure: list[object] = []
    try:
        store.prepare(
            activation_id="e" * 32,
            plan_id="f" * 32,
            backup_id="backup-api-exec-failure",
            plan_hash="1" * 64,
            manifest_sha256="2" * 64,
            signer_fingerprint_sha256="3" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://old@db/old",
            previous_data_dir="/old-data",
            previous_restart_command=["serve", "--dir", "/old-data"],
            target_database_url="postgresql+asyncpg://new@db/new",
            target_data_dir="/new-data",
            target_restart_command=["serve", "--dir", "/new-data"],
            expected_jwt_sha256="4" * 64,
            actor_id=None,
        )

        def capture_schedule(
            _reason: str,
            *,
            delay_seconds: float,
            command: list[str],
            env_overrides: dict[str, str],
            on_failure: object,
            reservation: object,
            before_exec: object,
        ) -> bool:
            assert delay_seconds == 1.0
            assert command == ["serve", "--dir", "/new-data"]
            assert env_overrides["PPBASE_DATABASE_URL"].endswith("/new")
            assert reservation is guard.restart_reservation
            assert before_exec == guard.verify_from_restart_thread
            captured_failure.append(on_failure)
            return True

        class CutoverGuard:
            def __init__(self) -> None:
                self.released = False
                self.restart_reservation = object()

            def retain_operation_context(self, _context: object) -> None:
                return None

            async def close(self) -> None:
                self.released = True

            def close_from_restart_thread(self) -> None:
                # Durable rollback must be the commit point before mutations
                # are allowed to resume.
                assert store.inspect("e" * 32)["status"] == "rolled_back"
                self.released = True

            def verify_from_restart_thread(self) -> None:
                return None

        guard = CutoverGuard()
        service = SimpleNamespace(
            control_root=root,
            get_activation_restart_spec=lambda _activation_id: (
                ["serve", "--dir", "/new-data"],
                {"PPBASE_DATABASE_URL": "postgresql+asyncpg://new@db/new"},
            ),
        )
        monkeypatch.setattr(
            backups_api,
            "schedule_process_restart",
            capture_schedule,
        )

        await backups_api._schedule_backup_activation(
            service,
            PreparedBackupActivation(
                {"activationId": "e" * 32},
                cutover_guard=guard,  # type: ignore[arg-type]
            ),
        )

        assert len(captured_failure) == 1
        callback = captured_failure[0]
        assert callable(callback)
        callback(OSError("synthetic exec failure"))
        state = store.inspect("e" * 32)
        assert state["status"] == "rolled_back"
        assert state["errorCode"] == "restart_exec_failed"
        assert store.active() is None
        assert guard.released is True
    finally:
        store.close()
        root.close()


def test_chained_activation_rollback_restores_previous_healthy_pointer(
    tmp_path: Path,
) -> None:
    root, store = _store(tmp_path)
    first_id = "1" * 32
    second_id = "2" * 32
    first_database_identity = {
        "role": "runtime",
        "database": "first",
        "serverAddress": "127.0.0.1",
        "serverPort": 5432,
        "postmasterStartedAt": "2026-07-18T00:00:00Z",
        "serverVersionNum": "160000",
    }
    try:
        store.prepare(
            activation_id=first_id,
            plan_id="3" * 32,
            backup_id="backup-first",
            plan_hash="4" * 64,
            manifest_sha256="5" * 64,
            signer_fingerprint_sha256="6" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://runtime@db/original",
            previous_data_dir="/data/original",
            previous_restart_command=["serve", "--dir", "/data/original"],
            target_database_url="postgresql+asyncpg://runtime@db/first",
            target_data_dir="/data/first",
            target_restart_command=["serve", "--dir", "/data/first"],
            expected_jwt_sha256="7" * 64,
            actor_id=None,
            target_data_identity=(101, 201),
            expected_database_identity=first_database_identity,
        )
        store.mark_starting(first_id)
        store.mark_healthy(first_id)

        store.prepare(
            activation_id=second_id,
            plan_id="8" * 32,
            backup_id="backup-second",
            plan_hash="9" * 64,
            manifest_sha256="a" * 64,
            signer_fingerprint_sha256="b" * 64,
            jwt_secret_mode="clone",
            previous_database_url="postgresql+asyncpg://runtime@db/first",
            previous_data_dir="/data/first",
            previous_restart_command=["serve", "--dir", "/data/first"],
            target_database_url="postgresql+asyncpg://runtime@db/second",
            target_data_dir="/data/second",
            target_restart_command=["serve", "--dir", "/data/second"],
            expected_jwt_sha256="c" * 64,
            actor_id=None,
            previous_data_identity=(101, 201),
            expected_previous_jwt_sha256="7" * 64,
            expected_previous_database_identity=first_database_identity,
            target_data_identity=(102, 202),
            expected_database_identity={
                **first_database_identity,
                "database": "second",
            },
        )
        assert store.inspect(second_id)["previousActivationId"] == first_id
        store.mark_starting(second_id)
        store.mark_rollback_pending(second_id, error_code="second_failed")
        store.mark_rolled_back(second_id)

        active = store.active()
        assert active is not None
        assert active["activationId"] == first_id
        assert active["status"] == "healthy"

        settings = SimpleNamespace(
            backup_control_dir=str(root.path),
            database_url="postgresql+asyncpg://runtime@db/original",
            data_dir="/data/original",
            jwt_secret="deployment-secret",
        )
        applied = apply_activation_runtime_overlay(settings)
        assert applied is not None
        assert applied["activationId"] == first_id
        assert settings.database_url.endswith("/first")
        assert settings.data_dir == "/data/first"
    finally:
        store.close()
        root.close()
