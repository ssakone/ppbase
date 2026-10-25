"""Unit tests: filters and sorts on NULL values like PocketBase (no database).

PocketBase 0.22.34 builds ``=``/``!=`` with ``resolveEqualExpr``
(``tools/search/filter.go`` l.284-368, the same in v0.40.6 l.334-422): an
empty operand (``""``/``null``) also matches NULL, ``!=`` is NULL-safe
(``IS NOT``), two columns are compared with ``COALESCE(x, '')`` and a json
operand without COALESCE (``JSON_EXTRACT(x) IS [NOT] y``).  A current-record
``?op`` is ``op``.  SQLite sorts NULL first in ascending order.  The
PostgreSQL behaviour is checked in
``tests/integration/test_null_zero_values.py``; see ``docs/null-values.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ppbase.db.system_tables import CollectionRecord
from ppbase.services.filter_parser import parse_filter
from ppbase.services.record_service import (
    _build_relation_index_from_collections,
    _sort_term,
)


def _coll(name: str, schema: list[dict], kind: str = "view") -> CollectionRecord:
    now = datetime.now(UTC)
    return CollectionRecord(
        id=f"{name[:12]}_id"[:15],
        name=name,
        type=kind,
        system=False,
        schema=schema,
        indexes=[],
        list_rule="",
        view_rule="",
        create_rule="",
        update_rule="",
        delete_rule="",
        options={},
        created=now,
        updated=now,
    )


def _sql(expression: str) -> str:
    target = _coll("tg", [{"name": "tj", "type": "json"}], kind="base")
    view = _coll(
        "v",
        [
            {"name": "title", "type": "text"},
            {"name": "txt", "type": "text"},
            {"name": "num", "type": "number"},
            {"name": "flag", "type": "bool"},
            {"name": "meta", "type": "json"},
            {"name": "sels", "type": "select", "options": {"maxSelect": 2}},
            {"name": "d", "type": "date"},
            {
                "name": "rel",
                "type": "relation",
                "options": {"collectionId": target.id, "maxSelect": 1},
            },
        ],
    )
    index = _build_relation_index_from_collections([view, target])
    sql, _params = parse_filter(
        expression,
        relation_resolver=index,
        collection_resolver=index,
        current_table="v",
    )
    return sql


_JSON_NULL = """("v"."meta" IS NULL OR jsonb_typeof(to_jsonb("v"."meta")) = 'null')"""


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # Empty operand: the empty value or NULL.
        ('txt = ""', """("v"."txt"::text = '' OR "v"."txt" IS NULL)"""),
        ("txt = null", """("v"."txt"::text = '' OR "v"."txt" IS NULL)"""),
        ('"" = txt', """("v"."txt"::text = '' OR "v"."txt" IS NULL)"""),
        (
            'txt != ""',
            """("v"."txt"::text IS DISTINCT FROM '' AND "v"."txt" IS NOT NULL)""",
        ),
        ('num = ""', """("v"."num"::text = '' OR "v"."num" IS NULL)"""),
        ("flag = null", """("v"."flag"::text = '' OR "v"."flag" IS NULL)"""),
        # Known non-empty value: = unchanged, != NULL-safe.
        ('txt = "x"', '"v"."txt" = :_fp1'),
        ('txt != "x"', '"v"."txt" IS DISTINCT FROM :_fp1'),
        ("num != 7", '"v"."num" IS DISTINCT FROM 7'),
        ("flag != true", '"v"."flag" IS DISTINCT FROM TRUE'),
        ("num = 0", '"v"."num" = 0'),
        # A multi-value field compares its JSON text.
        (
            'sels = ""',
            (
                """(array_to_json("v"."sels")::text::text = ''"""
                """ OR array_to_json("v"."sels")::text IS NULL)"""
            ),
        ),
        # Two columns: COALESCE(a, '') = COALESCE(b, ''), typed when not NULL.
        (
            "txt = title",
            (
                """("v"."txt" = "v"."title" OR ("v"."txt" IS NULL AND """
                """COALESCE("v"."title"::text, '') = '') OR ("v"."title" IS NULL """
                """AND COALESCE("v"."txt"::text, '') = ''))"""
            ),
        ),
        (
            "txt != title",
            (
                """("v"."txt" IS DISTINCT FROM "v"."title" AND ("v"."txt" IS NOT NULL """
                """OR COALESCE("v"."title"::text, '') <> '') AND ("v"."title" IS NOT """
                """NULL OR COALESCE("v"."txt"::text, '') <> ''))"""
            ),
        ),
        # Current-record ?op is op (no multi-match).
        ('txt ?= ""', """("v"."txt"::text = '' OR "v"."txt" IS NULL)"""),
        ('txt ?!= "x"', '"v"."txt" IS DISTINCT FROM :_fp1'),
        ("num ?> 1", '"v"."num" > 1'),
        ('d ?= ""', '"v"."d" IS NULL'),
        # json: JSON null and SQL NULL are null; only the JSON string "" is "".
        ("meta = null", _JSON_NULL),
        ("null = meta", _JSON_NULL),
        ("meta ?= null", _JSON_NULL),
        (
            "meta != null",
            """("v"."meta" IS NOT NULL AND jsonb_typeof(to_jsonb("v"."meta")) <> 'null')""",
        ),
        ('meta = ""', """(to_jsonb("v"."meta") = '""'::jsonb)"""),
        ('meta != ""', """(to_jsonb("v"."meta") IS DISTINCT FROM '""'::jsonb)"""),
        (
            "meta.k = null",
            (
                """(jsonb_extract_path("v"."meta", 'k') IS NULL OR """
                """jsonb_typeof(to_jsonb(jsonb_extract_path("v"."meta", 'k'))) = 'null')"""
            ),
        ),
        # Other operators keep their SQL (NULL never matches, as in SQLite).
        ("num > 1", '"v"."num" > 1'),
        ('txt !~ "x"', '"v"."txt"::text NOT ILIKE :_fp2'),
    ],
)
def test_direct_comparisons_follow_resolve_equal_expr(
    expression: str, expected: str
) -> None:
    assert _sql(expression) == expected


def test_json_path_leaf_compares_json_null() -> None:
    sql = _sql("rel.tj = null")
    assert "IS NULL OR jsonb_typeof(to_jsonb(" in sql
    assert _sql('rel.tj = ""').count("""= '""'::jsonb""") == 1


def test_request_value_compared_with_a_json_column() -> None:
    target = _coll("tg", [{"name": "tj", "type": "json"}], kind="base")
    index = _build_relation_index_from_collections([target])
    sql, _params = parse_filter(
        "@request.data.missing = tj",
        request_context={"data": {}},
        relation_resolver=index,
        collection_resolver=index,
        current_table="tg",
    )
    assert (
        sql == """("tg"."tj" IS NULL OR jsonb_typeof(to_jsonb("tg"."tj")) = 'null')"""
    )


@pytest.mark.parametrize(("direction", "nulls"), [("ASC", "FIRST"), ("DESC", "LAST")])
def test_sort_places_null_like_sqlite(direction: str, nulls: str) -> None:
    # Every column of a view can be NULL (LEFT JOIN without match).
    assert _sort_term('"txt"', direction, set(), nullable=True) == (
        f'"txt" {direction} NULLS {nulls}'
    )
    # A json null is SQL NULL for PocketBase (JSON_EXTRACT).
    assert _sort_term('"meta"', direction, set(), json_fields={"meta"}) == (
        f"""NULLIF(to_jsonb("meta"), 'null'::jsonb) {direction} NULLS {nulls}"""
    )
    # Base/auth columns other than dates are NOT NULL: unchanged.
    assert _sort_term('"txt"', direction, set()) == f'"txt" {direction}'
    assert _sort_term('"d"', direction, {"d"}) == f'"d" {direction} NULLS {nulls}'
    assert _sort_term("RANDOM()", direction, set(), nullable=True) == (
        f"RANDOM() {direction}"
    )
