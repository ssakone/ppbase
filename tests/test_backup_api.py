from __future__ import annotations

import asyncio
from contextlib import contextmanager
import io
import tempfile
import threading
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest
from starlette.requests import ClientDisconnect, Request

from ppbase.api import backups as backups_api
from ppbase.backup.transport import PinnedBackupZip
from ppbase.middleware.request_logger import RequestLoggerMiddleware


class _FakeBackupService:
    def __init__(self):
        self.close_calls = 0
        self.created_transport_filename = None

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

    async def inspect_local_backup(self, backup_id):
        return {"id": backup_id, "resourcesVerified": True}

    @contextmanager
    def mutation_operation(self):
        yield SimpleNamespace(scope="global", mode="exclusive")

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

    assert service.close_calls == 10


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
