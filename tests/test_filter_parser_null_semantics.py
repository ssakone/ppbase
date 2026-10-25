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
from ppbase.services.filter_parser import (
    _sqlite_numeric_text,
    _sqlite_real_text,
    parse_filter,
)
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
            {
                "name": "rels",
                "type": "relation",
                "options": {"collectionId": target.id, "maxSelect": 5},
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
        # json ~ / !~: a JSON null is NULL (JSON_EXTRACT), never matched.
        ('meta ~ "x"', """NULLIF("v"."meta"::text, 'null')::text ILIKE :_fp2"""),
        (
            'meta !~ "x"',
            """NULLIF("v"."meta"::text, 'null')::text NOT ILIKE :_fp2""",
        ),
        (
            'meta.k !~ "x"',
            (
                """NULLIF(jsonb_extract_path("v"."meta", 'k')::text, 'null')::text """
                """NOT ILIKE :_fp2"""
            ),
        ),
        (
            "txt ~ meta",
            """"v"."txt"::text ILIKE ('%' || NULLIF("v"."meta"::text, 'null')::text || '%')""",
        ),
        # json and a known non-empty value: IS NOT keeps the NULL row; the
        # value comparison itself is unchanged (open divergence).
        ('meta = "x"', '"v"."meta" = :_fp1'),
        ('meta != "x"', '"v"."meta" IS DISTINCT FROM :_fp1'),
        (
            '"x" ?!= meta',
            """("v"."meta" IS NULL OR "v"."meta"::text != CAST(:_fp1 AS text))""",
        ),
        # json and another column: NULL-safe, without COALESCE (IS / IS NOT).
        (
            "meta = meta",
            (
                """(NULLIF(to_jsonb("v"."meta"), 'null'::jsonb) IS NOT DISTINCT FROM """
                """NULLIF(to_jsonb("v"."meta"), 'null'::jsonb))"""
            ),
        ),
        (
            "meta != txt",
            (
                """(NULLIF(to_jsonb("v"."meta"), 'null'::jsonb) IS DISTINCT FROM """
                """NULLIF(to_jsonb("v"."txt"), 'null'::jsonb))"""
            ),
        ),
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


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # No column at all: SQLite compares '' = v / v = w without affinity
        # (filter.go l.318-338); PostgreSQL would reject 1 = '' (HTTP 500).
        ("1 = null", "FALSE"),
        ("1 != null", "TRUE"),
        ("0 != null", "TRUE"),
        ("null = 0", "FALSE"),
        ("true = null", "FALSE"),
        ("true != null", "TRUE"),
        ("false != null", "TRUE"),
        ('1 = ""', "FALSE"),
        ('1 != ""', "TRUE"),
        ('"" = 0', "FALSE"),
        ('true = ""', "FALSE"),
        ('true != ""', "TRUE"),
        ("null = null", "TRUE"),
        ('"" != null', "FALSE"),
        ('"x" = null', "FALSE"),
        ('"x" != ""', "TRUE"),
        ("1 = 1", "TRUE"),
        ('1 = "1"', "FALSE"),
        ("true = 1", "TRUE"),
        ('"a" = "b"', "FALSE"),
        # ?op without any column is op (no multi-match).
        ("1 ?= null", "FALSE"),
        ("1 ?!= null", "TRUE"),
        ("null ?= null", "TRUE"),
        ('"a" ?= "a"', "TRUE"),
        # A compound filter keeps its column part.
        ('title = "a" || 1 = null', '("v"."title" = :_fp1 OR FALSE)'),
        ('title = "a" && 1 != null', '("v"."title" = :_fp1 AND TRUE)'),
    ],
)
def test_comparisons_without_a_column_are_folded(
    expression: str, expected: str
) -> None:
    assert _sql(expression) == expected


def test_multi_match_fails_a_json_null() -> None:
    # multiMatchValue is JSON_EXTRACT(...): NULL for a JSON null too, so the
    # "IS NULL" clause (filter.go l.680-706) fails it.
    for expression in ('rels.tj != ""', 'rels.tj ~ "x"', "rels.tj.k != null"):
        sql = _sql(expression)
        assert 'NULLIF(to_jsonb("__pbsm' in sql, expression
        assert """"."v"), 'null'::jsonb) IS NULL""" in sql, expression
    # ``=`` has no NULL clause.
    assert "IS NULL" not in _sql('rels.tj = ""')


def test_json_any_not_equal_keeps_a_null_row() -> None:
    sql = _sql('meta ?!= "x"')
    assert sql.startswith('("v"."meta" IS NULL OR (CASE WHEN'), sql
    # ?= is unchanged: a NULL never equals a known non-empty value.
    assert _sql('meta ?= "x"').startswith("(CASE WHEN")


def _parse(expression: str, request_context: dict | None = None) -> tuple[str, dict]:
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
        ],
    )
    index = _build_relation_index_from_collections([view, target])
    return parse_filter(
        expression,
        request_context=request_context,
        relation_resolver=index,
        collection_resolver=index,
        current_table="v",
    )


_D_TEXT = (
    """COALESCE(to_char("v"."d" AT TIME ZONE 'UTC', """
    """'YYYY-MM-DD HH24:MI:SS.MS"Z"'), '')"""
)


