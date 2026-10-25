"""Unit tests: a NULL value is served as the PocketBase zero value of its type.

PocketBase reads a SQL NULL as ``nil`` and serves every schema field through
``PrepareValue`` (v0.22.34 ``models/record.go`` l.48-58, l.328-359 and
l.528-531, ``models/schema/schema_field.go`` l.304-390; v0.40.6
``core/record_model.go`` l.484-518 and ``core/field_*.go``), so a NULL is
"" (text, email, url, editor, date, single select/file/relation), 0
(number), false (bool), [] (multiple select/file/relation) and null (json).
PPBase views produce NULL from a LEFT JOIN without match.  The HTTP and
PostgreSQL side is in ``tests/integration/test_null_zero_values.py`` and the
paired PocketBase 0.22.34 probe in
``tests/pocketbase_parity/null_zero_values_probe.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from ppbase.models.record import build_record_response, null_field_value


def _field(name: str, field_type: str, **options: Any) -> dict[str, Any]:
    return {"name": name, "type": field_type, "required": False, "options": options}


# One field of each type, nested (0.22) format.  ``rel_null`` is the 0.22
# ``maxSelect: null`` relation (D-0019: multiple, unlimited).
SCHEMA = [
    _field("t", "text"),
    _field("e", "email"),
    _field("u", "url"),
    _field("ed", "editor"),
    _field("n", "number"),
    _field("ni", "number", onlyInt=True),
    _field("b", "bool"),
    _field("j", "json"),
    _field("d", "date"),
    _field("ad", "autodate"),
    _field("s1", "select", maxSelect=1, values=["x", "y"]),
    _field("s2", "select", maxSelect=2, values=["x", "y"]),
    _field("f1", "file", maxSelect=1),
    _field("f2", "file", maxSelect=3),
    _field("r1", "relation", collectionId="c1", maxSelect=1),
    _field("r2", "relation", collectionId="c1", maxSelect=5),
    _field("rn", "relation", collectionId="c1", maxSelect=None),
    _field("g", "geoPoint"),
    _field("x", "someFutureType"),
]

ZERO = {
    "t": "",
    "e": "",
    "u": "",
    "ed": "",
    "n": 0,
    "ni": 0,
    "b": False,
    "j": None,
    "d": "",
    "ad": "",
    "s1": "",
    "s2": [],
    "f1": "",
    "f2": [],
    "r1": "",
    "r2": [],
    "rn": [],
    # Not in PocketBase 0.22.34: left null (see null_field_value).
    "g": None,
    "x": None,
}


@pytest.mark.parametrize("field", SCHEMA, ids=[f["name"] for f in SCHEMA])
def test_null_field_value_is_the_pocketbase_zero_value(field: dict[str, Any]) -> None:
    assert null_field_value(field) == ZERO[field["name"]]
    assert type(null_field_value(field)) is type(ZERO[field["name"]])


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        # Flat (v0.23+) definitions use the same multiplicity rule.
        ({"name": "r", "type": "relation", "maxSelect": 1}, ""),
        ({"name": "r", "type": "relation", "maxSelect": 2}, []),
        ({"name": "s", "type": "select", "maxSelect": 3}, []),
        ({"name": "f", "type": "file", "maxSelect": 1}, ""),
        # select/file maxSelect null decodes as 0 in 0.22 (single).
        ({"name": "s", "type": "select", "options": {"maxSelect": None}}, ""),
        ({"name": "f", "type": "file", "options": {"maxSelect": None}}, ""),
        # No maxSelect key at all: single (field_multiplicity rule).
        ({"name": "r", "type": "relation", "options": {}}, ""),
        ({"name": "n", "type": "number"}, 0),
    ],
)
def test_null_field_value_flat_and_edge_definitions(
    field: dict[str, Any], expected: Any
) -> None:
    assert null_field_value(field) == expected


def _all_null_row() -> dict[str, Any]:
    return {"id": "v1", **{f["name"]: None for f in SCHEMA}}


def test_null_values_are_served_as_zero_values() -> None:
    record = build_record_response(
        _all_null_row(), "cid", "view", SCHEMA, is_view_collection=True
    )
    assert {name: record[name] for name in ZERO} == ZERO
    # Absent columns (field not selected by the view) behave like NULL.
    record = build_record_response({"id": "v1"}, "cid", "view", SCHEMA)
    assert {name: record[name] for name in ZERO} == ZERO
    # The JSON body carries "", 0, false, [] and null exactly.
    encoded = json.loads(json.dumps(record))
    assert encoded["n"] == 0 and encoded["b"] is False and encoded["j"] is None
    assert encoded["s2"] == [] and encoded["rn"] == []


def test_non_null_values_are_unchanged() -> None:
    stamp = datetime(2024, 2, 3, 4, 5, 6, 789000, tzinfo=UTC)
    row = {
        "id": "v1",
        "t": "",
        "e": "a@example.invalid",
        "u": "https://example.invalid",
        "ed": "<p>x</p>",
        "n": 0.0,
        "ni": 7,
        "b": False,
        "j": {"k": [1, None]},
        "d": stamp,
        "ad": stamp,
        "s1": "x",
        "s2": [],
        "f1": "",
        "f2": ["a.txt", "b.txt"],
        "r1": "",
        "r2": ["r1", "r2"],
        "rn": [],
        "g": {"lon": 1, "lat": 2},
        "x": "raw",
    }
    record = build_record_response(row, "cid", "base", SCHEMA)
    expected = dict(row)
    expected["n"] = 0  # whole floats were already served as integers
    expected["d"] = expected["ad"] = "2024-02-03 04:05:06.789Z"
    assert {name: record[name] for name in ZERO} == {
        name: expected[name] for name in ZERO
    }
    # A stored JSON null (jsonb 'null' read as None) and a JSON false/0 stay.
    for value in (None, False, 0, "", []):
        served = build_record_response({"id": "v1", "j": value}, "cid", "b", SCHEMA)
        assert served["j"] == value and type(served["j"]) is type(value)


def test_null_id_and_auth_columns_are_zero_values() -> None:
    record = build_record_response({"id": None}, "cid", "view", [])
    assert record["id"] == ""
    auth = build_record_response(
        {"id": "u1", "email": None, "email_visibility": None, "verified": None},
        "cid",
        "users",
        [],
        is_auth_collection=True,
    )
    assert auth["email"] == "" and auth["emailVisibility"] is False
    assert auth["verified"] is False


def test_hidden_fields_and_fields_filter_still_apply() -> None:
    row = _all_null_row()
    record = build_record_response(
        row, "cid", "view", SCHEMA, hidden_fields={"t"}, is_view_collection=True
    )
    assert "t" not in record and record["n"] == 0
    record = build_record_response(
        row, "cid", "view", SCHEMA, fields_filter=["id", "n", "rn", "j"]
    )
    assert record == {"id": "v1", "n": 0, "rn": [], "j": None}
    # A hidden field explicitly requested is served with its zero value.
    record = build_record_response(
        row, "cid", "view", SCHEMA, fields_filter=["t"], hidden_fields={"t"}
    )
    assert record == {"t": ""}


def test_view_without_schema_keeps_raw_columns() -> None:
    # No field types are known: the raw row is served unchanged.
    record = build_record_response(
        {"id": "v1", "extra": None}, "cid", "view", [], is_view_collection=True
    )
    assert record["extra"] is None
