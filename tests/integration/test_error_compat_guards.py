"""The ``pb.apis`` route guards in both error modes.

PPBase's guards follow current PocketBase; in the opt-in ``"0.22"`` mode
each one answers the message of its PocketBase 0.22.34 counterpart
(``apis/middlewares.go``), as measured on the 0.22.34 binary by
``tests/pocketbase_parity/expand_rules_probe.py`` (``guards`` group).
Statuses are never rewritten: those that differ are listed in
docs/error-compat.md.  No database is needed (the auth dependency is faked).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase
from ppbase.api import deps as deps_module
from ppbase.db import engine as engine_module
from ppbase.middleware.error_compat import ERROR_KIND_HEADER

pytestmark = pytest.mark.asyncio

TOKENS: dict[str, dict[str, Any]] = {
    "Bearer users_u1": {
        "type": "authRecord",
        "id": "u1",
        "collectionName": "users",
        "collectionId": "users_id",
    },
    "Bearer members_m1": {
        "type": "authRecord",
        "id": "m1",
        "collectionName": "members",
        "collectionId": "members_id",
    },
    "Bearer admin": {"type": "admin", "id": "admin_1"},
}
CALLERS = {
    "guest": {},
    "user": {"Authorization": "Bearer users_u1"},
    "member": {"Authorization": "Bearer members_m1"},
    "admin": {"Authorization": "Bearer admin"},
}

# (route, caller) -> (status, PPBase message, PocketBase 0.22.34 message)
EXPECTED: dict[tuple[str, str], tuple[int, str, str]] = {
    ("/guest", "user"): (
        403,
        "The request is allowed only for guest clients.",
        "The request can be accessed only by guests.",  # l.37
    ),
    ("/guest", "admin"): (
        403,
        "The request is allowed only for guest clients.",
        "The request can be accessed only by guests.",
    ),
    ("/any", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires admin or record authorization token to be set.",  # l.165
    ),
    ("/users", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires valid record authorization token to be set.",  # l.77
    ),
    ("/users", "member"): (
        403,
        "The authorized auth model is not allowed to perform this action.",
        "The authorized record model is not allowed to perform this action.",  # l.82
    ),
    ("/users", "admin"): (
        403,
        "The authorized auth model is not allowed to perform this action.",
        "The request requires valid record authorization token to be set.",  # l.77
    ),
    ("/users-or-admin", "member"): (
        403,
        "The authorized auth model is not allowed to perform this action.",
        "The authorized record model is not allowed to perform this action.",  # l.169
    ),
    ("/users-or-admin", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires admin or record authorization token to be set.",
    ),
    ("/admin", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires valid admin authorization token to be set.",  # l.119
    ),
    ("/admin", "user"): (
        403,
        "Only superusers can perform this action.",
        "The request requires valid admin authorization token to be set.",
    ),
    ("/owner/u1", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires admin or record authorization token to be set.",  # l.193
    ),
    ("/owner/u1", "member"): (
        403,
        "The authorized auth model is not allowed to perform this action.",
        "You are not allowed to perform this request.",  # l.204
    ),
    # pb.require_auth() / pb.require_record_auth() (FastAPI dependencies of
    # ppbase/api/deps.py): RequireAdminOrRecordAuth() / RequireRecordAuth().
    ("/dep-any", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires admin or record authorization token to be set.",  # l.165
    ),
    ("/dep-record", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires valid record authorization token to be set.",  # l.77
    ),
    ("/dep-record", "admin"): (
        403,
        "The authorized auth model is not allowed to perform this action.",
        "The request requires valid record authorization token to be set.",  # l.77
    ),
    # event.require_auth() / require_auth_record() / require_superuser().
    ("/event-any", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires admin or record authorization token to be set.",
    ),
    ("/event-record", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires valid record authorization token to be set.",
    ),
    ("/event-record", "admin"): (
        403,
        "The authorized auth model is not allowed to perform this action.",
        "The request requires valid record authorization token to be set.",
    ),
    # 401 without auth, like 0.22.34 RequireAdminAuth (l.114-124) and v0.40.6
    # RequireSuperuserAuth; it used to answer 403.
    ("/event-admin", "guest"): (
        401,
        "The request requires authentication.",
        "The request requires valid admin authorization token to be set.",  # l.119
    ),
    ("/event-admin", "user"): (
        403,
        "Only superusers can perform this action.",
        "The request requires valid admin authorization token to be set.",
    ),
}
ALLOWED = {
    ("/guest", "guest"),
    ("/any", "user"),
    ("/any", "admin"),
    ("/users", "user"),
    ("/users-or-admin", "user"),
    ("/users-or-admin", "admin"),
    ("/admin", "admin"),
    ("/owner/u1", "user"),
    ("/owner/u1", "admin"),
    ("/dep-any", "user"),
    ("/dep-any", "admin"),
    ("/dep-record", "user"),
    ("/event-any", "user"),
    ("/event-any", "admin"),
    ("/event-record", "user"),
    ("/event-admin", "admin"),
}


def _guarded_app(monkeypatch: pytest.MonkeyPatch, mode: str) -> Any:
    dependency = deps_module.get_optional_auth

    async def fake_get_optional_auth(request, session=None):
        return TOKENS.get(request.headers.get("authorization", ""))

    async def fake_auth_dependency(request: Request) -> Any:
        return TOKENS.get(request.headers.get("authorization", ""))

    async def fake_get_async_session():
        yield object()

    monkeypatch.setattr(deps_module, "get_optional_auth", fake_get_optional_auth)
    monkeypatch.setattr(engine_module, "get_async_session", fake_get_async_session)

    app_pb = PPBase()
    app_pb.set_error_compat(mode)
    guards = {
        "/ext/guard/guest": app_pb.apis.require_guest_only(),
        "/ext/guard/any": app_pb.apis.require_auth(),
        "/ext/guard/users": app_pb.apis.require_auth("users"),
        "/ext/guard/users-or-admin": app_pb.apis.require_auth("users", "_superusers"),
        "/ext/guard/admin": app_pb.apis.require_superuser_auth(),
        "/ext/guard/owner/{id}": app_pb.apis.require_superuser_or_owner_auth("id"),
    }
    for index, (path, guard) in enumerate(guards.items()):

        async def ok() -> dict[str, bool]:
            return {"ok": True}

        ok.__name__ = f"guard_route_{index}"
        app_pb.get(path, middlewares=[guard])(ok)

    any_auth = app_pb.require_auth()
    record_auth = app_pb.require_record_auth()

    @app_pb.get("/ext/guard/dep-any")
    async def dep_any(_auth: Any = any_auth) -> dict[str, bool]:
        return {"ok": True}

    @app_pb.get("/ext/guard/dep-record")
    async def dep_record(_auth: Any = record_auth) -> dict[str, bool]:
        return {"ok": True}

    for index, (path, helper) in enumerate(
        (
            ("/ext/guard/event-any", "require_auth"),
            ("/ext/guard/event-record", "require_auth_record"),
            ("/ext/guard/event-admin", "require_superuser"),
        )
    ):

        async def event_guard(event: Any, name: str = helper) -> Any:
            getattr(event, name)()
            return await event.next()

        async def event_ok() -> dict[str, bool]:
            return {"ok": True}

        event_ok.__name__ = f"event_guard_route_{index}"
        app_pb.get(path, middlewares=[event_guard])(event_ok)

    app = app_pb.get_app()
    app.dependency_overrides[dependency] = fake_auth_dependency
    return app


async def _responses(app: Any) -> dict[tuple[str, str], Any]:
    results = {}
    routes = {route for route, _caller in [*EXPECTED, *ALLOWED]}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for route in routes:
            for caller, headers in CALLERS.items():
                if (route, caller) not in EXPECTED and (route, caller) not in ALLOWED:
                    continue
                response = await client.get(f"/ext/guard{route}", headers=headers)
                assert ERROR_KIND_HEADER not in response.headers
                results[(route, caller)] = (response.status_code, response.json())
    return results


async def test_guards_keep_ppbase_messages_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = await _responses(_guarded_app(monkeypatch, ""))
    for key in ALLOWED:
        assert results[key] == (200, {"ok": True}), key
    for key, (status, message, _legacy) in EXPECTED.items():
        assert results[key] == (
            status,
            {"status": status, "message": message, "data": {}},
        ), key


async def test_guards_answer_the_pocketbase_022_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = await _responses(_guarded_app(monkeypatch, "0.22"))
    for key in ALLOWED:
        assert results[key] == (200, {"ok": True}), key
    for key, (status, _message, legacy) in EXPECTED.items():
        got_status, body = results[key]
        assert got_status == status, key
        assert list(body) == ["code", "message", "data"], key
        assert body == {"code": status, "message": legacy, "data": {}}, key


async def test_an_unknown_kind_header_is_ignored() -> None:
    from fastapi import HTTPException

    app_pb = PPBase()
    app_pb.set_error_compat("0.22")

    @app_pb.get("/ext/guard/bogus")
    async def bogus():
        raise HTTPException(
            status_code=403,
            detail={
                "status": 403,
                "message": "Only superusers can perform this action.",
                "data": {},
            },
            headers={ERROR_KIND_HEADER: "bogus"},
        )

    async with AsyncClient(
        transport=ASGITransport(app=app_pb.get_app()), base_url="http://testserver"
    ) as client:
        response = await client.get("/ext/guard/bogus")
    assert ERROR_KIND_HEADER not in response.headers
    assert response.status_code == 403
    assert response.json() == {
        "code": 403,
        "message": "Only admins can perform this action.",
        "data": {},
    }
