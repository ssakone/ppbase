from __future__ import annotations

import pytest

from ppbase.models.field_types import (
    FieldDefinition,
    FieldType,
    FieldValidationError,
    validate_field_value,
)
from ppbase.services.record_service import _apply_append


def test_json_max_size_zero_means_unlimited() -> None:
    field = FieldDefinition(
        name="modules",
        type=FieldType.JSON,
        options={"maxSize": 0},
    )

    value = ["quick_sale", "ventes", "transactions"]

    assert validate_field_value(field, value) == value


def test_json_positive_max_size_is_enforced() -> None:
    field = FieldDefinition(
        name="modules",
        type=FieldType.JSON,
        options={"maxSize": 5},
    )

    with pytest.raises(FieldValidationError) as exc:
        validate_field_value(field, ["quick_sale"])

    assert exc.value.field_name == "modules"
    assert exc.value.code == "validation_max_size_constraint"


def test_json_stringified_object_is_normalized_like_pocketbase() -> None:
    field = FieldDefinition(
        name="permissions",
        type=FieldType.JSON,
    )

    value = '{"all":true,"article":{"read":true}}'

    assert validate_field_value(field, value) == {
        "all": True,
        "article": {"read": True},
    }


def test_json_plain_string_stays_string() -> None:
    field = FieldDefinition(
        name="label",
        type=FieldType.JSON,
    )

    assert validate_field_value(field, "hello") == "hello"


def test_number_empty_string_is_normalized_to_zero_like_pocketbase_formdata() -> None:
    field = FieldDefinition(
        name="solde",
        type=FieldType.NUMBER,
    )

    assert validate_field_value(field, "") == 0.0


def test_editor_max_size_zero_means_unlimited() -> None:
    field = FieldDefinition(
        name="description",
        type=FieldType.EDITOR,
        options={"maxSize": 0},
    )

    assert validate_field_value(field, "long editor content") == "long editor content"


def test_single_file_list_value_normalizes_to_last_filename_like_pocketbase() -> None:
    field = FieldDefinition(
        name="photo",
        type=FieldType.FILE,
        options={"maxSelect": 1},
    )

    assert validate_field_value(field, ["old.jpg", "new.jpg"]) == "new.jpg"


def test_single_file_append_modifier_normalizes_to_plain_filename() -> None:
    field = FieldDefinition(
        name="photo",
        type=FieldType.FILE,
        options={"maxSelect": 1},
    )

    appended = _apply_append("old.jpg", ["new.jpg"], field)

    assert appended == ["old.jpg", "new.jpg"]
    assert validate_field_value(field, appended) == "new.jpg"


@pytest.mark.parametrize(
    "value",
    ["../secret.txt", "..\\secret.txt", "/absolute.txt", "C:\\secret.txt"],
)
def test_single_file_field_rejects_path_references(value: str) -> None:
    field = FieldDefinition(
        name="photo",
        type=FieldType.FILE,
        options={"maxSelect": 1},
    )

    with pytest.raises(FieldValidationError) as exc:
        validate_field_value(field, value)

    assert exc.value.field_name == "photo"
    assert exc.value.code == "validation_invalid_filename"


def test_multi_file_field_rejects_one_unsafe_reference() -> None:
    field = FieldDefinition(
        name="documents",
        type=FieldType.FILE,
        options={"maxSelect": 5},
    )

    with pytest.raises(FieldValidationError) as exc:
        validate_field_value(field, ["safe.txt", "../secret.txt"])

    assert exc.value.field_name == "documents"
    assert exc.value.code == "validation_invalid_filename"


@pytest.mark.parametrize(
    "value",
    [
        "photo_52iWbGinWd.jpg",
        "LICENSE",
        "résumé_final.txt",
        "https://example.com/avatar.png",
    ],
)
def test_file_field_accepts_compatible_references(value: str) -> None:
    field = FieldDefinition(
        name="document",
        type=FieldType.FILE,
        options={"maxSelect": 1},
    )

    assert validate_field_value(field, value) == value
