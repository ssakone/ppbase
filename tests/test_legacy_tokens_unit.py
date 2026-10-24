"""Unit tests: PocketBase 0.22 purpose tokens and forged collection ids."""

from __future__ import annotations

from types import SimpleNamespace

import jwt
import pytest
from sqlalchemy.sql.elements import TextClause

from ppbase.api.deps import get_optional_auth
from ppbase.services.auth_service import verify_purpose_token
from ppbase.services.file_tokens import create_file_token_for_auth, verify_file_token


@pytest.mark.parametrize(
    ("token_type", "legacy", "accepted"),
    [
        ("verification", False, True),
        ("authRecord", False, False),
        ("verification", True, True),
        ("authRecord", True, True),
        ("auth", True, False),
        ("admin", True, False),
    ],
)
def test_verify_purpose_token_accepts_the_022_type_only_on_request(
    token_type: str, legacy: bool, accepted: bool
) -> None:
    token = jwt.encode(
        {"id": "r", "type": token_type, "exp": 4102444800},
        "record-token-key-and-verification-secret",
        algorithm="HS256",
    )
    payload = verify_purpose_token(
        token,
        "record-token-key-and-verification-secret",
        "verification",
        accept_legacy_record_type=legacy,
    )
    assert (payload is not None) is accepted
    assert (
        verify_purpose_token(
            token,
            "another-record-token-key-and-secret-value",
            "verification",
            accept_legacy_record_type=True,
        )
        is None
    )


class _NoTokenKeyQuerySession:
    """Fails the test if the token_key of a non-auth collection is queried."""

    def __init__(self, collection):
        self.collection = collection

    async def get(self, _model, _key):
        return self.collection

    async def execute(self, statement, *_args, **_kwargs):
        if isinstance(statement, TextClause):
            raise AssertionError("token_key must not be queried for a non-auth collection")
        collection = self.collection

        class _Result:
            def scalars(self):
                return self

            def first(self):
                return collection

        return _Result()


BASE_COLLECTION = SimpleNamespace(id="posts0000000001", name="posts", type="base", options={})
VIEW_COLLECTION = SimpleNamespace(id="view00000000001", name="report", type="view", options={})


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", [BASE_COLLECTION, VIEW_COLLECTION])
async def test_forged_token_for_a_non_auth_collection_is_anonymous(collection) -> None:
    token = jwt.encode(
        {"id": "user00000000001", "type": "authRecord", "collectionId": collection.id},
        "forged-key-which-is-long-enough-for-hs256",
        algorithm="HS256",
    )
    request = SimpleNamespace(
        headers={"Authorization": token},
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(get_jwt_secret=lambda: "unused"))
        ),
    )
    assert await get_optional_auth(request, _NoTokenKeyQuerySession(collection)) is None

    file_token = jwt.encode(
        {"id": "user00000000001", "type": "auth", "collectionId": collection.id, "for": "file"},
        "forged-key-which-is-long-enough-for-hs256",
        algorithm="HS256",
    )
    session = _NoTokenKeyQuerySession(collection)
    assert await verify_file_token(session, file_token) is None
    with pytest.raises(ValueError):
        await create_file_token_for_auth(
            session,
            {"id": "user00000000001", "type": "auth", "collectionId": collection.id},
        )
