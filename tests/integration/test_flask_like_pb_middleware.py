from __future__ import annotations

import pytest
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase
from ppbase.api import deps as deps_module
from ppbase.db import engine as engine_module


@pytest.mark.asyncio
async def test_global_middlewares_follow_priority_and_wrap_handler() -> None:
    app_pb = PPBase()
    calls: list[str] = []

    @app_pb.middleware(priority=10)
    async def high(event):
        calls.append("high:before")
        result = await event.next()
        calls.append("high:after")
        return result

    @app_pb.middleware(priority=0)
    async def low(event):
        calls.append("low:before")
        result = await event.next()
        calls.append("low:after")
        return result

    @app_pb.get("/ext/mw/priority")
    async def handler():
        calls.append("handler")
        return {"ok": True}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/mw/priority")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert calls == [
        "high:before",
        "low:before",
        "handler",
        "low:after",
        "high:after",
    ]


@pytest.mark.asyncio
async def test_global_middleware_can_short_circuit_route() -> None:
    app_pb = PPBase()
    called = False

    @app_pb.middleware()
    async def block(_event):
        return JSONResponse(
            status_code=403,
            content={"status": 403, "message": "blocked", "data": {}},
        )

    @app_pb.get("/ext/mw/blocked")
    async def blocked_route():
        nonlocal called
        called = True
        return {"ok": True}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/mw/blocked")

    assert response.status_code == 403
    assert response.json()["message"] == "blocked"
    assert called is False


@pytest.mark.asyncio
async def test_group_middlewares_apply_only_to_group_routes() -> None:
    app_pb = PPBase()
    calls: list[str] = []
    group = app_pb.group("/ext/group")

    @group.middleware()
    async def only_group(event):
        calls.append(event.path)
        return await event.next()

    @group.get("/inside")
    async def inside():
        return {"scope": "inside"}

    @app_pb.get("/ext/outside")
    async def outside():
        return {"scope": "outside"}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        inside_response = await client.get("/ext/group/inside")
        outside_response = await client.get("/ext/outside")

    assert inside_response.status_code == 200
    assert outside_response.status_code == 200
    assert calls == ["/ext/group/inside"]


@pytest.mark.asyncio
async def test_route_specific_middlewares_can_extend_global_chain() -> None:
    app_pb = PPBase()
    calls: list[str] = []

    @app_pb.middleware(priority=5)
    async def global_mw(event):
        calls.append("global")
        return await event.next()

    async def route_mw(event):
        calls.append("route")
        return await event.next()

    @app_pb.get("/ext/mw/route", middlewares=[route_mw])
    async def route_handler():
        calls.append("handler")
        return {"ok": True}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/mw/route")

    assert response.status_code == 200
    assert calls == ["global", "route", "handler"]


@pytest.mark.asyncio
async def test_route_middlewares_receive_loaded_auth_helpers(monkeypatch) -> None:
    app_pb = PPBase()

    async def fake_get_optional_auth(_request, session=None):
        return {"type": "admin", "id": "admin_1"}

    async def fake_get_async_session():
        yield object()

    monkeypatch.setattr(deps_module, "get_optional_auth", fake_get_optional_auth)
    monkeypatch.setattr(engine_module, "get_async_session", fake_get_async_session)

    @app_pb.middleware()
    async def auth_guard(event):
        assert event.has_auth() is True
        assert event.is_superuser() is True
        assert event.is_admin() is True
        return await event.next()

    @app_pb.get("/ext/mw/auth")
    async def auth_route():
        return {"ok": True}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/mw/auth")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_global_middleware_can_filter_by_path_and_method() -> None:
    app_pb = PPBase()
    calls: list[str] = []

    @app_pb.middleware(path="/ext/mw/filtered/*", methods=["POST"])
    async def filtered(event):
        calls.append(f"{event.method}:{event.path}")
        return await event.next()

    @app_pb.get("/ext/mw/filtered/item")
    async def filtered_get():
        return {"method": "GET"}

    @app_pb.post("/ext/mw/filtered/item")
    async def filtered_post():
        return {"method": "POST"}

    @app_pb.post("/ext/mw/other")
    async def other_post():
        return {"method": "POST"}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response_get = await client.get("/ext/mw/filtered/item")
        response_other = await client.post("/ext/mw/other")
        response_post = await client.post("/ext/mw/filtered/item")

    assert response_get.status_code == 200
    assert response_other.status_code == 200
    assert response_post.status_code == 200
    assert calls == ["POST:/ext/mw/filtered/item"]


