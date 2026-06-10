from __future__ import annotations

from ppbase.services.filter_parser import parse_filter


def test_like_casts_numeric_field_to_text() -> None:
    sql, params = parse_filter('total ~ "898"', current_table="orders")

    assert '"orders"."total"::text ILIKE' in sql
    assert "%898%" in params.values()


def test_like_wraps_field_operand_as_contains_pattern() -> None:
    sql, params = parse_filter("total ~ invoice_no", current_table="orders")

    assert params == {}
    assert (
        '"orders"."total"::text ILIKE '
        '(\'%\' || "orders"."invoice_no"::text || \'%\')'
    ) in sql

