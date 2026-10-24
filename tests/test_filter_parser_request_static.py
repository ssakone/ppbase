"""Unit tests for @request.* static comparisons (no database).

PocketBase binds an absent @request.* value as SQL NULL and resolves the
comparison with resolveEqualExpr; PPBase must emit PostgreSQL-safe SQL that
never casts an empty string to boolean or runs an untyped ``IS NOT NULL``.
See docs/request-static-fields.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ppbase.db.system_tables import CollectionRecord
from ppbase.services.filter_parser import parse_filter
from ppbase.services.record_service import _build_relation_index_from_collections

RULE_AUTH_ID = "@request.auth.id != '' && @request.auth.id != null"
RULE_PERM = (
    "(@request.auth.role.permission.isAdmin = true"
    " || @request.auth.role.permission.pdv.read = true)"
    " && @request.auth.agent = agent"
)


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


def _index():
    return _build_relation_index_from_collections(
        [
            _coll("buma_article_stockage", [{"name": "x", "type": "text"}]),
            _coll(
                "buma_pdv",
                [{"name": "agent", "type": "text"}, {"name": "name", "type": "text"}],
            ),
        ]
    )


def _sql(expr: str, auth: dict, table: str) -> tuple[str, dict]:
    return parse_filter(expr, {"auth": auth}, _index(), None, table)


def test_absent_request_auth_folds_to_constants_without_type_errors() -> None:
    # Anonymous: the whole @request.auth subtree is absent.  No ``:p IS NOT
    # NULL`` (untyped) and no ``'' = TRUE`` (type error) may appear.
    sql, _ = _sql(RULE_AUTH_ID, {}, "buma_article_stockage")
    assert sql == "(FALSE AND FALSE)"
    assert "IS NOT NULL" not in sql

    sql, _ = _sql(RULE_PERM, {}, "buma_pdv")
    # The permission clause folds to FALSE; the agent clause matches empty/NULL
    # agents like PocketBase (COALESCE), but the && short-circuits to no rows.
    assert (
        sql
        == "((FALSE OR FALSE) AND (COALESCE(\"buma_pdv\".\"agent\"::text, '') = ''))"
    )
    assert "= TRUE" not in sql


def test_present_request_auth_values_fold_like_sqlite() -> None:
    sql, params = _sql(
        RULE_PERM,
        {
            "id": "u1",
            "role.permission.isAdmin": False,
            "role.permission.pdv.read": True,
            "agent": "A1",
        },
        "buma_pdv",
    )
    # A known value compared with ``true`` folds to a constant (PocketBase:
    # ``value = 1`` in SQLite); the agent compares to the column directly.
    assert sql == '((FALSE OR TRUE) AND :_fp6 = "buma_pdv"."agent")'
    assert params["_fp6"] == "A1"


def test_present_auth_id_passes_the_stockage_rule() -> None:
    sql, _ = _sql(RULE_AUTH_ID, {"id": "u1"}, "buma_article_stockage")
    assert sql == "(TRUE AND TRUE)"


def test_admin_permission_key_present_true() -> None:
    sql, params = _sql(
        RULE_PERM,
        {"id": "a1", "role.permission.isAdmin": True, "agent": "A9"},
        "buma_pdv",
    )
    assert sql == '((TRUE OR FALSE) AND :_fp6 = "buma_pdv"."agent")'
    assert params["_fp6"] == "A9"


@pytest.mark.parametrize(
    ("value", "equal_true", "equal_false"),
    [
        # SQLite without affinity: numbers (and Go booleans, bound as 1/0)
        # compare numerically, a text never equals a number.
        (True, True, False),
        (False, False, True),
        (1, True, False),
        (1.0, True, False),
        (0, False, True),
        (2, False, False),
        ("true", False, False),
        ("1", False, False),
        ("", False, False),
        (None, False, False),
        ({"a": 1}, False, False),
    ],
)
def test_non_boolean_request_values_never_bind_against_booleans(
    value: object, equal_true: bool, equal_false: bool
) -> None:
    auth = {} if value is None else {"role.permission.pdv.read": value}
    for literal, expected in (("true", equal_true), ("false", equal_false)):
        sql, _ = _sql(
            f"@request.auth.role.permission.pdv.read = {literal}", auth, "buma_pdv"
        )
        assert sql == ("TRUE" if expected else "FALSE")
        sql, _ = _sql(
            f"@request.auth.role.permission.pdv.read != {literal}", auth, "buma_pdv"
        )
        assert sql == ("FALSE" if expected else "TRUE")


def test_absent_request_values_are_null_for_ordering_and_like() -> None:
    # PocketBase resolves an absent @request.* value to NULL: ``<``/``>``/``~``
    # never match, whereas a present '' is an empty string.
    sql, params = _sql("name < @request.query.nope", {}, "buma_pdv")
    assert sql == '"buma_pdv"."name" < :_fp1'
    assert params["_fp1"] is None
    sql, params = _sql("name ~ @request.query.nope", {}, "buma_pdv")
    assert params == {"_fp1": None}
    sql, _ = _sql("@request.query.nope > 5", {}, "buma_pdv")
    assert sql == "NULL"
    sql, _ = _sql("@request.auth.x > 5", {"x": "abc"}, "buma_pdv")
    assert sql == "TRUE"  # TEXT sorts after numbers in SQLite
    sql, _ = _sql("@request.auth.x <= 5", {"x": 5.0}, "buma_pdv")
    assert sql == "TRUE"
