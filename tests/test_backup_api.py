from __future__ import annotations

import asyncio
from contextlib import contextmanager
import gc
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest
from starlette.requests import ClientDisconnect, Request

from ppbase.api import backups as backups_api
from ppbase.backup.service import (
    BackupServiceError,
    NativeBackupService,
    PreparedBackupActivation,
)
from ppbase.backup.activation import BackupActivationStore
from ppbase.backup.control import ControlPlaneRoot
from ppbase.backup.transport import PinnedBackupZip
from ppbase.config import Settings
from ppbase.middleware.request_logger import RequestLoggerMiddleware


class _FakeCutoverGuard:
    def __init__(self) -> None:
        self.contexts: list[object] = []
        self.closed = False
        self.restart_reservation = object()

    def retain_operation_context(self, context: object) -> None:
        self.contexts.append(context)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for context in reversed(self.contexts):
            context.__exit__(None, None, None)  # type: ignore[attr-defined]

    def close_from_restart_thread(self) -> None:
        self.closed = True

    def verify_from_restart_thread(self) -> None:
        return None


def test_backup_readiness_reports_missing_prerequisites_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backups_api, "can_self_restart", lambda: False)
    settings = SimpleNamespace(
        storage_backend="local",
        backup_dump_database_url="",
        backup_creator_database_url="postgresql://creator:CREATOR_SECRET@db/postgres",
        backup_restore_database_url="",
        backup_target_owner="",
    )

    payload = backups_api._backup_readiness(settings)

    assert payload == {
        "create": {
            "configured": False,
            "missing": ["PPBASE_BACKUP_DUMP_DATABASE_URL"],
        },
        "restore": {
            "configured": False,
            "missing": [
                "PPBASE_BACKUP_RESTORE_DATABASE_URL",
                "PPBASE_BACKUP_TARGET_OWNER",
            ],
        },
        "activation": {
            "configured": False,
            "missing": ["PPBASE_RESTART_CMD"],
        },
        "postgresqlTools": {
            "configured": False,
            "missing": ["pg_dump", "pg_restore", "psql"],
        },
        "storage": {"configured": True, "missing": []},
        "controlPlane": {"configured": True, "missing": []},
        "storageBackend": "local",
        "warnings": [],
        "onboarding": {
            "recommended": "production",
            "productionCommand": "ppbase backup provision --plan",
            "localCommand": "ppbase backup provision --plan --local",
            "doctorCommand": "ppbase backup doctor",
        },
    }
    assert "CREATOR_SECRET" not in json.dumps(payload)


def test_backup_readiness_marks_a_configured_local_deployment_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backups_api, "can_self_restart", lambda: True)
    monkeypatch.setattr(backups_api.shutil, "which", lambda value: f"/bin/{value}")
    settings = SimpleNamespace(
        storage_backend="local",
        backup_dump_database_url="postgresql://dump@db/app",
        backup_creator_database_url="postgresql://creator@db/postgres",
        backup_restore_database_url="postgresql://restore@db/postgres",
        backup_target_owner="ppbase_restore_owner",
    )

    payload = backups_api._backup_readiness(settings)

    assert payload["create"] == {"configured": True, "missing": []}
    assert payload["restore"] == {"configured": True, "missing": []}
    assert payload["activation"] == {"configured": True, "missing": []}
    assert payload["postgresqlTools"] == {"configured": True, "missing": []}
    assert payload["storage"] == {"configured": True, "missing": []}
    assert payload["controlPlane"] == {"configured": True, "missing": []}
    assert payload["warnings"] == []


@pytest.mark.asyncio
async def test_backup_readiness_api_reports_runtime_superuser_as_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backups_api, "can_self_restart", lambda: True)
    monkeypatch.setattr(backups_api.shutil, "which", lambda value: f"/bin/{value}")

    class _ReadinessSession:
        def __init__(self) -> None:
            self.rollback_calls = 0

        async def scalar(self, statement):
            sql = str(statement)
            assert "pg_catalog.pg_roles" in sql
            assert "SESSION_USER" in sql
            return True

        async def rollback(self) -> None:
            self.rollback_calls += 1

    session = _ReadinessSession()
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        storage_backend="local",
        backup_dump_database_url="postgresql://dump:DUMP_SECRET@db/app",
        backup_creator_database_url=(
            "postgresql://creator:CREATOR_SECRET@db/postgres"
        ),
        backup_restore_database_url=(
            "postgresql://restore:RESTORE_SECRET@db/postgres"
        ),
        backup_target_owner="ppbase_restore_owner",
    )
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    app.dependency_overrides[backups_api.get_session] = lambda: session
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/backups/readiness")

    assert response.status_code == 200
    assert response.json()["warnings"] == [
        {
            "code": "legacy_runtime_superuser",
            "name": "runtime_role",
            "detail": "PostgreSQL superuser runtime",
        }
    ]
    assert session.rollback_calls == 1
    response_body = response.text
    assert "DUMP_SECRET" not in response_body
    assert "CREATOR_SECRET" not in response_body
    assert "RESTORE_SECRET" not in response_body


