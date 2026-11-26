from __future__ import annotations

from datetime import UTC, datetime

from ppbase.db.system_tables import CollectionRecord
from ppbase.services.filter_parser import parse_filter
from ppbase.services.record_service import _build_relation_index_from_collections


def _coll(
    name: str,
    schema: list[dict] | None = None,
) -> CollectionRecord:
    now = datetime.now(UTC)
    return CollectionRecord(
        id=f"{name[:12]}_id"[:15],
        name=name,
        type="base",
        system=False,
        schema=schema or [],
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


def _rel(name: str, collection_id: str, *, max_select: int = 1) -> dict:
    return {
        "name": name,
        "type": "relation",
        "required": False,
        "options": {"collectionId": collection_id, "maxSelect": max_select},
    }


def _date(name: str) -> dict:
    return {"name": name, "type": "date", "required": False}


def test_system_datetime_string_literals_are_bound_as_datetimes() -> None:
    sql, params = parse_filter(
        (
            'created >= "2026-05-17 00:00:00" '
            '&& created <= "2026-05-17 23:59:59"'
        ),
        current_table="boutique_sale",
    )

    assert '"boutique_sale"."created" >= :_fp1' in sql
    assert '"boutique_sale"."created" <= :_fp2' in sql
    assert params["_fp1"] == datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)
    assert params["_fp2"] == datetime(2026, 5, 17, 23, 59, 59, tzinfo=UTC)


def test_schema_date_string_literals_are_bound_as_datetimes() -> None:
    sales = _coll("boutique_sale", [_date("sold_at")])
    index = _build_relation_index_from_collections([sales])

    sql, params = parse_filter(
        'sold_at >= "2026-05-17T00:00:00Z"',
        relation_resolver=index,
        current_table="boutique_sale",
    )

    assert '"boutique_sale"."sold_at" >= :_fp1' in sql
    assert params["_fp1"] == datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)


def test_relation_date_string_literals_are_bound_as_datetimes() -> None:
    customers = _coll("customers", [_date("joined_at")])
    sales = _coll("boutique_sale", [_rel("customer", customers.id)])
    index = _build_relation_index_from_collections([customers, sales])

    sql, params = parse_filter(
        'customer.joined_at <= "2026-05-17"',
        relation_resolver=index,
        current_table="boutique_sale",
    )

    assert 'EXISTS (SELECT 1 FROM "customers"' in sql
    assert '"customers"."joined_at" <= :_fp1' in sql
    assert params["_fp1"] == datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)


def test_datetime_literal_forms_are_parsed_strictly() -> None:
    import pytest

    from ppbase.services.filter_parser import parse_datetime_literal

    expected = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    for value in (
        "2026-11-01",
        "2026-11-01T00:00:00Z",
        "2026-11-01T00:00:00z",
        "2026-11-01T00:00Z",
        "2026-11-01 00:00:00",
        "2026-11-01 00:00:00.000Z",
        "2026-11-01T00:00:00.000000000Z",
        "2026-11-01T01:00:00+01:00",
        "2026-11-01T01:00:00+0100",
        "2026-10-31T19:00:00-05",
    ):
        assert parse_datetime_literal(value) == expected, value

    assert parse_datetime_literal("2026-11-01 10:20:30.123Z") == datetime(
        2026, 11, 1, 10, 20, 30, 123000, tzinfo=UTC
    )

    for value in (
        "not-a-date",
        "2026-13-01",
        "2026-11-31",
        "20261101",
        "2026-W44",
        "2026-11-01T00:00:00+25:00",
        "2026-11-01 00:00:00 UTC",
    ):
        with pytest.raises(ValueError):
            parse_datetime_literal(value)


def test_invalid_datetime_literal_is_a_value_error() -> None:
    import pytest

    sales = _coll("boutique_sale", [_date("sold_at")])
    index = _build_relation_index_from_collections([sales])

    with pytest.raises(ValueError, match="Invalid datetime value"):
        parse_filter(
            'sold_at >= "tomorrow"',
            relation_resolver=index,
            current_table="boutique_sale",
        )


def test_empty_date_literal_matches_missing_dates() -> None:
    sales = _coll("boutique_sale", [_date("sold_at")])
    index = _build_relation_index_from_collections([sales])

    sql, params = parse_filter(
        'sold_at = "" || sold_at != ""',
        relation_resolver=index,
        current_table="boutique_sale",
    )

    assert '"boutique_sale"."sold_at" IS NULL' in sql
    assert '"boutique_sale"."sold_at" IS NOT NULL' in sql
    assert params == {}
