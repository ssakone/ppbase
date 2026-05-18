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