@pytest.mark.parametrize(
    ("expression", "expected", "text"),
    [
        # A TEXT column turns a value into text: a filter number is a REAL
        # ('1.0'), true/false the integers 1/0.  ?= is = (current record).
        ("txt ?= 0", '("v"."txt" = :_fp1)', "0.0"),
        ("txt = 1", '("v"."txt" = :_fp1)', "1.0"),
        ("txt ?= 4.5", '("v"."txt" = :_fp1)', "4.5"),
        ("1 ?!= txt", '(:_fp1 IS DISTINCT FROM "v"."txt")', "1.0"),
        ("title ?= true", '("v"."title" = :_fp1)', "1"),
        ("txt != false", '("v"."txt" IS DISTINCT FROM :_fp1)', "0"),
        # A date column is its PocketBase text.
        ('d ?= "x"', f"({_D_TEXT} = :_fp1)", "x"),
        ("d != 1", f"({_D_TEXT} IS DISTINCT FROM :_fp1)", "1.0"),
        # A NUMERIC column (number, bool, multi-value json) turns a numeric
        # text into a number; any other text never equals a number.
        ('num ?= "x"', "FALSE", None),
        ('num ?!= "x"', "TRUE", None),
        ('num = " 1 "', '("v"."num" = 1)', None),
        ('num ?= "4.5"', """("v"."num" = CAST('4.5' AS double precision))""", None),
        ("num = true", '("v"."num" = 1)', None),
        ('flag ?= "1"', '(("v"."flag")::int = 1)', None),
        ("flag = 1", """(("v"."flag")::int = CAST('1.0' AS double precision))""", None),
        ('flag ?!= "true"', "TRUE", None),
        ("sels ?= 1", "FALSE", None),
        # Two columns: COALESCE(a, '') = COALESCE(b, ''), without affinity.
        (
            "num ?!= txt",
            """(NOT ("v"."num" IS NULL AND COALESCE("v"."txt", '') = ''))""",
            None,
        ),
        ("num = flag", '("v"."num" IS NOT DISTINCT FROM ("v"."flag")::int)', None),
        (
            "d ?= txt",
            f"""(COALESCE({_D_TEXT}, '') = COALESCE("v"."txt", ''))""",
            None,
        ),
        # Operands of one type keep their typed comparison.
        ('txt ?= "x"', '"v"."txt" = :_fp1', "x"),
        ("num ?!= 7", '"v"."num" IS DISTINCT FROM 7', None),
    ],
)
def test_operands_of_different_types_compare_like_sqlite(
    expression: str, expected: str, text: str | None
) -> None:
    sql, params = _parse(expression)
    assert sql == expected
    if text is not None:
        assert params["_fp1"] == text


def test_request_values_of_another_type_compare_like_sqlite() -> None:
    context = {"query": {"q": "1", "x": "x"}, "data": {"n": 4.5, "b": True}}
    assert _parse("@request.query.q ?= num", context)[0] == '(1 = "v"."num")'
    assert _parse("@request.query.x = num", context)[0] == "FALSE"
    sql, params = _parse("@request.data.n ?= txt", context)
    assert sql == '(:_fp2 = "v"."txt")'
    assert params["_fp2"] == "4.5"
    sql, params = _parse("@request.data.b != txt", context)
    assert sql == '(:_fp2 IS DISTINCT FROM "v"."txt")'
    assert params["_fp2"] == "1"


@pytest.mark.parametrize(
    ("expression", "distinct"),
    [
        ("meta ?= txt", "IS NOT DISTINCT FROM"),
        ("meta ?!= txt", "IS DISTINCT FROM"),
        ("txt ?!= meta", "IS DISTINCT FROM"),
        ("meta ?= meta", "IS NOT DISTINCT FROM"),
    ],
)
def test_json_any_equal_to_a_column_is_null_safe(
    expression: str, distinct: str
) -> None:
    # ?= / ?!= are = / != : JSON_EXTRACT(x) IS [NOT] y, no "x IS NULL OR".
    sql = _parse(expression)[0]
    assert sql.startswith("(NULLIF(to_jsonb(") and f") {distinct} NULLIF(" in sql
    assert "IS NULL OR" not in sql


def test_json_compared_with_a_date_column_reads_its_text() -> None:
    sql = _parse("meta ?!= d")[0]
    assert sql == (
        """(NULLIF(to_jsonb("v"."meta"), 'null'::jsonb) IS DISTINCT FROM """
        f"""NULLIF(to_jsonb({_D_TEXT}), 'null'::jsonb))"""
    )


def test_any_equal_of_an_unknown_operand_keeps_the_text_comparison() -> None:
    # Without metadata the type of a field is unknown: ?= keeps the text
    # comparison instead of a typed one PostgreSQL could reject.
    sql, _params = parse_filter("x ?= 1")
    assert sql.startswith("(CASE WHEN jsonb_typeof(to_jsonb(")
    assert "1::text = " in sql


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (1.0, "1.0"),
        (0.0, "0.0"),
        (0.1, "0.1"),
        (4.5, "4.5"),
        (-1.0, "-1.0"),
        (100.0, "100.0"),
        (1e21, "1.0e+21"),
        (1e-05, "1.0e-05"),
        (123456789012345678.0, "1.23456789012346e+17"),
    ],
)
def test_sqlite_real_text(value: float, text: str) -> None:
    assert _sqlite_real_text(value) == text


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("1", 1),
        (" 1 ", 1),
        ("+1", 1),
        ("-2", -2),
        ("1.0", 1.0),
        ("1e0", 1.0),
        (".5", 0.5),
        ("5.", 5.0),
        ("x", None),
        ("", None),
        ("true", None),
        ("0x10", None),
        ("1e", None),
        ("1 2", None),
    ],
)
def test_sqlite_numeric_text(text: str, number: float | None) -> None:
    assert _sqlite_numeric_text(text) == number
