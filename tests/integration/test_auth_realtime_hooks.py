from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from ppbase.api import files as files_api
from ppbase.api import record_auth as record_auth_api
from ppbase.api import realtime as realtime_api
from ppbase.ext.events import FileTokenRequestEvent, RecordAuthRequestEvent
from ppbase.ext.flask_like_pb import FlaskLikePB
from ppbase.ext.registry import (
    ExtensionRegistry,
    HOOK_RECORD_AUTH_WITH_OTP_REQUEST,
    HOOK_FILE_DOWNLOAD_REQUEST,
    HOOK_FILE_TOKEN_REQUEST,
    HOOK_RECORD_REQUEST_OTP_REQUEST,
    HOOK_REALTIME_CONNECT_REQUEST,
    HOOK_REALTIME_MESSAGE_SEND,
    HOOK_REALTIME_SUBSCRIBE_REQUEST,
    HOOK_RECORD_AUTH_REQUEST,
    HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
)
from ppbase.services.realtime_service import SubscriptionManager, broadcast_record_change

@pytest.mark.asyncio
async def test_record_auth_request_hook_triggers_specific_only() -> None:
    app = FastAPI()
    extensions = ExtensionRegistry()
    app.state.extension_registry = extensions

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth",
        "headers": [],
        "app": app,
    }
    request = Request(scope)
    event = RecordAuthRequestEvent(
        app=app,
        request=request,
        collection=SimpleNamespace(name="users", id="users_id"),
        collection_id_or_name="users",
        method="password",
        body={"identity": "u@example.com"},
    )

    calls: list[str] = []

    async def _generic(e):
        calls.append(f"generic:{e.method}")
        return await e.next()

    async def _specific(e):
        calls.append(f"specific:{e.collection.name}")
        return await e.next()

    async def _default(_: RecordAuthRequestEvent):
        calls.append("default")
        return "ok"

    extensions.hooks.get(HOOK_RECORD_AUTH_REQUEST).bind_func(_generic)
    extensions.hooks.get(HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST).bind_func(_specific)

    result = await record_auth_api._trigger_record_auth_request_hook(
        request,
        HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
        event,
        _default,
    )

    assert result == "ok"
    assert calls == ["specific:users", "default"]


@pytest.mark.asyncio
async def test_record_auth_success_hook_triggers_generic_and_can_mutate_payload() -> None:
    app = FastAPI()
    extensions = ExtensionRegistry()
    app.state.extension_registry = extensions

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth",
        "headers": [],
        "app": app,
    }
    request = Request(scope)
    event = RecordAuthRequestEvent(
        app=app,
        request=request,
        collection=SimpleNamespace(name="users", id="users_id"),
        collection_id_or_name="users",
        method="password",
        auth_method="password",
    )

    calls: list[str] = []

    async def _generic(e):
        calls.append(f"generic:{e.auth_method}")
        e.token = "changed_token"
        return await e.next()

    extensions.hooks.get(HOOK_RECORD_AUTH_REQUEST).bind_func(_generic)

    result = await record_auth_api._trigger_record_auth_success_hook(
        request,
        event,
        {
            "token": "orig_token",
            "record": {"id": "u1"},
        },
    )

    assert result["token"] == "changed_token"
    assert result["record"]["id"] == "u1"
    assert calls == ["generic:password"]


