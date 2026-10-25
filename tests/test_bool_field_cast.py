"""Bool field values are converted like PocketBase's ``cast.ToBool``.

PocketBase 0.22.34 prepares a bool field value with ``cast.ToBool``
(``models/schema/schema_field.go`` l.346-347, called by ``LoadData`` in
``forms/record_upsert.go`` l.415-417 after a JSON round trip, l.406-413), as
v0.40.6 does (``core/field_bool.go`` l.103-106).  ``cast.ToBool`` (spf13/cast
v1.7.1 ``caste.go`` l.103-148 and ``cast.go`` l.12-15; v1.10.0 ``basic.go``
l.16-67) keeps a bool, gives false for nil, compares a number to 0, parses a
string with Go's ``strconv.ParseBool`` (go1.23.7 ``strconv/atob.go`` l.10-18:
exact spellings, no trimming) and gives false for an error or any other type.
Python's ``bool()`` made the multipart/form-data string ``"false"`` true.
"""

from __future__ import annotations

import pytest

from ppbase.models.field_types import (
    FieldDefinition,
    FieldType,
    FieldValidationError,
    validate_field_value,
)
from ppbase.services.record_file_fields import MultipartText

GO_TRUE = ("1", "t", "T", "TRUE", "true", "True")
GO_FALSE = ("0", "f", "F", "FALSE", "false", "False")
# strconv.ParseBool errors: cast.ToBool returns false.
GO_ERRORS = (
    "",
    " ",
    " true",
    "true ",
    "\ttrue",
    "tRUE",
    "yes",
    "on",
    "no",
    "off",
    "null",
    "2",
    "-1",
    "0.5",
    "1.0",
    "01",
    "-0",
)


def _bool_field(*, required: bool = False) -> FieldDefinition:
    return FieldDefinition(name="flag", type=FieldType.BOOL, required=required)


@pytest.mark.parametrize("raw", GO_TRUE)
def test_parse_bool_true_spellings_are_true(raw: str) -> None:
    assert validate_field_value(_bool_field(), raw) is True


@pytest.mark.parametrize("raw", GO_FALSE)
def test_parse_bool_false_spellings_are_false(raw: str) -> None:
    assert validate_field_value(_bool_field(), raw) is False


@pytest.mark.parametrize("raw", GO_ERRORS)
def test_unparsable_strings_are_false(raw: str) -> None:
    assert validate_field_value(_bool_field(), raw) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        (None, False),
        (0, False),
        (1, True),
        (-1, True),
        (2, True),
        (0.0, False),
        (-0.0, False),
        (0.5, True),
        (1e-300, True),
        (10**30, True),
    ],
)
def test_native_json_values_keep_their_go_meaning(raw: object, expected: bool) -> None:
    assert validate_field_value(_bool_field(), raw) is expected


@pytest.mark.parametrize("raw", [[], [True], ["true"], {}, {"a": True}])
def test_lists_and_objects_are_false(raw: object) -> None:
    # cast.ToBool default branch: unsupported type, false.  A JSON array or
    # object (and a repeated multipart key, see D-XXXX) is never true.
    assert validate_field_value(_bool_field(), raw) is False


@pytest.mark.parametrize(("raw", "expected"), [("false", False), ("true", True)])
def test_multipart_text_of_the_022_mode_is_parsed_too(raw: str, expected: bool) -> None:
    assert validate_field_value(_bool_field(), MultipartText(raw)) is expected


@pytest.mark.parametrize("raw", ["false", "0", "", "yes", [True], None, False, 0])
def test_required_rejects_every_false_value(raw: object) -> None:
    with pytest.raises(FieldValidationError) as exc:
        validate_field_value(_bool_field(required=True), raw)
    assert exc.value.field_name == "flag"
    assert exc.value.code == "validation_required"
    assert exc.value.message == "Must be true."


@pytest.mark.parametrize("raw", ["true", "1", "T", True, 1, 0.5])
def test_required_accepts_every_true_value(raw: object) -> None:
    assert validate_field_value(_bool_field(required=True), raw) is True
