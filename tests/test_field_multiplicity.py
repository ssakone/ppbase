"""Unit tests for the shared select/file/relation multiplicity rule (D-0019).

Reference semantics: PocketBase v0.22.34 ``models/schema/schema_field.go``
(relation ``IsMultiple`` l.715-716, ``PrepareValue`` l.375-386) and v0.40.6
``core/field_relation.go`` (``IsMultiple`` l.151-153).
"""

from __future__ import annotations

import pytest

from ppbase.backup.canonical import (
    _field_column_spec,
    _legacy_relation_column_spec,
    build_collection_table_spec,
)
from ppbase.db.schema_manager import (
    _col_ddl,
    _field_indexes,
    _is_array_field,
    _multiplicity_changed,
    _type_options_changed,
)
from ppbase.models.collection import CollectionCreate, _schema_to_fields
from ppbase.models.field_multiplicity import (
    field_is_multiple,
    field_options,
    is_multiple,
    prepare_relation_value,
    relation_is_multiple,
    relation_max_select,
    select_or_file_is_multiple,
    select_or_file_max_select,
    to_unique_string_list,
)
from ppbase.models.field_types import (
    FieldDefinition,
    FieldType,
    FieldValidationError,
    validate_field_value,
)
from ppbase.services.expand_service import _get_relation_field
from ppbase.services.filter_parser import parse_filter
from ppbase.services.record_service import (
    _apply_append,
    _apply_prepend,
    _apply_remove,
    _build_relation_index_from_collections,
    _relation_targets,
)

_ABSENT = object()


def _relation(max_select=_ABSENT, **extra) -> FieldDefinition:
    options = {"collectionId": "target_coll_id1", **extra}
    if max_select is not _ABSENT:
        options["maxSelect"] = max_select
    return FieldDefinition(name="rels", type=FieldType.RELATION, options=options)


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "multiple", "limit"),
    [
        ({"maxSelect": None}, True, None),  # PocketBase 0.22 nil pointer
        ({}, False, 1),  # absent key: single, PPBase/current PocketBase
        ({"maxSelect": 0}, False, 1),  # 0.22.34 IsMultiple(*0 > 1) is false
        ({"maxSelect": 1}, False, 1),
        ({"maxSelect": 2}, True, 2),
        ({"maxSelect": 999}, True, 999),
        ({"maxSelect": 2147483647}, True, 2147483647),  # v0.23 upgrade value
        ({"maxSelect": 3.0}, True, 3),
        ({"maxSelect": "4"}, True, 4),
        ({"maxSelect": -5}, False, 1),
        ({"maxSelect": "many"}, False, 1),
        (None, False, 1),
    ],
)
def test_relation_multiplicity_rule(options, multiple, limit) -> None:
    assert relation_is_multiple(options) is multiple
    assert relation_max_select(options) == limit
    assert is_multiple("relation", options) is multiple
    assert is_multiple(FieldType.RELATION, options) is multiple


@pytest.mark.parametrize(
    ("options", "multiple", "limit"),
    [
        ({"maxSelect": None}, False, 1),  # int in every PocketBase release
        ({}, False, 1),
        ({"maxSelect": 0}, False, 1),
        ({"maxSelect": 1}, False, 1),
        ({"maxSelect": 2}, True, 2),
        ({"maxSelect": 99}, True, 99),
    ],
)
def test_select_and_file_multiplicity_rule(options, multiple, limit) -> None:
    assert select_or_file_is_multiple(options) is multiple
    assert select_or_file_max_select(options) == limit
    assert is_multiple("select", options) is multiple
    assert is_multiple("file", options) is multiple


def test_non_multi_value_types_are_never_multiple() -> None:
    assert is_multiple("text", {"maxSelect": None}) is False
    assert is_multiple("json", {"maxSelect": 5}) is False


