from __future__ import annotations

import re

from ppbase.services.filter_parser import parse_filter


def _aliases(sql: str, table: str) -> list[str]:
    """Main-query aliases of ``table`` (PocketBase-like LEFT JOINs)."""
    return re.findall(rf'LEFT JOIN "{table}" AS "(__pb\d+)"', sql)


def test_collection_reference_simple_field_still_groups_by_alias() -> None:
    sql, params = parse_filter(
        (
            '@collection.refs:first.linkKey = itemKey && '
            '@collection.refs:second.tag = "b"'
        ),
        current_table="items",
    )

    assert params == {"_fp1": "b"}
    # One LEFT JOIN per @collection alias, each in its own EXISTS (they do
    # not share rows), plus PocketBase's multi-match subquery.
    first, second = _aliases(sql, "refs")
    assert f'LEFT JOIN "refs" AS "{first}" ON TRUE' in sql
    assert (
        f'COALESCE("{first}"."linkKey"::text, \'\') = '
        'COALESCE("items"."itemKey"::text, \'\')'
    ) in sql
    assert f'"{second}"."tag" = :_fp1' in sql
    assert sql.count("EXISTS (SELECT 1 FROM (SELECT 1)") == 2
    assert sql.count("NOT EXISTS") == 2


def test_relation_traversal_still_qualifies_current_table() -> None:
    sql, params = parse_filter(
        "owner.id = @request.auth.id",
        {"auth": {"id": "user_1"}},
        {"owner": {"table": "users", "max_select": 1, "relations": {}}},
        current_table="business",
    )

    assert params == {"_fp1": "user_1"}
    (users,) = _aliases(sql, "users")
    assert f'LEFT JOIN "users" AS "{users}" ON "{users}"."id" = "business"."owner"' in sql
    assert f'"{users}"."id" = :_fp1' in sql
    assert "NOT EXISTS" not in sql  # a single relation has no multi-match


def test_any_equals_is_scalar_safe_for_current_collection_fields() -> None:
    sql, params = parse_filter('tags ?= "vip"', current_table="posts")

    assert params == {"_fp1": "vip"}
    assert "jsonb_array_elements_text" in sql
    assert "ANY(" not in sql
    assert '"posts"."tags"' in sql


def test_collection_relation_id_joins_the_target_like_pocketbase_0_22() -> None:
    # PocketBase 0.22.34 joins the target for ``rel.id`` (a missing target is
    # NULL); reading the stored column only exists since v0.23.0.
    sql, params = parse_filter(
        "@collection.business_access.business.id ?= id",
        collection_resolver={
            "business_access": {
                "table": "business_access",
                "relations": {
                    "business": {
                        "table": "business",
                        "max_select": 1,
                        "relations": {},
                    },
                },
            },
        },
        current_table="business",
    )

    assert params == {}
    (access,) = _aliases(sql, "business_access")
    (business,) = _aliases(sql, "business")
    assert f'LEFT JOIN "business_access" AS "{access}" ON TRUE' in sql
    assert (
        f'LEFT JOIN "business" AS "{business}" '
        f'ON "{business}"."id" = "{access}"."business"'
    ) in sql
    assert f'COALESCE("{business}"."id"::text, \'\')' in sql
    assert 'COALESCE("business"."id"::text, \'\')' in sql
    assert "NOT EXISTS" not in sql  # ?= is any-match
    assert "ANY(" not in sql


def test_collection_relation_field_generates_target_exists() -> None:
    sql, params = parse_filter(
        "@collection.business_access.business.owner ?= @request.auth.id",
        {"auth": {"id": "user_1"}},
        collection_resolver={
            "business_access": {
                "table": "business_access",
                "relations": {
                    "business": {
                        "table": "business",
                        "max_select": 1,
                        "relations": {
                            "owner": {
                                "table": "users",
                                "max_select": 1,
                                "relations": {},
                            },
                        },
                    },
                },
            },
        },
        current_table="business",
    )

    assert params == {"_fp1": "user_1"}
    (access,) = _aliases(sql, "business_access")
    (business,) = _aliases(sql, "business")
    assert (
        f'LEFT JOIN "business" AS "{business}" '
        f'ON "{business}"."id" = "{access}"."business"'
    ) in sql
    assert f'"{business}"."owner" = :_fp1' in sql
    assert "ANY(" not in sql