@pytest.mark.asyncio
async def test_backup_readiness_checks_auth_before_runtime_role() -> None:
    class _UnreadSession:
        async def scalar(self, _statement):
            raise AssertionError("runtime role must not be queried before auth")

        async def rollback(self) -> None:
            raise AssertionError("endpoint body must not run before auth")

    async def deny_admin():
        raise HTTPException(status_code=401, detail="unauthorized")

    app = FastAPI()
    app.state.settings = SimpleNamespace()
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = deny_admin
    app.dependency_overrides[backups_api.get_session] = lambda: _UnreadSession()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/backups/readiness")

    assert response.status_code == 401


class _FakeBackupService:
    def __init__(self):
        self.close_calls = 0
        self.created_transport_filename = None
        self.mutation_entries = 0
        self.mutation_exits = 0
        self.mutation_active = False
        self.inspect_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.close_calls += 1

    def get_identity(self):
        return {"algorithm": "Ed25519", "fingerprintSha256": "f" * 64}

    async def create_local_backup(self, *, actor_id=None, transport_filename=None):
        self.created_transport_filename = transport_filename
        return {"id": "backup_1", "status": "sealed", "actorId": actor_id}

    async def list_local_backups(self):
        return [{"id": "backup_1", "status": "sealed"}]

    async def inspect_local_backup(
        self,
        backup_id,
        *,
        resource_offset=0,
        resource_limit=100,
    ):
        self.inspect_calls.append((backup_id, resource_offset, resource_limit))
        resource_count = 300
        returned = min(resource_limit, max(0, resource_count - resource_offset))
        return {
            "id": backup_id,
            "resourcesVerified": True,
            "resourceCount": resource_count,
            "resourceOffset": resource_offset,
            "resourceLimit": resource_limit,
            "resourcesReturned": returned,
            "hasMoreResources": resource_offset + returned < resource_count,
            "resources": [
                {"path": f"resources/{index:04d}"}
                for index in range(resource_offset, resource_offset + returned)
            ],
        }

    @contextmanager
    def mutation_operation(self):
        self.mutation_entries += 1
        self.mutation_active = True
        try:
            yield SimpleNamespace(scope="global", mode="exclusive")
        finally:
            self.mutation_active = False
            self.mutation_exits += 1

    async def materialize_local_backup_zip(self, backup_id):
        payload = b"PK\x03\x04synthetic-backup-zip"
        self.last_pinned = PinnedBackupZip(
            filename=f"ppbase_backup_test_{backup_id}.zip",
            size=len(payload),
            _handle=io.BytesIO(payload),
        )
        return self.last_pinned

    async def upload_local_backup(self, source, *, operation_lease=None):
        assert operation_lease is not None
        return {
            "id": "uploaded_backup",
            "status": "sealed",
            "uploadedBytes": len(source.read()),
        }

    async def delete_local_backup(self, backup_id):
        self.deleted_backup_id = backup_id

    def list_trusted_signers(self):
        return [
            {
                "fingerprintSha256": "e" * 64,
                "trustStatus": "trusted_external",
            }
        ]

    async def approve_backup_signer(
        self,
        backup_id,
        *,
        expected_fingerprint_sha256,
        actor_id,
    ):
        return {
            "backupId": backup_id,
            "fingerprintSha256": expected_fingerprint_sha256,
            "actorId": actor_id,
            "trustStatus": "trusted_external",
        }

    def revoke_backup_signer(self, fingerprint_sha256):
        self.revoked_fingerprint = fingerprint_sha256

    async def create_staging_plan(
        self,
        backup_id,
        *,
        jwt_secret_mode,
        actor_id,
    ):
        return {
            "id": "a" * 32,
            "backupId": backup_id,
            "jwtSecretMode": jwt_secret_mode,
            "actorId": actor_id,
            "planHash": "b" * 64,
            "status": "planned",
        }

    def inspect_staging_plan(self, plan_id):
        return {"id": plan_id, "status": "planned"}

    async def execute_staging_plan(self, plan_id, *, expected_plan_hash):
        return {
            "id": plan_id,
            "planHash": expected_plan_hash,
            "status": "validated",
            "activationPerformed": False,
        }

    async def activate_staging_plan(
        self,
        plan_id,
        *,
        expected_plan_hash,
        actor_id,
        activation_id=None,
        resume_token=None,
    ):
        self.activation = PreparedBackupActivation({
            "activationId": activation_id or "c" * 32,
            "planId": plan_id,
            "planHash": expected_plan_hash,
            "actorId": actor_id,
            "status": "restart_scheduled",
            "phase": "restarting",
            "resumeToken": resume_token or "resume-token",
        }, cutover_guard=_FakeCutoverGuard())
        return self.activation

    async def restore_local_backup(
        self,
        backup_id,
        *,
        actor_id,
        operation_lease=None,
    ):
        assert operation_lease is not None
        assert self.mutation_active is True
        self.restored_backup_id = backup_id
        return PreparedBackupActivation({
            "activationId": "d" * 32,
            "backupId": backup_id,
            "actorId": actor_id,
            "status": "restart_scheduled",
            "resumeToken": "sdk-resume-token",
        }, cutover_guard=_FakeCutoverGuard())

    async def resolve_restore_reference(self, backup_id, *, operation_lease):
        assert operation_lease is not None
        assert self.mutation_active is True
        self.resolved_restore_reference = backup_id
        if backup_id == "sdk-visible-name.zip":
            return "backup_canonical_1"
        return backup_id

    def get_activation_restart_spec(self, activation_id):
        return (
            ["python", "-m", "ppbase", "serve", "--activation", activation_id],
            {"PPBASE_DATABASE_URL": "postgresql+asyncpg://runtime@db/target"},
        )

    def abandon_prepared_activation(self, activation_id, *, error_code):
        self.abandoned_activation = (activation_id, error_code)
        return {"activationId": activation_id, "status": "rolled_back"}

    def authenticate_activation(self, activation_id, resume_token):
        return resume_token in {
            "resume-token",
            "sdk-resume-token",
            "dashboard_generated_resume_token_123456789",
        }

    def inspect_activation(self, activation_id):
        return {
            "activationId": activation_id,
            "status": "succeeded",
            "phase": "health_check",
        }


