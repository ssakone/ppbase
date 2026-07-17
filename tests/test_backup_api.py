from __future__ import annotations

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest
from starlette.requests import Request

from ppbase.api import backups as backups_api
from ppbase.middleware.request_logger import RequestLoggerMiddleware


class _FakeBackupService:
    def get_identity(self):
        return {"algorithm": "Ed25519", "fingerprintSha256": "f" * 64}

    async def create_local_backup(self, *, actor_id=None):
        return {"id": "backup_1", "status": "sealed", "actorId": actor_id}

    async def list_local_backups(self):
        return [{"id": "backup_1", "status": "sealed"}]

    async def inspect_local_backup(self, backup_id):
        return {"id": backup_id, "resourcesVerified": True}

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


@pytest.mark.asyncio
async def test_backup_api_exposes_local_and_staging_workflow(monkeypatch) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)

    app = FastAPI()
    app.include_router(backups_api.router, prefix="/api")
    app.include_router(backups_api.staging_router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
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

        listed = await client.get("/api/backups")
        assert listed.json() == [{"id": "backup_1", "status": "sealed"}]
        inspected = await client.get("/api/backups/backup_1")
        assert inspected.json()["resourcesVerified"] is True

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