def test_field_options_merges_flat_and_nested_like_the_collection_model() -> None:
    nested = {"name": "rels", "type": "relation", "options": {"maxSelect": None}}
    flat = {"name": "rels", "type": "relation", "maxSelect": None, "collectionId": "x"}
    both = {
        "name": "rels",
        "type": "relation",
        "maxSelect": 1,
        "options": {"maxSelect": None},
    }
    assert field_options(nested) == {"maxSelect": None}
    assert field_options(flat) == {"maxSelect": None, "collectionId": "x"}
    assert field_options(both) == {"maxSelect": None}  # nested options win
    assert field_is_multiple(nested) is True
    assert field_is_multiple(flat) is True
    assert field_is_multiple({"name": "rels", "type": "relation"}) is False
    assert field_is_multiple(_relation(None)) is True
    assert field_is_multiple(_relation()) is False
    assert field_is_multiple({"name": "t", "type": "text", "maxSelect": None}) is False


def test_api_metadata_keeps_explicit_null_max_select() -> None:
    """The stored schema is not rewritten; GET returns maxSelect null."""
    body = CollectionCreate(
        name="owners",
        schema=[
            {
                "name": "rels",
                "type": "relation",
                "options": {"collectionId": "abc", "maxSelect": None},
            }
        ],
    )
    assert body.schema[0]["options"] == {"collectionId": "abc", "maxSelect": None}
    flat = _schema_to_fields(body.schema)
    assert "maxSelect" in flat[0] and flat[0]["maxSelect"] is None
    # The flat representation round-trips to the same multiplicity.
    again = CollectionCreate(name="owners", schema=flat)
    assert again.schema[0]["options"]["maxSelect"] is None
    assert field_is_multiple(again.schema[0]) is True


# ---------------------------------------------------------------------------
# PrepareValue / ToUniqueStringSlice port
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("abc", ["abc"]),
        ("a,b", ["a,b"]),  # never split on commas
        ('["a","b","a"]', ["a", "b"]),
        ('["a", null, ""]', ["a"]),
        ("[not json", ["[not json"]),
        ("[1,2]", ["[1,2]"]),
        (["b", "a", "b", "", None], ["b", "a"]),
        ([1, 2.5, 3.0, True, {"x": 1}], ["1", "2.5", "3", "true"]),
        (7, ["7"]),
        ({"x": 1}, []),
    ],
)
def test_to_unique_string_list_matches_pocketbase(raw, expected) -> None:
    assert to_unique_string_list(raw) == expected


def test_prepare_relation_value_single_keeps_last_and_multiple_wraps() -> None:
    assert prepare_relation_value(["a", "b"], multiple=False) == "b"
    assert prepare_relation_value([], multiple=False) == ""
    assert prepare_relation_value("a", multiple=True) == ["a"]
    assert prepare_relation_value(None, multiple=True) == []


def test_validate_relation_null_max_select_is_unlimited_list() -> None:
    field = _relation(None)
    assert validate_field_value(field, "abc") == ["abc"]
    assert validate_field_value(field, ["b", "a", "b"]) == ["b", "a"]
    assert validate_field_value(field, None) == []
    many = [f"id{i:013d}" for i in range(250)]
    assert validate_field_value(field, many) == many


def test_validate_relation_single_keeps_last_id() -> None:
    for options in (1, 0, _ABSENT):
        field = _relation(options)
        assert validate_field_value(field, ["a", "b"]) == "b"
        assert validate_field_value(field, "a") == "a"
        assert validate_field_value(field, None) == ""


def test_validate_relation_numeric_limit_and_required() -> None:
    with pytest.raises(FieldValidationError) as exc:
        validate_field_value(_relation(2), ["a", "b", "c"])
    assert exc.value.code == "validation_max_select_constraint"
    assert validate_field_value(_relation(2), ["a", "b", "a"]) == ["a", "b"]

    required = FieldDefinition(
        name="rels",
        type=FieldType.RELATION,
        required=True,
        options={"collectionId": "x", "maxSelect": None},
    )
    with pytest.raises(FieldValidationError) as exc:
        validate_field_value(required, [])
    assert exc.value.code == "validation_required"


def test_validate_relation_keeps_long_orphan_element() -> None:
    joined = "a" * 15 + "," + "b" * 15  # 31 characters, as in the real backup
    assert validate_field_value(_relation(None), [joined]) == [joined]


