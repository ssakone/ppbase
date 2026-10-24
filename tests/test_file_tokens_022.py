"""File tokens issued by PocketBase 0.22.34 (no ``for`` claim) stay valid.

v0.22.34 tokens/admin.go:28-35 and tokens/record.go:81-95: ``exp``, ``id``,
``type`` (``admin`` or ``authRecord``) and, for records, ``collectionId``;
signed with the token key plus the file secret (apis/file.go:221-251).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest

from ppbase.db.system_tables import CollectionRecord, SuperuserRecord
from ppbase.services.file_tokens import verify_file_token

ADMIN = SimpleNamespace(id="admin0000000001", email="admin@example.test", token_key="A" * 50)
SUPERUSERS = SimpleNamespace(
    id="pbc_3142635823",
    name="_superusers",
    type="auth",
    options={
        "authToken": {"secret": "superusers-session-secret-0123456789", "duration": 1209600},
        "fileToken": {"secret": "superusers-file-secret-0123456789ab", "duration": 120},
    },
)
USERS = SimpleNamespace(
    id="_pb_users_auth_",
    name="users",
    type="auth",
    options={
        "authToken": {"secret": "users-session-secret-0123456789abcd", "duration": 1209600},
        "fileToken": {"secret": "users-file-secret-0123456789abcdefg", "duration": 180},
    },
)
RECORD_ID, RECORD_KEY = "user00000000001", "R" * 50


class _Result:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def mappings(self):
        return self

    def first(self):
        return self._value


class _Session:
    async def get(self, model, key):
        if model is SuperuserRecord and key == ADMIN.id:
            return ADMIN
        if model is CollectionRecord and key == USERS.id:
            return USERS
        return None

    async def execute(self, statement, *_args, **_kwargs):
        if "token_key" in str(statement):
            return _Result({"token_key": RECORD_KEY})
        return _Result(SUPERUSERS)


def _token(claims: dict, key: str) -> str:
    return jwt.encode({"exp": int(time.time()) + 120, **claims}, key, algorithm="HS256")


@pytest.mark.asyncio
async def test_pocketbase_022_admin_file_token_is_accepted() -> None:
    token = _token(
        {"id": ADMIN.id, "type": "admin"},
        ADMIN.token_key + SUPERUSERS.options["fileToken"]["secret"],
    )
    assert await verify_file_token(_Session(), token) == {
        "id": ADMIN.id,
        "email": ADMIN.email,
        "type": "admin",
    }


@pytest.mark.asyncio
async def test_pocketbase_022_record_file_token_is_accepted() -> None:
    token = _token(
        {"id": RECORD_ID, "type": "authRecord", "collectionId": USERS.id},
        RECORD_KEY + USERS.options["fileToken"]["secret"],
    )
    assert await verify_file_token(_Session(), token) == {
        "id": RECORD_ID,
        "type": "auth",
        "collectionId": USERS.id,
        "collectionName": "users",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "key"),
    [
        # 0.22 session tokens: same claims, signed with the session secret.
        (
            {"id": ADMIN.id, "type": "admin"},
            ADMIN.token_key + SUPERUSERS.options["authToken"]["secret"],
        ),
        (
            {"id": RECORD_ID, "type": "authRecord", "collectionId": USERS.id},
            RECORD_KEY + USERS.options["authToken"]["secret"],
        ),
        # PPBase session tokens never act as file tokens, whatever the key.
        (
            {"id": RECORD_ID, "type": "auth", "collectionId": USERS.id},
            RECORD_KEY + USERS.options["fileToken"]["secret"],
        ),
        # Another purpose stays refused.
        (
            {"id": ADMIN.id, "type": "admin", "for": "other"},
            ADMIN.token_key + SUPERUSERS.options["fileToken"]["secret"],
        ),
    ],
)
async def test_session_tokens_are_not_file_tokens(claims: dict, key: str) -> None:
    assert await verify_file_token(_Session(), _token(claims, key)) is None
