"""Unit checks of the PocketBase multi-match rules (SQL shape only).

Behavior against PostgreSQL and the official binary is covered by
``tests/integration/test_multi_match_rules.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ppbase.db.system_tables import CollectionRecord
from ppbase.services.filter_parser import parse_filter
from ppbase.services.record_service import (
    _build_relation_index_from_collections,
    _has_single_column_unique_index,
)


def _coll(
    name: str, schema: list[dict], indexes: list[str] | None = None
) -> CollectionRecord:
    now = datetime.now(UTC)
    return CollectionRecord(
        id=f"{name}_id"[:15],
        name=name,
        type="base",
        system=False,
        schema=schema,
        indexes=indexes or [],
        list_rule="",
        view_rule="",
        create_rule="",
        update_rule="",
        delete_rule="",
        options={},
        created=now,
        updated=now,
    )


def _rel(name: str, target: str, max_select: int | None) -> dict:
    return {
        "name": name,
        "type": "relation",
        "options": {"collectionId": f"{target}_id"[:15], "maxSelect": max_select},
    }


def _index() -> dict:
    targets = _coll("targets", [{"name": "name", "type": "text"}])
    owners = _coll(
        "owners",
        [
            {"name": "title", "type": "text"},
            _rel("rels", "targets", None),
            _rel("one", "targets", 1),
        ],
    )
    links = _coll("links", [_rel("owner", "owners", 1)])
    ulinks = _coll(
        "ulinks",
        [_rel("owner", "owners", 1)],
        ["CREATE UNIQUE INDEX `idx_u` ON `ulinks` (`owner`)"],
    )
    return _build_relation_index_from_collections([targets, owners, links, ulinks])


@pytest.mark.parametrize(
    ("indexes", "expected"),
    [
        (["CREATE UNIQUE INDEX `idx` ON `t` (`owner`)"], True),
        (['CREATE UNIQUE INDEX "idx" ON "t" ("owner" DESC)'], True),
        (
            ["create unique index idx on t (Owner collate nocase) where owner != ''"],
            True,
        ),
        (["CREATE INDEX idx ON t (owner)"], False),
        (["CREATE UNIQUE INDEX idx ON t (owner, title)"], False),
        (["CREATE UNIQUE INDEX idx ON t (lower(owner))"], False),
        ([], False),
    ],
)
def test_single_column_unique_index_like_pocketbase(
    indexes: list[str], expected: bool
) -> None:
    assert _has_single_column_unique_index("owner", indexes) is expected


def test_back_relations_record_their_unique_index() -> None:
    back = _index()["back_relations"]["owners"]
    assert back["links_via_owner"]["unique"] is False
    assert back["ulinks_via_owner"]["unique"] is True


@pytest.mark.parametrize(
    ("expression", "table", "multi_match"),
    [
        ('rels.name = "a"', "owners", True),
        ('rels.name ?= "a"', "owners", False),
        ('one.name = "a"', "owners", False),
        ('links_via_owner.owner = "a"', "owners", True),
        ('ulinks_via_owner.owner = "a"', "owners", False),
        ('@collection.targets.name = "a"', "owners", True),
        ('@collection.targets.name ?!= "a"', "owners", False),
        ('owner.rels.name != "a"', "links", True),
    ],
)
def test_multi_match_only_without_question_mark(
    expression: str, table: str, multi_match: bool
) -> None:
    index = _index()
    sql, _params = parse_filter(expression, {}, index, index, current_table=table)
    assert ("NOT EXISTS" in sql) is multi_match


def test_a_null_row_fails_every_operator_but_equal() -> None:
    index = _index()
    equal, _params = parse_filter('rels.name = "a"', {}, index, current_table="owners")
    not_equal, _params = parse_filter(
        'rels.name != "a"', {}, index, current_table="owners"
    )
    assert '"v" IS NULL' not in equal
    assert 'OR "__pbsm' in not_equal and '"v" IS NULL' in not_equal


def test_or_operands_get_their_own_join_group() -> None:
    index = _index()
    sql, _params = parse_filter(
        'rels.name ?= "a" || one.name = "b"', {}, index, current_table="owners"
    )
    assert sql.count('"owners"."id" IN (SELECT') == 2
    sql, _params = parse_filter(
        'rels.name ?= "a" && rels.name ?= "b"', {}, index, current_table="owners"
    )
    # Same path prefix: one join, one group (the same related row).
    assert sql.count('"owners"."id" IN (SELECT') == 1
    assert sql.count("LEFT JOIN LATERAL unnest") == 1