def test_relation_modifiers_follow_pocketbase_list_semantics() -> None:
    field = _relation(None)
    assert _apply_append(["a"], ["b", "a"], field) == ["a", "b", "a"]
    assert validate_field_value(field, _apply_append(["a"], ["b", "a"], field)) == [
        "a",
        "b",
    ]
    assert _apply_append("a", '["b","c"]', field) == ["a", "b", "c"]
    assert _apply_prepend(["a"], "c", field) == ["c", "a"]
    assert _apply_remove(["a", "b"], '["a"]', field) == ["b"]
    assert _apply_remove("a", "a", field) == []


# ---------------------------------------------------------------------------
# Physical schema
# ---------------------------------------------------------------------------


def test_relation_columns_are_unbounded_text_like_pocketbase() -> None:
    assert _col_ddl(_relation(None)) == "\"rels\" TEXT[] NOT NULL DEFAULT '{}'"
    assert _col_ddl(_relation(5)) == "\"rels\" TEXT[] NOT NULL DEFAULT '{}'"
    for options in (1, 0, _ABSENT):
        assert _col_ddl(_relation(options)) == "\"rels\" TEXT NOT NULL DEFAULT ''"


def test_select_and_file_columns_are_unchanged() -> None:
    select_null = FieldDefinition(
        name="s", type=FieldType.SELECT, options={"maxSelect": None, "values": ["a"]}
    )
    file_many = FieldDefinition(name="f", type=FieldType.FILE, options={"maxSelect": 3})
    assert _col_ddl(select_null) == "\"s\" TEXT NOT NULL DEFAULT ''"
    assert _col_ddl(file_many) == "\"f\" TEXT[] NOT NULL DEFAULT '{}'"


def test_array_detection_and_managed_indexes_follow_the_rule() -> None:
    assert _is_array_field(_relation(None)) is True
    assert _is_array_field(_relation(1)) is False
    indexes = _field_indexes("owners", [_relation(None)])
    assert "USING GIN" in next(iter(indexes.values()))
    single = _field_indexes("owners", [_relation(1)])
    assert "WHERE \"rels\" != ''" in next(iter(single.values()))


@pytest.mark.parametrize(
    ("old", "new", "changed"),
    [
        (None, None, False),  # update without change keeps the column
        (None, 5, False),  # both multiple
        (None, 1, True),
        (1, None, True),
        (1, _ABSENT, False),
        (0, 1, False),
        (2, 3, False),
    ],
)
def test_multiplicity_change_detection(old, new, changed) -> None:
    old_field, new_field = _relation(old), _relation(new)
    assert _multiplicity_changed(old_field, new_field) is changed
    assert _type_options_changed(old_field, new_field) is changed


def test_canonical_backup_specs_match_the_new_ddl_and_accept_legacy_shapes() -> None:
    multiple = _field_column_spec(_relation(None))
    single = _field_column_spec(_relation(1))
    assert (multiple.type, multiple.default) == ("text[]", "'{}'::text[]")
    assert (single.type, single.default) == ("text", "''::text")

    legacy_multi = _legacy_relation_column_spec(_relation(5), "character varying(15)[]")
    legacy_single = _legacy_relation_column_spec(_relation(1), "character varying(15)")
    assert (
        legacy_multi is not None and legacy_multi.default == "'{}'::character varying[]"
    )
    assert (
        legacy_single is not None and legacy_single.default == "''::character varying"
    )
    # A scalar legacy column is not a valid shape for a multiple relation.
    assert (
        _legacy_relation_column_spec(_relation(None), "character varying(15)") is None
    )
    assert _legacy_relation_column_spec(_relation(None), "text[]") is None

    spec = build_collection_table_spec(
        name="owners",
        kind="base",
        schema_raw=[
            {"name": "rels", "type": "relation", "options": {"maxSelect": None}},
            {"name": "one", "type": "relation", "options": {"maxSelect": 1}},
        ],
        live_column_types={"one": "character varying(15)"},
    )
    by_name = {column.name: column for column in spec.columns}
    assert by_name["rels"].type == "text[]"
    assert by_name["one"].type == "character varying(15)"


