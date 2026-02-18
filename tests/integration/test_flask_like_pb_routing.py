from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, Request
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase, pb
from ppbase.api.deps import get_optional_auth, require_auth, require_record_auth
from ppbase.api import deps as deps_module
from ppbase.db import engine as engine_module


@pytest.mark.asyncio
async def test_flask_like_route_supports_typed_params_and_depends() -> None:
    app_pb = PPBase()

    def dep_value() -> str:
        return "dep-ok"

    @app_pb.get("/ext/hello/{name}")
    async def ext_hello(name: str, dep: str = Depends(dep_value)):
        return {"name": name, "dep": dep}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/hello/world")

    assert response.status_code == 200
    assert response.json() == {"name": "world", "dep": "dep-ok"}


def test_route_collision_with_core_routes_is_blocking() -> None:
    app_pb = PPBase()

    @app_pb.get("/api/health")
    async def conflict():
        return {"ok": True}

    with pytest.raises(RuntimeError, match="collisions"):
        app_pb.get_app()


@pytest.mark.asyncio
async def test_side_effect_module_and_register_function_can_mix(tmp_path: Path, monkeypatch) -> None:
    pb._reset_for_tests()
    module_name = "tmp_hooks_mix"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(
        "\n".join(
            [
                "from ppbase import pb",
                "",
                "@pb.get('/ext/side-effect')",
                "async def side_effect_route():",
                "    return {'source': 'side-effect'}",
                "",
                "def register(app_pb):",
                "    @app_pb.get('/ext/register')",
                "    async def register_route():",
                "        return {'source': 'register'}",
            ]
        )
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module(module_name)
    module.register(pb)

    app = pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        side = await client.get("/ext/side-effect")
        reg = await client.get("/ext/register")

    assert side.status_code == 200
    assert reg.status_code == 200
    assert side.json()["source"] == "side-effect"
    assert reg.json()["source"] == "register"

    pb._reset_for_tests()


@pytest.mark.asyncio
async def test_route_can_use_optional_auth_dependency_helper() -> None:
    app_pb = PPBase()

    @app_pb.get("/ext/whoami")
    async def whoami(auth: dict | None = app_pb.optional_auth()):
        return {"id": auth.get("id") if auth else None}

    app = app_pb.get_app()
    app.dependency_overrides[get_optional_auth] = lambda: {"id": "user_1", "type": "authRecord"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/whoami")

    assert response.status_code == 200
    assert response.json() == {"id": "user_1"}


@pytest.mark.asyncio
async def test_route_can_use_require_auth_dependency_helpers() -> None:
    app_pb = PPBase()

    @app_pb.get("/ext/need-auth")
    async def need_auth(auth: dict = app_pb.require_auth()):
        return {"id": auth.get("id"), "type": auth.get("type")}

    @app_pb.get("/ext/need-record-auth")
    async def need_record_auth(auth: dict = app_pb.require_record_auth()):
        return {"id": auth.get("id"), "type": auth.get("type")}

    app = app_pb.get_app()
    app.dependency_overrides[require_auth] = lambda: {"id": "any_1", "type": "admin"}
    app.dependency_overrides[require_record_auth] = (
        lambda: {"id": "user_42", "type": "authRecord"}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth_response = await client.get("/ext/need-auth")
        record_auth_response = await client.get("/ext/need-record-auth")

    assert auth_response.status_code == 200
    assert auth_response.json() == {"id": "any_1", "type": "admin"}
    assert record_auth_response.status_code == 200
    assert record_auth_response.json() == {"id": "user_42", "type": "authRecord"}


@pytest.mark.asyncio
async def test_route_helpers_can_enrich_records_with_query_context(monkeypatch) -> None:
    app_pb = PPBase()
    collection = SimpleNamespace(id="posts_id", name="posts", type="base", schema=[])
    engine = object()
    captured: dict[str, object] = {}

    async def fake_get_optional_auth(_request, session=None):
        return {
            "type": "authRecord",
            "id": "u1",
            "collectionName": "users",
            "collectionId": "users_id",
            "email": "u1@example.com",
        }

    async def fake_get_async_session():
        yield object()

    async def fake_resolve_collection(_engine, _id_or_name):
        return collection

    async def fake_get_all_collections(_engine):
        return [collection]

    async def fake_expand_records(
        _engine,
        _collection,
        records,
        expand_str,
        _all_collections,
        request_context=None,
    ):
        captured["expand"] = expand_str
        captured["auth_id"] = (
            request_context.get("auth", {}).get("id")
            if isinstance(request_context, dict)
            else None
        )
        for record in records:
            record["expand"] = {"author": {"id": "a1", "name": "Author"}}
        return records

    monkeypatch.setattr(deps_module, "get_optional_auth", fake_get_optional_auth)
    monkeypatch.setattr(engine_module, "get_async_session", fake_get_async_session)
    monkeypatch.setattr(engine_module, "get_engine", lambda: engine)

    import ppbase.services.record_service as record_service_module
    import ppbase.services.expand_service as expand_service_module

    monkeypatch.setattr(record_service_module, "resolve_collection", fake_resolve_collection)
    monkeypatch.setattr(record_service_module, "get_all_collections", fake_get_all_collections)
    monkeypatch.setattr(expand_service_module, "expand_records", fake_expand_records)

    @app_pb.get("/ext/helpers/enrich")
    async def route_enrich(request: Request):
        items = [
            {
                "id": "r1",
                "collectionId": "posts_id",
                "collectionName": "posts",
                "title": "hello",
                "secret": "hidden",
            }
        ]
        enriched = await app_pb.apis.enrich_records(
            request,
            items,
            collection="posts",
            default_expand="author",
        )
        return {"items": enriched}

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/helpers/enrich?fields=id,title,expand")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": "r1",
                "title": "hello",
                "expand": {"author": {"id": "a1", "name": "Author"}},
            }
        ]
    }
    assert captured["expand"] == "author"
    assert captured["auth_id"] == "u1"


