"""Unit tests for auth dependency behavior."""

from __future__ import annotations

from types import SimpleNamespace

import jwt as pyjwt
import pytest

from ppbase.api.deps import get_optional_auth


class _FakeResult:
    def scalars(self) -> "_FakeResult":
        return self

    def first(self):
        return None


class _FakeSession:
    async def execute(self, _stmt):
        return _FakeResult()

    async def get(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_get_optional_auth_rejects_unknown_authrecord_collection_id(monkeypatch):
    """Auth record tokens with unknown collectionId must be treated as unauthenticated."""
    import ppbase.db.engine as engine_mod

    monkeypatch.setattr(engine_mod, "get_engine", lambda: object())

    token = pyjwt.encode(
        {
            "id": "attacker-id",
            "type": "authRecord",
            "collectionId": "missing_collection",
        },
        "attacker-secret-which-is-long-enough-for-hs256",
        algorithm="HS256",
    )

    request = SimpleNamespace(
        headers={"Authorization": token},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(get_jwt_secret=lambda: "test-secret")
            )
        ),
    )

    auth = await get_optional_auth(request, _FakeSession())
    assert auth is None
