"""Unit tests for hidden auth fields in filters (no database).

PocketBase 0.22.34 resolves only ``id``/``created``/``updated``, the schema
fields and, on an auth collection, ``username``/``verified``/
``emailVisibility``/``email`` (resolvers/record_field_resolve_runner.go
l.361-395, l.718-732).  A system field never takes a modifier, and a dotted
path must start with a relation, a back relation or a JSON field.  See
docs/hidden-auth-fields.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ppbase.db.system_tables import CollectionRecord
from ppbase.services.filter_parser import FilterFieldError, parse_filter
from ppbase.services.record_service import _build_relation_index_from_collections


def _coll(name: str, kind: str, schema: list[dict]) -> CollectionRecord:
    now = datetime.now(UTC)
    return CollectionRecord(
        id=f"{name}_id"[:15],
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


def _index(with_username: bool = False):
    users_schema = [
        {"name": "name", "type": "text"},
        {
            "name": "friend",
            "type": "relation",
            "options": {"collectionId": "hu_users_id", "maxSelect": 1},
        },
        {"name": "meta", "type": "json"},
    ]
    if with_username:
        users_schema.append({"name": "username", "type": "text"})
    return _build_relation_index_from_collections(
        [
            _coll("hu_users", "auth", users_schema),
            _coll(
                "hu_posts",
                "base",
                [
                    {"name": "name", "type": "text"},
                    {
                        "name": "author",
                        "type": "relation",
                        "options": {"collectionId": "hu_users_id", "maxSelect": 1},
                    },
                ],
            ),
        ]
    )


def _sql(expr: str, table: str = "hu_users", *, allow_hidden: bool = False, **kw):
    index = _index(**kw)
    return parse_filter(
        expr, {"auth": {}}, index, index, table, allow_hidden=allow_hidden
    )


@pytest.mark.parametrize(
    "expr",
    [
        # Table-qualified self references must never reach the raw column.
        'hu_users.password_hash ~ "$2"',
        'hu_users.token_key != ""',
        'hu_users.email = "x@example.invalid"',
        'hu_users.name = "x"',
        # Another table, or a scalar used as a relation: unresolved paths.
        'hu_posts.name != ""',
        "password_hash.x = 1",
        'name.x = "a"',
        # Modifiers on system fields are unknown fields in 0.22.34.
        'email:lower = "x@example.invalid"',
        'email:lower ~ "x"',
        "email:length > 3",
        "email:isset = true",
        'email:each = "x"',
        "emailVisibility:isset = true",
        "verified:length > 0",
        "id:length > 0",
        "created:isset = true",
        'friend.email:lower = "x"',
        "friend.email:length > 3",
        "friend.token_key:length > 0",
        # Hidden columns, with or without a modifier.
        "token_key:length > 0",
        'tokenKey != ""',
        'password != ""',
        'lastResetSentAt = ""',
    ],
)
def test_unresolvable_paths_are_filter_field_errors(expr: str) -> None:
    for allow_hidden in (False, True):
        with pytest.raises(FilterFieldError):
            _sql(expr, allow_hidden=allow_hidden)


def test_relation_email_modifier_from_another_collection_is_rejected() -> None:
    with pytest.raises(FilterFieldError):
        _sql('author.email:lower = "x"', "hu_posts")
    with pytest.raises(FilterFieldError):
        _sql("@collection.hu_users.email:length > 3", "hu_posts", allow_hidden=True)


def test_email_keeps_its_visibility_guard() -> None:
    sql, _ = _sql('email = "x@example.invalid"')
    assert '"hu_users"."email_visibility" = TRUE' in sql
    sql, _ = _sql('email = "x@example.invalid"', allow_hidden=True)
    assert "email_visibility" not in sql


def test_schema_field_modifiers_and_json_paths_still_resolve() -> None:
    sql, _ = _sql("name:length > 1")
    assert "char_length" in sql
    sql, _ = _sql('meta.a = "b"')
    assert "meta" in sql


def test_username_resolves_only_with_its_schema_column() -> None:
    with pytest.raises(FilterFieldError):
        _sql('username = "a"')
    sql, _ = _sql('username = "a"', with_username=True)
    assert '"hu_users"."username"' in sql


def test_parser_without_metadata_keeps_the_legacy_raw_path() -> None:
    sql, _ = parse_filter('other.col = "x"')
    assert "other.col" in sql