@pytest.mark.asyncio
async def test_route_helpers_record_auth_response_matches_auth_shape(monkeypatch) -> None:
    app_pb = PPBase()
    collection = SimpleNamespace(id="users_id", name="users", type="auth", schema=[])
    engine = object()
    calls: list[str] = []

    async def fake_get_optional_auth(_request, session=None):
        return None

    async def fake_get_async_session():
        yield object()

    async def fake_resolve_collection(_engine, _id_or_name):
        return collection

    async def fake_generate_record_auth_token(_engine, _collection, record_id, _settings):
        assert record_id == "u1"
        return "tok_generated"

    monkeypatch.setattr(deps_module, "get_optional_auth", fake_get_optional_auth)
    monkeypatch.setattr(engine_module, "get_async_session", fake_get_async_session)
    monkeypatch.setattr(engine_module, "get_engine", lambda: engine)

    import ppbase.services.record_service as record_service_module
    import ppbase.services.record_auth_service as record_auth_service_module

    monkeypatch.setattr(record_service_module, "resolve_collection", fake_resolve_collection)
    monkeypatch.setattr(
        record_auth_service_module,
        "generate_record_auth_token",
        fake_generate_record_auth_token,
    )

    @app_pb.on_record_auth_request("users")
    async def auth_success_hook(event):
        calls.append(event.auth_method or "")
        event.token = f"{event.token}_hooked"
        return await event.next()

    @app_pb.get("/ext/helpers/auth-response")
    async def route_auth_response(request: Request):
        return await app_pb.apis.record_auth_response(
            request,
            {
                "id": "u1",
                "collectionId": "users_id",
                "collectionName": "users",
                "email": "u1@example.com",
            },
            collection="users",
            auth_method="password",
            meta={"source": "custom"},
        )

    app = app_pb.get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/helpers/auth-response?fields=id,email")

    assert response.status_code == 200
    assert response.json() == {
        "token": "tok_generated_hooked",
        "record": {
            "id": "u1",
            "email": "u1@example.com",
        },
        "meta": {"source": "custom"},
    }
    assert calls == ["password"]
