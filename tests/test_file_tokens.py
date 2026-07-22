"""Unit tests for the shared PocketBase-compatible file-token service."""

from __future__ import annotations

from types import SimpleNamespace

import jwt
import pytest

from ppbase.db.system_tables import CollectionRecord, SuperuserRecord
from ppbase.services.file_tokens import (
    create_file_token_for_auth,
    verify_file_token,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


class _MappingResult:
    def __init__(self, value):
        self._value = value

    def mappings(self):
        return self

    def first(self):
        return self._value


@pytest.mark.asyncio
async def test_admin_file_token_round_trip_uses_superuser_file_secret() -> None:
    admin = SimpleNamespace(
        id="admin000000001",
        email="admin@example.test",
        token_key="admin-token-key",
    )
    superusers = SimpleNamespace(
        id="_superusers",
        name="_superusers",
        options={
            "fileToken": {
                "secret": "superuser-file-secret",
                "duration": 180,
            }
        },
    )

    class _Session:
        async def get(self, model, record_id):
            if model is SuperuserRecord and record_id == admin.id:
                return admin
            return None

        async def execute(self, _statement, *_args, **_kwargs):
            return _ScalarResult(superusers)

    session = _Session()
    token = await create_file_token_for_auth(
        session,
        {"id": admin.id, "type": "admin"},
    )
    claims = jwt.decode(
        token,
        admin.token_key + "superuser-file-secret",
        algorithms=["HS256"],
    )

    verified = await verify_file_token(session, token)

    assert claims["for"] == "file"
    assert claims["id"] == admin.id
    assert claims["type"] == "admin"
    assert verified == {
        "id": admin.id,
        "email": admin.email,
        "type": "admin",
    }


@pytest.mark.asyncio
async def test_auth_record_file_token_round_trip_uses_collection_file_secret() -> None:
    collection = SimpleNamespace(
        id="users000000001",
        name="users",
        options={
            "fileToken": {
                "secret": "users-file-secret",
                "duration": 180,
            }
        },
    )
    record_id = "record000000001"
    record_token_key = "record-token-key"

    class _Session:
        async def get(self, model, selected_id):
            if model is CollectionRecord and selected_id == collection.id:
                return collection
            return None

        async def execute(self, _statement, *_args, **_kwargs):
            return _MappingResult({"token_key": record_token_key})

    session = _Session()
    token = await create_file_token_for_auth(
        session,
        {
            "id": record_id,
            "type": "authRecord",
            "collectionId": collection.id,
        },
    )
    claims = jwt.decode(
        token,
        record_token_key + "users-file-secret",
        algorithms=["HS256"],
    )

    verified = await verify_file_token(session, token)

    assert claims["for"] == "file"
    assert claims["id"] == record_id
    assert claims["type"] == "authRecord"
    assert claims["collectionId"] == collection.id
    assert verified == {
        "id": record_id,
        "type": "authRecord",
        "collectionId": collection.id,
        "collectionName": collection.name,
    }