@pytest.mark.asyncio
async def test_backup_api_exposes_local_and_staging_workflow(monkeypatch) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    monkeypatch.setattr(
        backups_api,
        "schedule_process_restart",
        lambda *_args, **_kwargs: True,
    )

    async def unexpected_activation_admin(_request):
        raise AssertionError("scoped activation token must bypass stale JWT auth")

    monkeypatch.setattr(
        backups_api,
        "_require_activation_admin",
        unexpected_activation_admin,
    )

    class FakeControlRoot:
        def close(self):
            return None

    class FakeActivationStore:
        def __init__(self, _root):
            pass

        def authenticate_state(self, activation_id, resume_token):
            if resume_token != "dashboard_generated_resume_token_123456789":
                return None
            return {"activationId": activation_id, "status": "healthy"}

        def public_payload(self, state):
            return {
                "activationId": state["activationId"],
                "status": "succeeded",
                "phase": "health_check",
            }

        def close(self):
            return None

    monkeypatch.setattr(
        backups_api.ControlPlaneRoot,
        "open",
        lambda *_args, **_kwargs: FakeControlRoot(),
    )
    monkeypatch.setattr(backups_api, "BackupActivationStore", FakeActivationStore)

    app = FastAPI()
    app.state.settings = SimpleNamespace(backup_control_dir="/unused-control")
    app.include_router(backups_api.router, prefix="/api")
    app.include_router(backups_api.staging_router, prefix="/api")
    app.include_router(backups_api.activation_router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    app.dependency_overrides[backups_api._authorize_backup_read] = lambda: {
        "mode": "inspect",
        "auth": {"id": "admin_1"},
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        identity = await client.get("/api/backups/identity")
        assert identity.status_code == 200
        assert identity.json()["algorithm"] == "Ed25519"

        created = await client.post("/api/backups")
        assert created.status_code == 201
        assert created.json() == {
            "id": "backup_1",
            "status": "sealed",
            "actorId": "admin_1",
        }
        assert service.created_transport_filename is None

        named = await client.post(
            "/api/backups",
            json={"name": "PPBase sauvegarde été.zip"},
        )
        assert named.status_code == 201
        assert service.created_transport_filename == "PPBase sauvegarde été.zip"

        empty_name = await client.post("/api/backups", json={"name": ""})
        assert empty_name.status_code == 201
        assert service.created_transport_filename is None

        null_body = await client.post(
            "/api/backups",
            content=b"null",
            headers={"content-type": "application/json"},
        )
        assert null_body.status_code == 201
        assert service.created_transport_filename is None

        listed = await client.get("/api/backups")
        assert listed.json() == [{"id": "backup_1", "status": "sealed"}]
        inspected = await client.get("/api/backups/backup_1")
        assert inspected.json()["resourcesVerified"] is True

        signers = await client.get("/api/backups/trusted-signers")
        assert signers.status_code == 200
        assert signers.json()[0]["fingerprintSha256"] == "e" * 64
        approved = await client.post(
            "/api/backups/backup_1/trust",
            json={"fingerprintSha256": "d" * 64},
        )
        assert approved.status_code == 200
        assert approved.json()["backupId"] == "backup_1"
        revoked = await client.delete(
            f"/api/backups/trusted-signers/{'d' * 64}"
        )
        assert revoked.status_code == 204
        assert service.revoked_fingerprint == "d" * 64

        planned = await client.post(
            "/api/backups/backup_1/staging-plans",
            json={"jwtSecretMode": "clone"},
        )
        assert planned.status_code == 201
        plan = planned.json()
        assert plan["jwtSecretMode"] == "clone"

        status = await client.get(f"/api/backup-staging/{plan['id']}")
        assert status.json()["status"] == "planned"
        executed = await client.post(
            f"/api/backup-staging/{plan['id']}/execute",
            json={"planHash": plan["planHash"]},
        )
        assert executed.status_code == 200
        assert executed.json()["status"] == "validated"
        assert executed.json()["activationPerformed"] is False

        activated = await client.post(
            f"/api/backup-staging/{plan['id']}/activate",
            json={
                "planHash": plan["planHash"],
                "activationId": "e" * 32,
                "resumeToken": "dashboard_generated_resume_token_123456789",
            },
        )
        assert activated.status_code == 202
        assert activated.json()["activationId"] == "e" * 32
        assert activated.json()["resumeToken"] == (
            "dashboard_generated_resume_token_123456789"
        )

        activation_status = await client.get(
            f"/api/backup-activations/{'e' * 32}",
            headers={
                "X-PPBase-Activation-Token": (
                    "dashboard_generated_resume_token_123456789"
                )
            },
        )
        assert activation_status.status_code == 200
        assert activation_status.json()["status"] == "succeeded"

        restored = await client.post("/api/backups/backup_1/restore")
        assert restored.status_code == 204
        await backups_api._restore_coordinator(app).wait_idle()
        assert service.restored_backup_id == "backup_1"

    assert service.close_calls == 15


@pytest.mark.asyncio
async def test_activation_polling_uses_only_the_control_plane_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    activation_id = "1" * 32
    resume_token = "scoped_resume_token_abcdefghijklmnopqrstuvwxyz0123456789"
    try:
        store.prepare(
            activation_id=activation_id,
            plan_id="2" * 32,
            backup_id="poll-after-restart",
            plan_hash="3" * 64,
            manifest_sha256="4" * 64,
            signer_fingerprint_sha256="5" * 64,
            jwt_secret_mode="disaster_recovery",
            previous_database_url="postgresql+asyncpg://runtime@db/source",
            previous_data_dir=str(tmp_path / "source-data"),
            previous_restart_command=["serve"],
            target_database_url="postgresql+asyncpg://runtime@db/restored",
            target_data_dir=str(tmp_path / "targets" / ("2" * 32) / "data"),
            target_restart_command=["serve"],
            expected_jwt_sha256="6" * 64,
            actor_id=None,
            resume_token=resume_token,
        )
        store.mark_starting(activation_id)
        store.mark_healthy(activation_id)
    finally:
        store.close()
        root.close()

    def full_service_must_not_be_built(_request):
        raise AssertionError("activation polling initialized NativeBackupService")

    monkeypatch.setattr(backups_api, "_service", full_service_must_not_be_built)
    app = FastAPI()
    app.state.settings = SimpleNamespace(backup_control_dir=str(control))
    app.include_router(backups_api.activation_router, prefix="/api")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/api/backup-activations/{activation_id}",
            headers={"X-PPBase-Activation-Token": resume_token},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


@pytest.mark.asyncio
async def test_staging_abandon_api_commits_success_and_propagates_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=str(tmp_path / "active-data"),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
    )
    owner = NativeBackupService(object(), settings)  # type: ignore[arg-type]

    def validated_plan(backup_id: str):
        plan = owner.plans.create(
            backup_id=backup_id,
            manifest_sha256="a" * 64,
            destination_fingerprint_sha256="b" * 64,
            jwt_secret_mode="clone",
            actor_id="admin_1",
        )
        running = owner.plans.begin_execution(
            plan.plan_id,
            expected_plan_hash=plan.plan_hash,
        )
        return owner.plans.finish(
            plan.plan_id,
            status="validated",
            expected_attempt_id=running.status_data["attemptId"],
        )

    successful = validated_plan("api-abandon-success")
    wrong_hash = validated_plan("api-abandon-wrong-hash")
    running_plan = owner.plans.create(
        backup_id="api-abandon-running",
        manifest_sha256="c" * 64,
        destination_fingerprint_sha256="d" * 64,
        jwt_secret_mode="clone",
        actor_id="admin_1",
    )
    running = owner.plans.begin_execution(
        running_plan.plan_id,
        expected_plan_hash=running_plan.plan_hash,
    )

    monkeypatch.setattr(
        backups_api,
        "_service",
        lambda _request: NativeBackupService(object(), settings),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(backups_api.staging_router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            committed = await client.post(
                f"/api/backup-staging/{successful.plan_id}/abandon",
                json={"planHash": successful.plan_hash},
            )
            mismatched = await client.post(
                f"/api/backup-staging/{wrong_hash.plan_id}/abandon",
                json={"planHash": "f" * 64},
            )
            active = await client.post(
                f"/api/backup-staging/{running_plan.plan_id}/abandon",
                json={"planHash": running_plan.plan_hash},
            )

        assert committed.status_code == 204
        abandoned = owner.plans.inspect(successful.plan_id)
        assert abandoned.status == "abandoned"
        assert abandoned.status_data["actorId"] == "admin_1"
        assert abandoned.status_data["previousStatus"] == "validated"

        assert mismatched.status_code == 409
        assert mismatched.json()["detail"]["data"]["code"] == (
            "staging_plan_not_abandonable"
        )
        assert owner.plans.inspect(wrong_hash.plan_id).status == "validated"

        assert active.status_code == 409
        assert active.json()["detail"]["data"]["code"] == (
            "staging_plan_not_abandonable"
        )
        assert owner.plans.inspect(running_plan.plan_id).status == "running"
    finally:
        owner.plans.abandon_execution(
            running_plan.plan_id,
            expected_attempt_id=running.status_data["attemptId"],
        )
        owner.close()


@pytest.mark.asyncio
async def test_sdk_restore_is_app_owned_single_flight_and_returns_before_work_finishes(
    monkeypatch,
) -> None:
    service = _FakeBackupService()
    started = asyncio.Event()
    release = asyncio.Event()
    schedule_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def blocked_restore(
        backup_id,
        *,
        actor_id,
        operation_lease=None,
    ):
        assert operation_lease is not None
        assert service.mutation_active is True
        service.restored_backup_id = backup_id
        service.restore_actor_id = actor_id
        started.set()
        await release.wait()
        service.activation = PreparedBackupActivation({
            "activationId": "d" * 32,
            "backupId": backup_id,
            "status": "restart_scheduled",
            "resumeToken": "sdk-resume-token",
        }, cutover_guard=_FakeCutoverGuard())
        return service.activation

    def schedule(*args, **kwargs):
        schedule_calls.append((args, kwargs))
        return True

    service.restore_local_backup = blocked_restore
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    monkeypatch.setattr(backups_api, "schedule_process_restart", schedule)
    app = FastAPI()
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await asyncio.wait_for(
            client.post("/api/backups/sdk-visible-name.zip/restore"),
            timeout=0.5,
        )
        assert response.status_code == 204
        await asyncio.wait_for(started.wait(), timeout=0.5)

        coordinator = backups_api._restore_coordinator(app)
        assert getattr(app.state, backups_api._RESTORE_COORDINATOR_STATE) is coordinator
        assert coordinator.active is True
        assert service.mutation_entries == 1
        assert service.mutation_exits == 0
        assert service.close_calls == 0

        duplicate = await client.post("/api/backups/another.zip/restore")
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["data"]["code"] == (
            "backup_restore_in_progress"
        )
        assert service.mutation_entries == 1

        gc.collect()
        await asyncio.sleep(0)
        assert coordinator.active is True
        assert service.close_calls == 0

        release.set()
        await coordinator.wait_idle()

    assert service.resolved_restore_reference == "sdk-visible-name.zip"
    assert service.restored_backup_id == "backup_canonical_1"
    assert service.restore_actor_id == "admin_1"
    assert len(schedule_calls) == 1
    assert service.mutation_exits == 0
    await service.activation.cutover_guard.close()
    assert service.mutation_exits == 1
    assert service.close_calls == 1
    assert coordinator.active is False


@pytest.mark.asyncio
async def test_sdk_restore_missing_reference_fails_before_optimistic_response(
    monkeypatch,
) -> None:
    service = _FakeBackupService()

    async def missing_reference(_backup_id, *, operation_lease):
        assert operation_lease is not None
        assert service.mutation_active is True
        raise BackupServiceError(
            404,
            "backup_not_found",
            "The sealed local backup was not found.",
        )

    service.resolve_restore_reference = missing_reference
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    app = FastAPI()
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/backups/missing.zip/restore")

    assert response.status_code == 404
    assert response.json()["detail"]["data"]["code"] == "backup_not_found"
    coordinator = backups_api._restore_coordinator(app)
    assert coordinator.active is False
    assert service.mutation_entries == 1
    assert service.mutation_exits == 1
    assert service.close_calls == 1
    assert not hasattr(service, "restored_backup_id")


@pytest.mark.asyncio
async def test_app_restore_coordinator_shutdown_cancels_work_and_closes_resources(
    monkeypatch,
) -> None:
    service = _FakeBackupService()
    started = asyncio.Event()

    async def blocked_restore(
        _backup_id,
        *,
        actor_id,
        operation_lease=None,
    ):
        assert actor_id == "admin_1"
        assert operation_lease is not None
        started.set()
        await asyncio.Event().wait()

    service.restore_local_backup = blocked_restore
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    app = FastAPI()
    outer_router = APIRouter()
    outer_router.include_router(backups_api.router)
    app.include_router(outer_router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post("/api/backups/backup_1/restore")
            assert response.status_code == 204
            await asyncio.wait_for(started.wait(), timeout=0.5)
            coordinator = backups_api._restore_coordinator(app)
            assert coordinator.active is True

    assert coordinator.active is False
    assert service.mutation_entries == 1
    assert service.mutation_exits == 1
    assert service.close_calls == 1


@pytest.mark.asyncio
async def test_activation_is_durably_cleared_when_restart_scheduling_loses_race(
    monkeypatch,
) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    monkeypatch.setattr(
        backups_api,
        "schedule_process_restart",
        lambda *_args, **_kwargs: False,
    )
    app = FastAPI()
    app.include_router(backups_api.staging_router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/backup-staging/{'a' * 32}/activate",
            json={"planHash": "b" * 64},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["data"]["code"] == (
        "backup_activation_restart_rejected"
    )
    assert service.abandoned_activation == (
        "c" * 32,
        "restart_schedule_rejected",
    )
    assert service.activation.cutover_guard.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    (
        "../escape.zip",
        "nested/backup.zip",
        "nested\\backup.zip",
        "backup.tar",
        " backup.zip",
        "backup.zip\x00",
    ),
)
async def test_backup_create_rejects_unsafe_requested_transport_name(
    monkeypatch,
    name: str,
) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    app = FastAPI()
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/backups", json={"name": name})

    assert response.status_code == 422
    assert service.created_transport_filename is None
    assert service.close_calls == 0


@pytest.mark.asyncio
async def test_backup_get_route_switches_between_inspection_and_tokenized_zip(
    monkeypatch,
) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    app = FastAPI()
    app.state.settings = SimpleNamespace(backup_transport_chunk_size=4)
    app.include_router(backups_api.router, prefix="/api")

    async def authorize(request: Request):
        return {
            "mode": "download" if "token" in request.query_params else "inspect",
            "auth": {"id": "admin_1"},
        }

    app.dependency_overrides[backups_api._authorize_backup_read] = authorize
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        inspected = await client.get("/api/backups/backup_1")
        downloaded = await client.get("/api/backups/backup_1?token=file-token")

    assert inspected.status_code == 200
    assert inspected.json()["resourcesVerified"] is True
    assert downloaded.status_code == 200
    assert downloaded.content == b"PK\x03\x04synthetic-backup-zip"
    assert downloaded.headers["content-type"] == "application/zip"
    assert downloaded.headers["content-length"] == str(len(downloaded.content))
    assert "attachment;" in downloaded.headers["content-disposition"]
    assert downloaded.headers["content-disposition"].endswith(
        "ppbase_backup_test_backup_1.zip"
    )


@pytest.mark.asyncio
async def test_backup_inspection_resource_pagination_is_strictly_bounded(
    monkeypatch,
) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    app = FastAPI()
    app.state.settings = SimpleNamespace(backup_transport_chunk_size=4)
    app.include_router(backups_api.router, prefix="/api")

    async def authorize(_request: Request):
        return {"mode": "inspect", "auth": {"id": "admin_1"}}

    app.dependency_overrides[backups_api._authorize_backup_read] = authorize
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get(
            "/api/backups/backup_1?resourceOffset=125&resourceLimit=50"
        )
        excessive = await client.get(
            "/api/backups/backup_1?resourceOffset=0&resourceLimit=251"
        )
        negative = await client.get(
            "/api/backups/backup_1?resourceOffset=-1&resourceLimit=50"
        )

    assert page.status_code == 200
    payload = page.json()
    assert payload["resourceCount"] == 300
    assert payload["resourceOffset"] == 125
    assert payload["resourceLimit"] == 50
    assert payload["resourcesReturned"] == 50
    assert payload["hasMoreResources"] is True
    assert [item["path"] for item in payload["resources"]] == [
        f"resources/{index:04d}" for index in range(125, 175)
    ]
    assert excessive.status_code == 422
    assert negative.status_code == 422
    assert service.inspect_calls == [("backup_1", 125, 50)]


@pytest.mark.asyncio
async def test_backup_download_disconnect_closes_pinned_zip(monkeypatch) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    app = FastAPI()
    app.state.settings = SimpleNamespace(backup_transport_chunk_size=4)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/backups/backup_1",
        "raw_path": b"/api/backups/backup_1",
        "query_string": b"token=file-token",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "app": app,
    }
    request = Request(scope)
    response = await backups_api.inspect_or_download_local_backup(
        "backup_1",
        request,
        {"mode": "download", "auth": {"id": "admin_1"}},
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("synthetic client disconnect")

    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)

    assert service.last_pinned.closed is True


@pytest.mark.asyncio
async def test_backup_upload_is_streamed_after_auth_and_delete_returns_204(
    monkeypatch,
) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        backup_max_upload_bytes=1024,
        backup_multipart_overhead_bytes=1024,
    )
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        uploaded = await client.post(
            "/api/backups/upload",
            files={"file": ("backup.zip", b"PK\x03\x04payload", "application/zip")},
        )
        wrong_field = await client.post(
            "/api/backups/upload",
            files={"archive": ("backup.zip", b"PK\x03\x04payload", "application/zip")},
        )
        deleted = await client.delete("/api/backups/uploaded_backup")

    assert uploaded.status_code == 201
    assert uploaded.json()["uploadedBytes"] == len(b"PK\x03\x04payload")
    assert wrong_field.status_code == 400
    assert wrong_field.json()["detail"]["data"]["code"] == (
        "backup_upload_file_required"
    )
    assert deleted.status_code == 204
    assert service.deleted_backup_id == "uploaded_backup"


@pytest.mark.asyncio
async def test_upload_cleanup_error_never_replaces_committed_success_or_failure(
    monkeypatch,
) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    close_attempts = 0

    class FailingCloseForm:
        async def close(self):
            nonlocal close_attempts
            close_attempts += 1
            raise OSError("synthetic multipart cleanup failure")

    async def prepared_upload(_request):
        return FailingCloseForm(), SimpleNamespace(file=io.BytesIO(b"signed ZIP"))

    monkeypatch.setattr(backups_api, "_read_backup_upload", prepared_upload)
    app = FastAPI()
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        committed = await client.post("/api/backups/upload")

        async def fail_upload(_source, *, operation_lease=None):
            assert operation_lease is not None
            raise backups_api.BackupServiceError(
                409,
                "synthetic_upload_rejected",
                "Synthetic upload rejection.",
            )

        service.upload_local_backup = fail_upload
        rejected = await client.post("/api/backups/upload")

    assert committed.status_code == 201
    assert committed.json()["status"] == "sealed"
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["data"]["code"] == "synthetic_upload_rejected"
    assert close_attempts == 2


@pytest.mark.asyncio
async def test_upload_spool_preparation_reaches_quiescence_before_cancel_cleanup(
    monkeypatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class TrackedFile:
        def __init__(self):
            self._file = tempfile.TemporaryFile(mode="w+b")
            self._file.write(b"signed ZIP")
            self._file.seek(0)

        def __getattr__(self, name):
            return getattr(self._file, name)

        def close(self):
            closed.set()
            self._file.close()

    handle = TrackedFile()
    upload = backups_api.UploadFile(
        handle,
        size=len(b"signed ZIP"),
        filename="backup.zip",
    )
    form = backups_api.FormData([("file", upload)])

    class FakeParser:
        def __init__(self, *_args, **_kwargs):
            pass

        async def parse(self):
            return form

    def blocked_prepare(_handle):
        started.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(backups_api, "MultiPartParser", FakeParser)
    monkeypatch.setattr(backups_api, "_prepare_uploaded_file", blocked_prepare)
    request = SimpleNamespace(
        headers={"content-type": "multipart/form-data; boundary=x"},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(
                    backup_max_upload_bytes=1024,
                    backup_multipart_overhead_bytes=1024,
                )
            )
        ),
    )

    task = asyncio.create_task(backups_api._read_backup_upload(request))
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.05)

    assert task.done() is False
    assert closed.is_set() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() is True