def test_back_relation_generates_reverse_exists() -> None:
    sql, params = parse_filter(
        "business_access_via_business.user ?= @request.auth.id",
        {"auth": {"id": "user_1"}},
        {
            "__fields__": {"id": {"type": "text"}},
            "business_access_via_business": {
                "kind": "back",
                "table": "business_access",
                "back_field": "business",
                "max_select": 1,
                "relations": {
                    "__fields__": {
                        "user": {"type": "relation"},
                    },
                },
            },
        },
        current_table="business",
    )

    assert params == {"_fp1": "user_1"}
    (access,) = _aliases(sql, "business_access")
    assert (
        f'LEFT JOIN "business_access" AS "{access}" '
        f'ON "{access}"."business" = "business"."id"'
    ) in sql
    assert f'"{access}"."user" = :_fp1' in sql
    assert "ANY(" not in sql


def test_back_relation_after_forward_relation_is_nested() -> None:
    sql, params = parse_filter(
        "account.business_access_via_business.user ?= @request.auth.id",
        {"auth": {"id": "user_1"}},
        {
            "__fields__": {"account": {"type": "relation"}},
            "account": {
                "kind": "forward",
                "field": "account",
                "table": "business",
                "max_select": 1,
                "relations": {
                    "__fields__": {"id": {"type": "text"}},
                    "business_access_via_business": {
                        "kind": "back",
                        "table": "business_access",
                        "back_field": "business",
                        "max_select": 1,
                        "relations": {
                            "__fields__": {
                                "user": {"type": "relation"},
                            },
                        },
                    },
                },
            },
        },
        current_table="sessions",
    )

    assert params == {"_fp1": "user_1"}
    (business,) = _aliases(sql, "business")
    (access,) = _aliases(sql, "business_access")
    assert (
        f'LEFT JOIN "business" AS "{business}" '
        f'ON "{business}"."id" = "sessions"."account" '
        f'LEFT JOIN "business_access" AS "{access}" '
        f'ON "{access}"."business" = "{business}"."id"'
    ) in sql
    assert f'"{access}"."user" = :_fp1' in sql


def test_back_relation_can_continue_through_relation_to_json_path() -> None:
    sql, params = parse_filter(
        (
            "business_access_via_business.user ?= @request.auth.id && "
            "business_access_via_business.role.permissions.wallet.read ?= true"
        ),
        {"auth": {"id": "user_1"}},
        {
            "__fields__": {"id": {"type": "text"}},
            "business_access_via_business": {
                "kind": "back",
                "table": "business_access",
                "back_field": "business",
                "max_select": 1,
                "relations": {
                    "__fields__": {
                        "user": {"type": "relation"},
                        "role": {"type": "relation"},
                    },
                    "role": {
                        "kind": "forward",
                        "field": "role",
                        "table": "business_roles",
                        "max_select": 1,
                        "relations": {
                            "__fields__": {
                                "permissions": {"type": "json"},
                            },
                        },
                    },
                },
            },
        },
        current_table="business",
    )

    assert params == {"_fp1": "user_1"}
    # Both operands share the back-relation row (one join, one EXISTS).
    (access,) = _aliases(sql, "business_access")
    (roles,) = _aliases(sql, "business_roles")
    assert sql.count("EXISTS (SELECT 1 FROM (SELECT 1)") == 1
    assert f'"{access}"."business" = "business"."id"' in sql
    assert f'"{access}"."user" = :_fp1' in sql
    assert (
        f'LEFT JOIN "business_roles" AS "{roles}" ON "{roles}"."id" = "{access}"."role"'
    ) in sql
    assert f'jsonb_extract_path("{roles}"."permissions", \'wallet\', \'read\')' in sql
