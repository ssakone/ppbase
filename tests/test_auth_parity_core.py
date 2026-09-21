from types import SimpleNamespace

import jwt
import pytest

from ppbase.models.field_types import FieldValidationError
from ppbase.models.collection import CollectionResponse
from ppbase.db.system_tables import CollectionRecord
from ppbase.services import record_auth_service, record_service
from ppbase.services.auth_service import (
    create_record_auth_token,
    get_password_cost,
    normalize_auth_password_schema,
    validate_password_value,
)


def _collection(schema=None):
    return SimpleNamespace(
        id="users_id",
        name="users",
        schema=schema or [],
        options={
            "authToken": {"secret": "collection-secret", "duration": 3600},
        },
    )


def test_password_constraints_are_read_from_auth_system_field():
    collection = _collection(
        [
            {"name": "displayName", "type": "text", "options": {"min": 99}},
            {"name": "passwordHint", "type": "text", "options": {"max": 2}},
            {
                "name": "password",
                "type": "password",
                "required": True,
                "min": 12,
                "max": 20,
                "cost": 4,
            }
        ]
    )

    assert get_password_cost(collection) == 4
    with pytest.raises(FieldValidationError) as exc:
        validate_password_value(collection, "short")
    assert exc.value.code == "validation_min_text_constraint"
    assert validate_password_value(collection, "long-enough12") == "long-enough12"


def test_password_maximum_is_selected_after_business_fields():
    collection = _collection(
        [
            {"name": "profile", "type": "text", "options": {"min": 99}},
            {
                "name": "password",
                "type": "password",
                "options": {"min": 5, "max": 6},
            },
        ]
    )
    assert validate_password_value(collection, "12345") == "12345"
    with pytest.raises(FieldValidationError) as exc:
        validate_password_value(collection, "1234567")
    assert exc.value.code == "validation_max_text_constraint"


def test_password_defaults_remain_pocketbase_compatible():
    collection = _collection()
    assert validate_password_value(collection, "12345678") == "12345678"
    with pytest.raises(FieldValidationError):
        validate_password_value(collection, "1234567")


def test_password_max_zero_uses_bcrypt_limit_default():
    collection = _collection(
        [{"name": "password", "type": "password", "min": 8, "max": 0}]
    )
    assert validate_password_value(collection, "a" * 71) == "a" * 71


def test_legacy_min_password_length_is_materialized_once():
    original_schema = []
    original_options = {"minPasswordLength": 5, "passwordAuth": {"enabled": True}}
    schema, options = normalize_auth_password_schema(original_schema, original_options)
    assert original_schema == []
    assert original_options["minPasswordLength"] == 5
    password = next(field for field in schema if field["name"] == "password")
    assert password["options"]["min"] == 5
    assert "minPasswordLength" not in options

    rerun_schema, rerun_options = normalize_auth_password_schema(schema, options)
    assert rerun_schema == schema
    assert rerun_options == options

    collection = _collection(schema)
    assert validate_password_value(collection, "12345") == "12345"
    with pytest.raises(FieldValidationError):
        validate_password_value(collection, "1234")


def test_existing_password_field_wins_over_legacy_option():
    schema, options = normalize_auth_password_schema(
        [
            {"name": "name", "type": "text", "options": {"min": 99}},
            {
                "name": "password",
                "type": "password",
                "options": {"min": 12},
            }
        ],
        {"minPasswordLength": 5},
    )
    password = next(field for field in schema if field["name"] == "password")
    assert password["options"]["min"] == 12
    assert "minPasswordLength" not in options


def test_existing_password_field_gets_pocketbase_system_properties():
    schema, _ = normalize_auth_password_schema(
        [{"name": "password", "type": "password", "options": {"min": 5}}],
        {},
    )
    password = schema[0]
    assert password["required"] is True
    assert password["system"] is True
    assert password["hidden"] is True
    assert password["presentable"] is False
    assert password["options"]["min"] == 5


def _auth_collection_with_password_rules(minimum: int = 5, maximum: int = 0):
    return SimpleNamespace(
        id="users_id",
        name="users",
        type="auth",
        schema=[
            {"name": "displayName", "type": "text", "options": {"min": 99}},
            {"name": "role", "type": "text", "options": {"max": 2}},
            {
                "name": "password",
                "type": "password",
                "required": True,
                "system": True,
                "hidden": True,
                "options": {"min": minimum, "max": maximum},
            }
        ],
        options={},
        indexes=[],
    )


