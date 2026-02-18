from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase, pb
from ppbase.api.deps import get_optional_auth, require_auth, require_record_auth


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
