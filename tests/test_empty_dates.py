"""Unit tests for empty date values (no database).

PocketBase serves an empty date as "" and compares/sorts it as the SQLite
text '' (v0.22.34 ``tools/types/datetime.go`` l.47-58,
``tools/search/filter.go`` l.155-176 and l.290-368, ``tools/search/sort.go``
l.23-37).  PPBase stores it as NULL; see ``docs/empty-dates.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ppbase.db.system_tables import CollectionRecord
from ppbase.models.record import build_record_response
from ppbase.services.filter_parser import parse_filter
from ppbase.services.record_service import (
    _build_relation_index_from_collections,
    _sort_term,
)

_SCHEMA = [
    {"name": "title", "type": "text"},
    {"name": "d", "type": "date"},
    {"name": "a", "type": "autodate"},
]


def _coll(name: str, schema: list[dict]) -> CollectionRecord:
    now = datetime.now(UTC)
    return CollectionRecord(
        id=f"{name[:12]}_id"[:15],
        name=name,
        type="base",
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


def _sql(expression: str) -> tuple[str, dict]:
    targets = _coll("tg", [{"name": "td", "type": "date"}])
    dates = _coll(
        "t",
        [
            {"name": "d", "type": "date"},
            {"name": "e", "type": "date"},
            {"name": "title", "type": "text"},
            {
                "name": "other",
                "type": "relation",
                "options": {"collectionId": targets.id, "maxSelect": 1},
            },
        ],
    )
    index = _build_relation_index_from_collections([dates, targets])
    return parse_filter(
        expression,
        relation_resolver=index,
        collection_resolver=index,
        current_table="t",
    )


def test_null_date_is_served_as_empty_string() -> None:
    stamp = datetime(2024, 2, 3, 4, 5, 6, 789000, tzinfo=UTC)
    row = {"id": "r1", "created": stamp, "updated": stamp, "title": None, "d": None}
    record = build_record_response(row, "cid", "cname", _SCHEMA)
    assert record["d"] == ""
    assert record["a"] == ""  # absent autodate column
    assert record["title"] is None  # only date types are DateTime values
    row["d"] = stamp
    assert build_record_response(row, "cid", "cname", _SCHEMA)["d"] == (
        "2024-02-03 04:05:06.789Z"
    )


def test_view_record_omits_zero_created_and_updated() -> None:
    stamp = datetime(2024, 2, 3, tzinfo=UTC)
    without = build_record_response(
        {"id": "v1", "d": None}, "cid", "view", _SCHEMA[:2], is_view_collection=True
    )
    assert "created" not in without and "updated" not in without
    assert without["d"] == ""
    selected = build_record_response(
        {"id": "v1", "created": stamp, "d": None},
        "cid",
        "view",
        _SCHEMA[:2],
        is_view_collection=True,
    )
    assert selected["created"] == "2024-02-03 00:00:00.000Z"
    assert "updated" not in selected
    base = build_record_response({"id": "b1"}, "cid", "base", _SCHEMA[:2])
    assert base["created"] == "" and base["updated"] == ""


@pytest.mark.parametrize(
    ("direction", "expected"),
    [("ASC", '"d" ASC NULLS FIRST'), ("DESC", '"d" DESC NULLS LAST')],
)
def test_date_sort_puts_empty_dates_where_sqlite_puts_empty_text(
    direction: str,
    expected: str,
) -> None:
    assert _sort_term('"d"', direction, {"d"}) == expected
    assert _sort_term('"title"', direction, {"d"}) == f'"title" {direction}'
    assert _sort_term("RANDOM()", direction, {"d"}) == f"RANDOM() {direction}"


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('d = ""', '"t"."d" IS NULL'),
        ("d = null", '"t"."d" IS NULL'),
        ('d != ""', '"t"."d" IS NOT NULL'),
        ('d < ""', "FALSE"),
        ('d <= ""', '"t"."d" IS NULL'),
        ('d > ""', '"t"."d" IS NOT NULL'),
        ('d >= ""', "TRUE"),
        ('"" < d', '"t"."d" IS NOT NULL'),
        ("d < null", '"t"."d" < NULL'),
        ('d < "2024-01-01"', '("t"."d" < :_fp1 OR "t"."d" IS NULL)'),
        ('d <= "2024-01-01"', '("t"."d" <= :_fp1 OR "t"."d" IS NULL)'),
        ('d > "2024-01-01"', '"t"."d" > :_fp1'),
        ('d != "2024-01-01"', '("t"."d" <> :_fp1 OR "t"."d" IS NULL)'),
        ('"2024-01-01" > d', '("t"."d" < :_fp1 OR "t"."d" IS NULL)'),
        ("d = e", '("t"."d" IS NOT DISTINCT FROM "t"."e")'),
        ("d != e", '("t"."d" IS DISTINCT FROM "t"."e")'),
        ("d < e", '("t"."d" < "t"."e" OR ("t"."d" IS NULL AND "t"."e" IS NOT NULL))'),
        ("d >= e", '("t"."d" >= "t"."e" OR "t"."e" IS NULL)'),
        ("created < d", '"t"."d" > "t"."created"'),
        ('title = ""', '"t"."title" = :_fp1'),
    ],
)
def test_date_comparisons_treat_null_as_the_empty_text(
    expression: str,
    expected: str,
) -> None:
    assert _sql(expression)[0] == expected


def test_date_like_uses_the_pocketbase_text_of_the_date() -> None:
    sql, params = _sql('d !~ "2024"')
    assert sql == (
        "COALESCE(to_char(\"t\".\"d\" AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD HH24:MI:SS.MS\"Z\"'), '')::text NOT ILIKE :_fp2"
    )
    assert params["_fp2"] == "%2024%"


def test_relation_and_collection_date_leaves_treat_null_as_empty() -> None:
    sql, _ = _sql('other.td = ""')
    assert sql.endswith('AND "tg"."td" IS NULL)')
    sql, _ = _sql('other.td < "2024-01-01"')
    assert sql.endswith('AND ("tg"."td" < :_fp1 OR "tg"."td" IS NULL))')
    sql, _ = _sql('@collection.tg.td != ""')
    assert sql == 'EXISTS (SELECT 1 FROM "tg" WHERE "tg"."td" IS NOT NULL)'


def test_request_values_follow_the_empty_date_rules() -> None:
    targets = _coll("t", [{"name": "d", "type": "date"}])
    index = _build_relation_index_from_collections([targets])
    for data, expected in (
        ({"x": ""}, '"t"."d" IS NULL'),
        ({"x": None}, '"t"."d" IS NULL'),
        ({}, '"t"."d" IS NULL'),
    ):
        sql, _ = parse_filter(
            "d = @request.data.x",
            {"data": data},
            relation_resolver=index,
            current_table="t",
        )
        assert sql == expected