# ---------------------------------------------------------------------------
# Relation index, expand and filters
# ---------------------------------------------------------------------------


class _Collection:
    def __init__(self, cid, name, schema):
        self.id = cid
        self.name = name
        self.type = "base"
        self.schema = schema
        self.updated = "now"


def _collections():
    targets = _Collection(
        "target_coll_id1", "targets", [{"name": "name", "type": "text"}]
    )
    owners = _Collection(
        "owner_coll_id01",
        "owners",
        [
            {
                "name": "rels",
                "type": "relation",
                "options": {"collectionId": "target_coll_id1", "maxSelect": None},
            },
            {
                "name": "one",
                "type": "relation",
                "options": {"collectionId": "target_coll_id1", "maxSelect": 1},
            },
        ],
    )
    return targets, owners


def test_relation_targets_report_unlimited_as_the_pocketbase_upgrade_value() -> None:
    targets, owners = _collections()
    by_id = {c.id: c for c in (targets, owners)}
    by_name = {c.name: c for c in (targets, owners)}
    result = {
        name: limit
        for name, _target, limit in _relation_targets(owners, by_id, by_name)
    }
    assert result == {"rels": 2147483647, "one": 1}


def test_relation_index_marks_null_max_select_as_multiple() -> None:
    index = _build_relation_index_from_collections(list(_collections()))
    relations = index["collections"]["owners"]["relations"]
    assert relations["rels"]["multiple"] is True
    assert relations["one"]["multiple"] is False
    back = index["back_relations"]["targets"]["owners_via_rels"]
    assert back["multiple"] is True


def test_expand_uses_the_shared_rule() -> None:
    _targets, owners = _collections()
    field = _get_relation_field(owners, "rels")
    assert relation_is_multiple(field["options"]) is True


def test_filter_traversal_joins_null_max_select_relation_as_array() -> None:
    index = _build_relation_index_from_collections(list(_collections()))
    sql, params = parse_filter(
        'rels.name ?= "alpha"',
        relation_resolver=index,
        current_table="owners",
    )
    assert params == {"_fp1": "alpha"}
    assert "jsonb_array_elements_text" in sql  # array membership join
    assert '"targets"."id" = "owners"."rels"' not in sql


def test_direct_multi_value_comparisons_use_pocketbase_json_text() -> None:
    index = _build_relation_index_from_collections(list(_collections()))
    for expression in ('rels = "x"', 'rels ?= "x"', 'rels ~ "x"', 'rels ?!~ "x"'):
        sql, _params = parse_filter(
            expression, relation_resolver=index, current_table="owners"
        )
        assert 'array_to_json("owners"."rels")::text' in sql, expression
        assert "jsonb_array_elements_text" not in sql, expression
    # Single relations keep their plain column.
    sql, _params = parse_filter(
        'one = "x"', relation_resolver=index, current_table="owners"
    )
    assert "array_to_json" not in sql


def test_request_value_any_match_does_not_use_untyped_to_jsonb() -> None:
    index = _build_relation_index_from_collections(list(_collections()))
    sql, params = parse_filter(
        "@request.auth.id ?= rels",
        request_context={"auth": {"id": "user_1"}},
        relation_resolver=index,
        current_table="owners",
    )
    assert "to_jsonb(:" not in sql
    assert 'array_to_json("owners"."rels")::text' in sql
    assert "user_1" in params.values()


def test_list_request_value_any_match_unnests_a_text_array() -> None:
    sql, params = parse_filter(
        '@request.body.tags ?= "b"',
        request_context={"data": {"tags": ["a", "b", 3]}},
        current_table="owners",
    )
    assert "to_jsonb(:" not in sql
    assert "unnest(CAST(:" in sql
    assert ["a", "b", "3"] in params.values()


def test_direct_multi_value_ordering_operators_compare_json_text() -> None:
    index = _build_relation_index_from_collections(list(_collections()))
    sql, _params = parse_filter(
        'rels ?> "a"', relation_resolver=index, current_table="owners"
    )
    assert sql.startswith('array_to_json("owners"."rels")::text > :')
