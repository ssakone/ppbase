"""Unit tests of the PocketBase 0.22.34 expand helpers (no database)."""

from __future__ import annotations

from ppbase.services.expand_service import (
    _merge_expand,
    is_admin_auth,
    normalize_expands,
)

# ---------------------------------------------------------------------------


def test_normalize_expands_matches_pocketbase() -> None:
    # daos/record_expand.go l.269-302 (doc example and dot trimming).
    assert normalize_expands(["a.b.c", "a.b", "   test  ", "  ", "test"]) == [
        "a.b.c",
        "test",
    ]
    assert normalize_expands(["customer", "customer.pdv", ".user."]) == [
        "customer.pdv",
        "user",
    ]
    assert normalize_expands(["cust omer", "customer"]) == ["customer"]


def test_merge_expand_merges_lists_by_id() -> None:
    target = {"id": "a", "expand": {"x": {"id": "1"}}}
    _merge_expand(
        target, {"x": [{"id": "1", "expand": {"y": {"id": "9"}}}, {"id": "2"}]}
    )
    assert [item["id"] for item in target["expand"]["x"]] == ["1", "2"]
    assert target["expand"]["x"][0]["expand"]["y"]["id"] == "9"
    single = {"id": "b"}
    _merge_expand(single, {"z": {"id": "3"}})
    assert single["expand"] == {"z": {"id": "3"}}


def test_admin_detection_matches_the_records_api() -> None:
    assert is_admin_auth({"type": "admin"})
    assert is_admin_auth({"type": "authRecord", "collectionName": "_superusers"})
    assert is_admin_auth({"type": "auth", "collectionName": "_superusers"})
    assert not is_admin_auth({"type": "authRecord", "collectionName": "users"})
    assert not is_admin_auth({})
    assert not is_admin_auth(None)