@pytest.mark.asyncio
async def test_realtime_connect_and_subscribe_hooks_are_triggered(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(realtime_api.router)
    extensions = ExtensionRegistry()
    manager = SubscriptionManager(extension_registry=extensions)
    app.state.extension_registry = extensions
    app.state.subscription_manager = manager
    app.dependency_overrides[realtime_api.get_optional_auth] = lambda: None

    connect_calls = 0
    subscribe_calls = 0

    async def _connect(e):
        nonlocal connect_calls
        connect_calls += 1
        return await e.next()

    async def _subscribe(e):
        nonlocal subscribe_calls
        subscribe_calls += 1
        return await e.next()

    extensions.hooks.get(HOOK_REALTIME_CONNECT_REQUEST).bind_func(_connect)
    extensions.hooks.get(HOOK_REALTIME_SUBSCRIBE_REQUEST).bind_func(_subscribe)

    async def _fake_resolve_collection(_engine, collection_name):
        return SimpleNamespace(id=f"{collection_name}_id", name=collection_name)

    monkeypatch.setattr(realtime_api, "get_engine", lambda: object())
    monkeypatch.setattr(realtime_api, "resolve_collection", _fake_resolve_collection)

    connect_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/realtime",
            "headers": [],
            "app": app,
        }
    )
    connect_response = await realtime_api.realtime_connect(
        connect_request,
        subscription_manager=manager,
    )
    assert connect_response.status_code == 200
    assert connect_calls >= 1

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        client_id = manager.register_client()
        response = await client.post(
            "/api/realtime",
            json={
                "clientId": client_id,
                "subscriptions": ["posts/*"],
            },
        )
        assert response.status_code == 204, response.text

    assert subscribe_calls >= 1
    session = manager.get_session(client_id)
    assert session is not None
    assert len(session.subscriptions) == 1
    assert session.subscriptions[0].topic == "posts/*"


@pytest.mark.asyncio
async def test_realtime_message_send_hook_can_customize_payload() -> None:
    extensions = ExtensionRegistry()
    manager = SubscriptionManager(extension_registry=extensions)
    client_id = manager.register_client()
    await manager.add_subscription(client_id, "posts/*")
    session = manager.get_session(client_id)
    assert session is not None

    async def _decorate_message(e):
        e.data["hooked"] = True
        return await e.next()

    extensions.hooks.get(HOOK_REALTIME_MESSAGE_SEND).bind_func(_decorate_message)

    await broadcast_record_change(
        manager,
        "posts",
        "rec1",
        "create",
        {"id": "rec1", "title": "hello"},
    )

    event = await asyncio.wait_for(session.response_queue.get(), timeout=1.0)
    assert event["topic"] == "posts/*"
    assert event["data"]["hooked"] is True