@pytest.mark.asyncio
async def test_group_middleware_path_filter_is_group_relative() -> None:
    app_pb = PPBase()
    calls: list[str] = []
    group = app_pb.group("/ext/group-filter")

    @group.middleware(path="secure/*")
    async def secure_only(event):
        calls.append(event.path)
        return await event.next()

    @group.get("/secure/ping")
    async def secure_ping():
        return {"ok": "secure"}

    @group.get("/open/ping")
    async def open_ping():
        return {"ok": "open"}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        secure_response = await client.get("/ext/group-filter/secure/ping")
        open_response = await client.get("/ext/group-filter/open/ping")

    assert secure_response.status_code == 200
    assert open_response.status_code == 200
    assert calls == ["/ext/group-filter/secure/ping"]


@pytest.mark.asyncio
async def test_route_unbind_can_exclude_global_middleware_by_id() -> None:
    app_pb = PPBase()
    calls: list[str] = []

    @app_pb.middleware(id="track-all")
    async def track_all(event):
        calls.append(event.path)
        return await event.next()

    @app_pb.get("/ext/unbind/a")
    async def route_a():
        return {"id": "a"}

    @app_pb.get("/ext/unbind/b", unbind=["track-all"])
    async def route_b():
        return {"id": "b"}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response_a = await client.get("/ext/unbind/a")
        response_b = await client.get("/ext/unbind/b")

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert calls == ["/ext/unbind/a"]


@pytest.mark.asyncio
async def test_group_unbind_removes_global_middleware_for_group_routes() -> None:
    app_pb = PPBase()
    calls: list[str] = []

    @app_pb.middleware(id="global-auth")
    async def global_auth(event):
        calls.append(event.path)
        return await event.next()

    group = app_pb.group("/ext/group-unbind").unbind("global-auth")

    @group.get("/inside")
    async def inside():
        return {"scope": "inside"}

    @app_pb.get("/ext/group-unbind-outside")
    async def outside():
        return {"scope": "outside"}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        inside_response = await client.get("/ext/group-unbind/inside")
        outside_response = await client.get("/ext/group-unbind-outside")

    assert inside_response.status_code == 200
    assert outside_response.status_code == 200
    assert calls == ["/ext/group-unbind-outside"]


@pytest.mark.asyncio
async def test_builtin_middleware_require_guest_and_auth(monkeypatch) -> None:
    app_pb = PPBase()

    async def fake_get_optional_auth(request, session=None):
        auth_header = request.headers.get("authorization", "")
        if auth_header == "Bearer users_u1":
            return {
                "type": "authRecord",
                "id": "u1",
                "collectionName": "users",
                "collectionId": "users_id",
            }
        return None

    async def fake_get_async_session():
        yield object()

    monkeypatch.setattr(deps_module, "get_optional_auth", fake_get_optional_auth)
    monkeypatch.setattr(engine_module, "get_async_session", fake_get_async_session)

    @app_pb.get("/ext/apis/guest-only", middlewares=[app_pb.apis.require_guest_only()])
    async def guest_only():
        return {"ok": True}

    @app_pb.get("/ext/apis/auth-only", middlewares=[app_pb.apis.require_auth()])
    async def auth_only():
        return {"ok": True}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        guest_ok = await client.get("/ext/apis/guest-only")
        guest_denied = await client.get(
            "/ext/apis/guest-only",
            headers={"Authorization": "Bearer users_u1"},
        )
        auth_denied = await client.get("/ext/apis/auth-only")
        auth_ok = await client.get(
            "/ext/apis/auth-only",
            headers={"Authorization": "Bearer users_u1"},
        )

    assert guest_ok.status_code == 200
    assert guest_denied.status_code == 403
    assert auth_denied.status_code == 401
    assert auth_ok.status_code == 200