@pytest.mark.asyncio
async def test_backup_upload_rejects_unauthorized_and_oversized_before_body_read(
    monkeypatch,
) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        backup_max_upload_bytes=4,
        backup_multipart_overhead_bytes=4,
    )
    app.include_router(backups_api.router, prefix="/api")
    body_consumed = False

    async def deny_admin():
        raise HTTPException(status_code=401, detail="unauthorized")

    async def streamed_body():
        nonlocal body_consumed
        body_consumed = True
        yield b"123456789"

    app.dependency_overrides[backups_api._require_backup_admin] = deny_admin
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        denied = await client.post(
            "/api/backups/upload",
            headers={"content-type": "multipart/form-data; boundary=x"},
            content=streamed_body(),
        )

    assert denied.status_code == 401
    assert body_consumed is False

    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    body_consumed = False
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        oversized = await client.post(
            "/api/backups/upload",
            headers={
                "content-type": "multipart/form-data; boundary=x",
                "content-length": "9",
            },
            content=streamed_body(),
        )

    assert oversized.status_code == 413
    assert oversized.json()["detail"]["data"]["code"] == "backup_upload_too_large"
    assert body_consumed is False

    body_consumed = False
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        chunked = await client.post(
            "/api/backups/upload",
            headers={"content-type": "multipart/form-data; boundary=x"},
            content=streamed_body(),
        )

    assert chunked.status_code == 413
    assert chunked.json()["detail"]["data"]["code"] == "backup_upload_too_large"
    assert body_consumed is True