@pytest.mark.asyncio
async def test_file_token_request_hook_is_triggered(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(files_api.router, prefix="/api/files")
    extensions = ExtensionRegistry()
    app.state.extension_registry = extensions

    app.dependency_overrides[files_api.require_auth] = lambda: {
        "id": "admin_1",
        "type": "admin",
        "email": "admin@example.com",
    }

    async def _fake_session():
        yield object()

    app.dependency_overrides[files_api.get_session] = _fake_session

    calls: list[str] = []

    async def _file_token_hook(e):
        calls.append(f"{e.auth_type()}:{e.auth_id()}")
        result = await e.next()
        if isinstance(result, dict) and "token" in result:
            result["token"] = f"{result['token']}_hooked"
        return result

    async def _fake_create_file_token(_session, _auth):
        return "token_abc"

    monkeypatch.setattr(files_api, "_create_file_token_for_auth", _fake_create_file_token)
    async def _fake_resolve_file_token_event_context(_session, _auth):
        return (
            SimpleNamespace(id="_superusers", name="_superusers"),
            {"id": "admin_1", "email": "admin@example.com"},
        )

    monkeypatch.setattr(
        files_api,
        "_resolve_file_token_event_context",
        _fake_resolve_file_token_event_context,
    )
    extensions.hooks.get(HOOK_FILE_TOKEN_REQUEST).bind_func(_file_token_hook)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/files/token")

    assert response.status_code == 200, response.text
    assert response.json() == {"token": "token_abc_hooked"}
    assert calls == ["admin:admin_1"]


@pytest.mark.asyncio
async def test_file_download_hook_can_customize_disposition(tmp_path) -> None:
    app = FastAPI()
    app.include_router(files_api.router, prefix="/api/files")
    extensions = ExtensionRegistry()
    app.state.extension_registry = extensions
    app.state.settings = SimpleNamespace(data_dir=str(tmp_path))

    collection = SimpleNamespace(
        id="docs_id",
        name="docs",
        schema=[
            {
                "name": "doc",
                "type": "file",
                "required": False,
                "options": {"maxSelect": 1},
            }
        ],
        view_rule="",
    )
    row = {"id": "rec1", "doc": "original.txt"}

    storage_dir = tmp_path / "storage" / collection.id / "rec1"
    storage_dir.mkdir(parents=True, exist_ok=True)
    file_path = storage_dir / "original.txt"
    file_path.write_bytes(b"hello file body")

    class _Result:
        def mappings(self):
            class _Mappings:
                def first(self_inner):
                    return row

            return _Mappings()

    class _Session:
        async def execute(self, *_args, **_kwargs):
            return _Result()

    async def _fake_session():
        yield _Session()

    app.dependency_overrides[files_api.get_session] = _fake_session

    async def _fake_resolve_collection(_session, _collection_name):
        return collection

    import ppbase.api.files as files_module

    original_resolve_collection = files_module.resolve_collection
    files_module.resolve_collection = _fake_resolve_collection

    calls: list[str] = []

    async def _download_hook(e):
        calls.append(e.served_name)
        assert e.file_field is not None
        assert e.file_field.get("name") == "doc"
        e.force_download = True
        e.served_name = "renamed.txt"
        return await e.next()

    extensions.hooks.get(HOOK_FILE_DOWNLOAD_REQUEST).bind_func(_download_hook)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/files/docs/rec1/original.txt")
    finally:
        files_module.resolve_collection = original_resolve_collection

    assert response.status_code == 200, response.text
    assert response.content == b"hello file body"
    disposition = response.headers.get("content-disposition", "")
    assert "attachment" in disposition.lower()
    assert "renamed.txt" in disposition
    assert calls == ["original.txt"]


@pytest.mark.asyncio
async def test_file_download_hook_can_short_circuit_response(tmp_path) -> None:
    app = FastAPI()
    app.include_router(files_api.router, prefix="/api/files")
    extensions = ExtensionRegistry()
    app.state.extension_registry = extensions
    app.state.settings = SimpleNamespace(data_dir=str(tmp_path))

    collection = SimpleNamespace(
        id="docs_id",
        name="docs",
        schema=[
            {
                "name": "doc",
                "type": "file",
                "required": False,
                "options": {"maxSelect": 1},
            }
        ],
        view_rule="",
    )
    row = {"id": "rec1", "doc": "original.txt"}

    storage_dir = tmp_path / "storage" / collection.id / "rec1"
    storage_dir.mkdir(parents=True, exist_ok=True)
    file_path = storage_dir / "original.txt"
    file_path.write_bytes(b"hello file body")

    class _Result:
        def mappings(self):
            class _Mappings:
                def first(self_inner):
                    return row

            return _Mappings()

    class _Session:
        async def execute(self, *_args, **_kwargs):
            return _Result()

    async def _fake_session():
        yield _Session()

    app.dependency_overrides[files_api.get_session] = _fake_session

    async def _fake_resolve_collection(_session, _collection_name):
        return collection

    import ppbase.api.files as files_module

    original_resolve_collection = files_module.resolve_collection
    files_module.resolve_collection = _fake_resolve_collection

    async def _download_hook(_e):
        return JSONResponse({"ok": True}, status_code=200)

    extensions.hooks.get(HOOK_FILE_DOWNLOAD_REQUEST).bind_func(_download_hook)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/files/docs/rec1/original.txt")
    finally:
        files_module.resolve_collection = original_resolve_collection

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_flask_like_pb_on_file_token_request_decorator_registers_hook() -> None:
    app_pb = FlaskLikePB()
    calls: list[str] = []

    @app_pb.on_file_token_request("users")
    async def _hook(e):
        calls.append(e.auth_id() or "")
        return await e.next()

    event = FileTokenRequestEvent(
        app=None,
        collection=SimpleNamespace(name="users", id="users_id"),
        auth={"id": "admin_2", "type": "admin"},
    )

    async def _default(_: FileTokenRequestEvent):
        return {"token": "token_xyz"}

    hook = app_pb._extensions.hooks.get(HOOK_FILE_TOKEN_REQUEST)  # noqa: SLF001
    result = await hook.trigger(event, _default)

    assert result == {"token": "token_xyz"}
    assert calls == ["admin_2"]

    event_unmatched = FileTokenRequestEvent(
        app=None,
        collection=SimpleNamespace(name="admins", id="admins_id"),
        auth={"id": "admin_3", "type": "admin"},
    )
    result_unmatched = await hook.trigger(event_unmatched, _default)
    assert result_unmatched == {"token": "token_xyz"}
    assert calls == ["admin_2"]


@pytest.mark.asyncio
async def test_request_otp_hook_is_triggered_and_can_mutate_payload(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(record_auth_api.router)
    app.state.extension_registry = ExtensionRegistry()
    app.state.settings = SimpleNamespace()

    collection = SimpleNamespace(
        id="users_id",
        name="users",
        type="auth",
        options={"otp": {"enabled": True}},
    )

    monkeypatch.setattr(record_auth_api, "get_engine", lambda: object())

    async def _fake_resolve_collection(_engine, _collection_name):
        return collection

    monkeypatch.setattr(record_auth_api, "resolve_collection", _fake_resolve_collection)

    captured_email: list[str] = []

    async def _fake_request_otp(_engine, _collection, email, _settings):
        captured_email.append(email)
        return "otp_abc", False

    import ppbase.services.record_auth_service as record_auth_service

    monkeypatch.setattr(record_auth_service, "request_otp", _fake_request_otp)

    async def _request_otp_hook(e):
        assert e.email == "original@example.com"
        e.email = "hooked@example.com"
        return await e.next()

    app.state.extension_registry.hooks.get(HOOK_RECORD_REQUEST_OTP_REQUEST).bind_func(
        _request_otp_hook
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/collections/users/request-otp",
            json={"email": "original@example.com"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["otpId"] == "otp_abc"
    assert captured_email == ["hooked@example.com"]


@pytest.mark.asyncio
async def test_auth_with_otp_hooks_trigger_generic_and_specific(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(record_auth_api.router)
    app.state.extension_registry = ExtensionRegistry()
    app.state.settings = SimpleNamespace()

    collection = SimpleNamespace(
        id="users_id",
        name="users",
        type="auth",
        options={"otp": {"enabled": True}},
    )

    monkeypatch.setattr(record_auth_api, "get_engine", lambda: object())

    async def _fake_resolve_collection(_engine, _collection_name):
        return collection

    monkeypatch.setattr(record_auth_api, "resolve_collection", _fake_resolve_collection)

    async def _fake_passes_auth_rule(*_args, **_kwargs):
        return True

    monkeypatch.setattr(record_auth_api, "_passes_auth_rule", _fake_passes_auth_rule)

    captured_password: list[str] = []

    async def _fake_auth_with_otp(_engine, _collection, _otp_id, password, _settings):
        captured_password.append(password)
        return {"token": "token_otp", "record": {"id": "u1", "email": "u1@example.com"}}

    import ppbase.services.record_auth_service as record_auth_service

    monkeypatch.setattr(record_auth_service, "auth_with_otp", _fake_auth_with_otp)

    calls: list[str] = []

    async def _generic_hook(e):
        calls.append(f"generic:{e.method}")
        return await e.next()

    async def _specific_hook(e):
        calls.append("specific")
        assert e.otp == "original-pass"
        e.otp = "hooked-pass"
        return await e.next()

    app.state.extension_registry.hooks.get(HOOK_RECORD_AUTH_REQUEST).bind_func(_generic_hook)
    app.state.extension_registry.hooks.get(HOOK_RECORD_AUTH_WITH_OTP_REQUEST).bind_func(
        _specific_hook
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/collections/users/auth-with-otp",
            json={"otpId": "otp_1", "password": "original-pass"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["token"] == "token_otp"
    assert calls == ["specific", "generic:otp"]
    assert captured_password == ["hooked-pass"]
