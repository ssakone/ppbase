from __future__ import annotations

from ppbase.services.filter_parser import parse_filter


def test_collection_reference_simple_field_still_groups_by_alias() -> None:
    sql, params = parse_filter(
        (
            '@collection.refs:first.linkKey = itemKey && '
            '@collection.refs:second.tag = "b"'
        ),
        current_table="items",
    )

    assert params == {"_fp1": "b"}
    assert 'EXISTS (SELECT 1 FROM "refs" WHERE "refs"."linkKey" = "items"."itemKey")' in sql
    assert 'EXISTS (SELECT 1 FROM "refs" WHERE "refs"."tag" = :_fp1)' in sql


def test_relation_traversal_still_qualifies_current_table() -> None:
    sql, params = parse_filter(
        "owner.id = @request.auth.id",
        {"auth": {"id": "user_1"}},
        {"owner": {"table": "users", "max_select": 1, "relations": {}}},
        current_table="business",
    )

    assert params == {"_fp1": "user_1"}
    assert '"users"."id" = "business"."owner"' in sql
    assert '"users"."id" = :_fp1' in sql


def test_any_equals_is_scalar_safe_for_current_collection_fields() -> None:
    sql, params = parse_filter('tags ?= "vip"', current_table="posts")

    assert params == {"_fp1": "vip"}
    assert "jsonb_array_elements_text" in sql
    assert "ANY(" not in sql
    assert '"posts"."tags"' in sql


def test_collection_relation_id_uses_stored_relation_column() -> None:
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
    assert '"business_access"."business"' in sql
    assert '"business"."id"::text' in sql
    assert 'FROM "business_access"' in sql
    assert 'FROM "business" WHERE' not in sql
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
    assert 'EXISTS (SELECT 1 FROM "business_access"' in sql
    assert 'EXISTS (SELECT 1 FROM "business" WHERE "business"."id" = "business_access"."business"' in sql
    assert '"business"."owner"' in sql
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
    assert 'EXISTS (SELECT 1 FROM "business_access"' in sql
    assert '"business_access"."business" = "business"."id"' in sql
    assert '"business_access"."user"' in sql
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
    assert 'EXISTS (SELECT 1 FROM "business" WHERE "business"."id" = "sessions"."account"' in sql
    assert 'EXISTS (SELECT 1 FROM "business_access" WHERE "business_access"."business" = "business"."id"' in sql
    assert '"business_access"."user"' in sql


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
    assert sql.count('EXISTS (SELECT 1 FROM "business_access"') == 1
    assert '"business_access"."business" = "business"."id"' in sql
    assert '"business_access"."user"' in sql
    assert 'EXISTS (SELECT 1 FROM "business_roles" WHERE "business_roles"."id" = "business_access"."role"' in sql
    assert 'jsonb_extract_path("business_roles"."permissions", \'wallet\', \'read\')' in sql
