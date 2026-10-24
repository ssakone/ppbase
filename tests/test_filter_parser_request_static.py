"""Unit tests for @request.* static comparisons (no database).

PocketBase binds an absent @request.* value as SQL NULL and resolves the
comparison with resolveEqualExpr; PPBase must emit PostgreSQL-safe SQL that
never casts an empty string to boolean or runs an untyped ``IS NOT NULL``.
See docs/request-static-fields.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

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


def test_present_request_auth_binds_typed_params() -> None:
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
    # Booleans bind as booleans (``:p = TRUE``); the agent compares to the
    # column directly.
    assert sql == '((:_fp2 = TRUE OR :_fp4 = TRUE) AND :_fp6 = "buma_pdv"."agent")'
    assert params["_fp2"] is False
    assert params["_fp4"] is True
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
    assert sql == '((:_fp2 = TRUE OR FALSE) AND :_fp6 = "buma_pdv"."agent")'
    assert params["_fp2"] is True
