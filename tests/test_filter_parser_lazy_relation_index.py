from __future__ import annotations

import re
from datetime import datetime, timezone
from time import perf_counter

from ppbase.db.system_tables import CollectionRecord
from ppbase.services.filter_parser import parse_filter
from ppbase.services.record_service import _build_relation_index_from_collections


def _coll(
    name: str,
    schema: list[dict] | None = None,
) -> CollectionRecord:
    now = datetime.now(timezone.utc)
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


def _aliases(sql: str, table: str) -> list[str]:
    """Main-query aliases of ``table`` (PocketBase-like LEFT JOINs)."""
    return re.findall(rf'LEFT JOIN "{table}" AS "(__pb\d+)"', sql)


def _rel(name: str, collection_id: str, *, max_select: int = 1) -> dict:
    return {
        "name": name,
        "type": "relation",
        "required": False,
        "options": {"collectionId": collection_id, "maxSelect": max_select},
    }


def _text(name: str) -> dict:
    return {"name": name, "type": "text", "required": False}


def _json(name: str) -> dict:
    return {"name": name, "type": "json", "required": False}


def test_flat_relation_index_keeps_bumo_collection_rule_fast() -> None:
    users = _coll("users")
    business = _coll(
        "business",
        [_text("name"), _rel("owner", users.id)],
    )
    business_access = _coll(
        "business_access",
        [
            _rel("user", users.id),
            _rel("business", business.id),
        ],
    )
    extra_names = [f"m{i:03d}" for i in range(120)]
    extra = [
        _coll(
            name,
            [_text("name"), _rel("next", f"{extra_names[(i + 1) % 120]}_id")],
        )
        for i, name in enumerate(extra_names)
    ]
    collections = [users, business, business_access, *extra]

    started = perf_counter()
    index = _build_relation_index_from_collections(collections)
    sql, params = parse_filter(
        (
            "@request.auth.id != '' && "
            "(@request.auth.id = owner.id || "
            "(@collection.business_access.user ?= @request.auth.id && "
            "@collection.business_access.business.id ?= id))"
        ),
        {"auth": {"id": "user_1"}},
        index,
        index,
        current_table="business",
    )
    elapsed = perf_counter() - started

    assert elapsed < 1.0
    assert "user_1" in params.values()
    assert index["__flat__"] is True
    assert "relations" not in index["collections"]["business"]["relations"]["owner"]
    # Both @collection operands share the same business_access row.
    (access,) = _aliases(sql, "business_access")
    (target,) = _aliases(sql, "business")
    assert f'"{access}"."user" = :' in sql
    assert f'"{target}"."id" = "{access}"."business"' in sql
    assert "ANY(" not in sql


def test_flat_relation_index_handles_cycles_path_based() -> None:
    collection_a = _coll("cycle_a", [_text("name")])
    collection_b = _coll("cycle_b", [_text("name")])
    collection_a.schema.append(_rel("to_b", collection_b.id))
    collection_b.schema.append(_rel("to_a", collection_a.id))
    index = _build_relation_index_from_collections([collection_a, collection_b])

    sql, params = parse_filter(
        'to_b.to_a.to_b.name = "match"',
        relation_resolver=index,
        current_table="cycle_a",
    )

    assert params == {"_fp1": "match"}
    first_b, last_b = _aliases(sql, "cycle_b")
    (middle_a,) = _aliases(sql, "cycle_a")
    assert f'"{first_b}"."id" = "cycle_a"."to_b"' in sql
    assert f'"{middle_a}"."id" = "{first_b}"."to_a"' in sql
    assert f'"{last_b}"."id" = "{middle_a}"."to_b"' in sql
    assert f'"{last_b}"."name" = :_fp1' in sql


def test_flat_relation_index_groups_back_relation_json_path_on_same_record() -> None:
    users = _coll("users")
    business = _coll("business", [_text("name")])
    roles = _coll("business_roles", [_text("name"), _json("permissions")])
    business_access = _coll(
        "business_access",
        [
            _rel("user", users.id),
            _rel("business", business.id),
            _rel("role", roles.id),
        ],
    )
    index = _build_relation_index_from_collections(
        [users, business, roles, business_access]
    )

    sql, params = parse_filter(
        (
            "business_access_via_business.user ?= @request.auth.id && "
            "business_access_via_business.role.permissions.wallet.read ?= true"
        ),
        {"auth": {"id": "user_1"}},
        index,
        current_table="business",
    )

    assert params == {"_fp1": "user_1"}
    (access,) = _aliases(sql, "business_access")
    (roles,) = _aliases(sql, "business_roles")
    assert sql.count("EXISTS (SELECT 1 FROM (SELECT 1)") == 1
    assert f'"{access}"."business" = "business"."id"' in sql
    assert f'"{access}"."user" = :_fp1' in sql
    assert (
        f'LEFT JOIN "business_roles" AS "{roles}" ON "{roles}"."id" = "{access}"."role"'
    ) in sql
    assert f'jsonb_extract_path("{roles}"."permissions", \'wallet\', \'read\')' in sql


def test_flat_relation_index_resolves_boutique_wallet_caisse_sale_paths() -> None:
    business = _coll("business", [_text("name")])
    sale = _coll("sale", [_text("name")])
    caisse = _coll("caisse", [_rel("sale", sale.id)])
    boutique_wallet = _coll(
        "boutique_wallet",
        [
            _rel("business", business.id),
            _rel("caisse", caisse.id),
        ],
    )
    index = _build_relation_index_from_collections(
        [business, sale, caisse, boutique_wallet]
    )

    sql, params = parse_filter(
        'boutique_wallet_via_business.caisse.sale.name = "ticket"',
        relation_resolver=index,
        current_table="business",
    )

    assert params == {"_fp1": "ticket"}
    (wallet,) = _aliases(sql, "boutique_wallet")
    (caisse_alias,) = _aliases(sql, "caisse")
    (sale_alias,) = _aliases(sql, "sale")
    assert f'"{wallet}"."business" = "business"."id"' in sql
    assert f'"{caisse_alias}"."id" = "{wallet}"."caisse"' in sql
    assert f'"{sale_alias}"."id" = "{caisse_alias}"."sale"' in sql
    assert f'"{sale_alias}"."name" = :_fp1' in sql
    # The back relation has no unique index: PocketBase multi-match.
    assert "NOT EXISTS" in sql