def _auth_collection_with_minimum(minimum: int = 5):
    return _auth_collection_with_password_rules(minimum)


def test_create_record_uses_password_field_constraints():
    collection = _auth_collection_with_minimum()

    with pytest.raises(record_service._ValidationErrors) as exc:
        import asyncio

        asyncio.run(
            record_service.create_record(
                object(),
                collection,
                {
                    "email": "short-create@example.com",
                    "password": "1234",
                    "passwordConfirm": "1234",
                },
            )
        )

    assert exc.value.errors["password"]["code"] == "validation_min_text_constraint"


def test_create_record_rejects_password_above_field_maximum():
    collection = _auth_collection_with_password_rules(minimum=1, maximum=6)

    with pytest.raises(record_service._ValidationErrors) as exc:
        import asyncio

        asyncio.run(
            record_service.create_record(
                object(),
                collection,
                {
                    "email": "long-create@example.com",
                    "password": "1234567",
                    "passwordConfirm": "1234567",
                },
            )
        )

    assert exc.value.errors["password"]["code"] == "validation_max_text_constraint"


def test_update_record_uses_password_field_constraints(monkeypatch):
    collection = _auth_collection_with_minimum()

    class _Result:
        def mappings(self):
            return self

        def first(self):
            return {"id": "record_id"}

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, *args, **kwargs):
            return _Result()

    class _Engine:
        def connect(self):
            return _Connection()

    async def _existing(*args, **kwargs):
        return {"id": "record_id"}

    monkeypatch.setattr(record_service, "get_record", _existing)

    with pytest.raises(record_service._ValidationErrors) as exc:
        import asyncio

        asyncio.run(
            record_service.update_record(
                _Engine(),
                collection,
                "record_id",
                {"password": "1234", "passwordConfirm": "1234"},
            )
        )

    assert exc.value.errors["password"]["code"] == "validation_min_text_constraint"


def test_update_record_rejects_password_above_field_maximum(monkeypatch):
    collection = _auth_collection_with_password_rules(minimum=1, maximum=6)

    class _Result:
        def mappings(self):
            return self

        def first(self):
            return {"id": "record_id"}

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, *args, **kwargs):
            return _Result()

    class _Engine:
        def connect(self):
            return _Connection()

    async def _existing(*args, **kwargs):
        return {"id": "record_id"}

    monkeypatch.setattr(record_service, "get_record", _existing)

    with pytest.raises(record_service._ValidationErrors) as exc:
        import asyncio

        asyncio.run(
            record_service.update_record(
                _Engine(),
                collection,
                "record_id",
                {"password": "1234567", "passwordConfirm": "1234567"},
            )
        )

    assert exc.value.errors["password"]["code"] == "validation_max_text_constraint"


def test_password_reset_uses_password_field_constraints():
    collection = _auth_collection_with_minimum()

    import asyncio

    ok, errors = asyncio.run(
        record_auth_service.confirm_password_reset(
            object(),
            collection,
            "unused-token",
            "1234",
            "1234",
            None,
        )
    )

    assert ok is False
    assert errors["password"]["code"] == "validation_min_text_constraint"


def test_auth_collection_response_preserves_normalized_password_minimum():
    record = CollectionRecord(
        id="users_id",
        name="users",
        type="auth",
        system=False,
        schema=[
            {
                "id": "password_field",
                "name": "password",
                "type": "password",
                "required": True,
                "system": True,
                "hidden": True,
                "options": {"min": 5, "max": 0, "cost": 10, "pattern": ""},
            }
        ],
        indexes=[],
        options={},
    )
    response = CollectionResponse.from_record(record)
    password = next(field for field in response.fields if field["name"] == "password")
    assert password["min"] == 5


def test_record_auth_tokens_use_official_auth_claim_type():
    collection = _collection()
    token = create_record_auth_token(
        {"id": "record_id", "token_key": "record-secret"},
        collection,
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["type"] == "auth"
    assert claims["collectionId"] == "users_id"
    assert claims["refreshable"] is True