@pytest.mark.asyncio
async def test_builtin_middleware_require_auth_collection_filter(monkeypatch) -> None:
    app_pb = PPBase()

    async def fake_get_optional_auth(request, session=None):
        auth_header = request.headers.get("authorization", "")
        if auth_header == "Bearer users_u1":
            return {
                "type": "authRecord",
                "id": "u1",
                "collectionName": "users",
                "collectionId": "users_id",
            }
        if auth_header == "Bearer members_m1":
            return {
                "type": "authRecord",
                "id": "m1",
                "collectionName": "members",
                "collectionId": "members_id",
            }
        if auth_header == "Bearer admin":
            return {"type": "admin", "id": "admin_1"}
        return None

    async def fake_get_async_session():
        yield object()

    monkeypatch.setattr(deps_module, "get_optional_auth", fake_get_optional_auth)
    monkeypatch.setattr(engine_module, "get_async_session", fake_get_async_session)

    @app_pb.get(
        "/ext/apis/users-only",
        middlewares=[app_pb.apis.require_auth("users")],
    )
    async def users_only():
        return {"ok": True}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        users_ok = await client.get(
            "/ext/apis/users-only",
            headers={"Authorization": "Bearer users_u1"},
        )
        members_denied = await client.get(
            "/ext/apis/users-only",
            headers={"Authorization": "Bearer members_m1"},
        )
        admin_denied = await client.get(
            "/ext/apis/users-only",
            headers={"Authorization": "Bearer admin"},
        )

    assert users_ok.status_code == 200
    assert members_denied.status_code == 403
    assert admin_denied.status_code == 403


@pytest.mark.asyncio
async def test_builtin_middleware_require_superuser_or_owner(monkeypatch) -> None:
    app_pb = PPBase()

    async def fake_get_optional_auth(request, session=None):
        auth_header = request.headers.get("authorization", "")
        if auth_header == "Bearer users_u1":
            return {
                "type": "authRecord",
                "id": "u1",
                "collectionName": "users",
                "collectionId": "users_id",
            }
        if auth_header == "Bearer members_m2":
            return {
                "type": "authRecord",
                "id": "m2",
                "collectionName": "members",
                "collectionId": "members_id",
            }
        if auth_header == "Bearer admin":
            return {"type": "admin", "id": "admin_1"}
        return None

    async def fake_get_async_session():
        yield object()

    monkeypatch.setattr(deps_module, "get_optional_auth", fake_get_optional_auth)
    monkeypatch.setattr(engine_module, "get_async_session", fake_get_async_session)

    @app_pb.get(
        "/ext/apis/super-or-owner/{id}",
        middlewares=[app_pb.apis.require_superuser_or_owner_auth()],
    )
    async def super_or_owner():
        return {"ok": True}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        no_auth = await client.get("/ext/apis/super-or-owner/u1")
        owner_ok = await client.get(
            "/ext/apis/super-or-owner/u1",
            headers={"Authorization": "Bearer users_u1"},
        )
        owner_denied = await client.get(
            "/ext/apis/super-or-owner/u2",
            headers={"Authorization": "Bearer users_u1"},
        )
        superuser_ok = await client.get(
            "/ext/apis/super-or-owner/u2",
            headers={"Authorization": "Bearer admin"},
        )
        non_owner_denied = await client.get(
            "/ext/apis/super-or-owner/u1",
            headers={"Authorization": "Bearer members_m2"},
        )

    assert no_auth.status_code == 401
    assert owner_ok.status_code == 200
    assert owner_denied.status_code == 403
    assert superuser_ok.status_code == 200
    assert non_owner_denied.status_code == 403


@pytest.mark.asyncio
async def test_route_request_store_is_shared_between_middleware_and_handler() -> None:
    app_pb = PPBase()

    @app_pb.middleware(path="/ext/store")
    async def seed_store(event):
        event.set("traceId", "trace-123")
        event.set("counter", 1)
        assert event.has("traceId") is True
        event.set("counter", int(event.get("counter", 0)) + 1)
        return await event.next()

    @app_pb.get("/ext/store")
    async def read_store(store: dict = app_pb.request_store()):
        return {
            "traceId": store.get("traceId"),
            "counter": store.get("counter"),
            "missing": store.get("missing", "fallback"),
        }

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/store")

    assert response.status_code == 200
    assert response.json() == {
        "traceId": "trace-123",
        "counter": 2,
        "missing": "fallback",
    }
