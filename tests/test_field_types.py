from __future__ import annotations

import pytest

from ppbase.models.field_types import (
    FieldDefinition,
    FieldType,
    FieldValidationError,
    validate_field_value,
)


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


def test_editor_max_size_zero_means_unlimited() -> None:
    field = FieldDefinition(
        name="description",
        type=FieldType.EDITOR,
        options={"maxSize": 0},
    )

    assert validate_field_value(field, "long editor content") == "long editor content"