@pytest.mark.asyncio
async def test_invalid_download_token_never_falls_back_to_authorization(
    monkeypatch,
) -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/backups/backup_1",
        "query_string": b"token=invalid",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
    }
    request = Request(scope)

    class _Session:
        async def rollback(self):
            raise AssertionError("invalid token must fail before rollback")

    async def reject_token(_session, _token):
        return None

    monkeypatch.setattr(backups_api, "verify_file_token", reject_token)
    with pytest.raises(HTTPException) as error:
        await backups_api._authorize_backup_read(
            request,
            session=_Session(),
        )

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_backup_api_closes_service_when_operation_fails(monkeypatch) -> None:
    service = _FakeBackupService()

    async def fail_listing():
        raise backups_api.BackupServiceError(
            503,
            "synthetic_backup_failure",
            "Synthetic backup failure.",
        )

    service.list_local_backups = fail_listing
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    app = FastAPI()
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/backups")

    assert response.status_code == 503
    assert response.json()["detail"]["data"]["code"] == (
        "synthetic_backup_failure"
    )
    assert service.close_calls == 1


@pytest.mark.asyncio
async def test_request_logger_does_not_read_backup_body_before_auth(
    monkeypatch,
) -> None:
    body_reads = 0
    original_body = Request.body

    async def counted_body(self):
        nonlocal body_reads
        body_reads += 1
        return await original_body(self)

    async def deny_admin():
        raise HTTPException(status_code=401, detail="unauthorized")

    monkeypatch.setattr(Request, "body", counted_body)
    app = FastAPI()
    app.include_router(backups_api.router, prefix="/api")
    app.add_middleware(RequestLoggerMiddleware)
    app.dependency_overrides[backups_api._require_backup_admin] = deny_admin

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/backups/backup_1/staging-plans",
            json={"jwtSecretMode": "clone"},
        )

    assert response.status_code == 401
    assert body_reads == 0


@pytest.mark.asyncio
async def test_backup_control_json_rejects_pathological_integer() -> None:
    app = FastAPI()
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)
    body = b'{"jwtSecretMode":' + (b"9" * 5000) + b"}"

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/backups/backup_1/staging-plans",
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    (
        b'{"jwtSecretMode":NaN}',
        b'{"jwtSecretMode":Infinity}',
        b'{"jwtSecretMode":1e1000000}',
    ),
)
async def test_backup_control_json_rejects_non_finite_numbers(body: bytes) -> None:
    app = FastAPI()
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/backups/backup_1/staging-plans",
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
