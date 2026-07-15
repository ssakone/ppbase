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


@pytest.mark.asyncio
async def test_authrecord_lookup_reuses_the_dependency_session(monkeypatch):
    import ppbase.db.engine as engine_mod

    def _unexpected_engine_lookup():
        raise AssertionError("auth dependency must not open a second connection")

    monkeypatch.setattr(engine_mod, "get_engine", _unexpected_engine_lookup)
    collection = SimpleNamespace(
        id="users_collection",
        name="users",
        options={
            "authToken": {
                "secret": "collection-secret-long-enough",
                "duration": 60,
            }
        },
    )

    class _CollectionResult:
        def scalars(self):
            return self

        def first(self):
            return collection

    class _RecordResult:
        def mappings(self):
            return self

        def first(self):
            return {"token_key": "record-key-long-enough"}

    class _Session:
        def __init__(self):
            self.calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            return _CollectionResult() if self.calls == 1 else _RecordResult()

    token = pyjwt.encode(
        {
            "id": "record-id",
            "type": "authRecord",
            "collectionId": collection.id,
        },
        "record-key-long-enoughcollection-secret-long-enough",
        algorithm="HS256",
    )
    request = SimpleNamespace(
        headers={"Authorization": token},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(get_jwt_secret=lambda: "unused")
            )
        ),
    )
    session = _Session()

    auth = await get_optional_auth(request, session)

    assert auth is not None
    assert auth["id"] == "record-id"
    assert session.calls == 2
