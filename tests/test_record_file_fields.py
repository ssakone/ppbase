"""Unit tests of the PocketBase file field write semantics (no database)."""

from __future__ import annotations

from types import SimpleNamespace

from ppbase.services.record_file_fields import (
    CURRENT,
    POCKETBASE_022,
    UNKNOWN_FILENAMES,
    Upload,
    file_value,
    is_touched,
    plan_file_field,
)

SINGLE = SimpleNamespace(name="doc", options={"maxSelect": 1})
MULTI = SimpleNamespace(name="docs", options={"maxSelect": 4})
OLD = ["a.txt", "b.txt", "c.txt"]


def _plan(semantics, field, old, data, uploads=None):
    return plan_file_field(semantics, field, old, data, uploads or {})


def test_both_semantics_refuse_names_the_record_does_not_have() -> None:
    for name in ("x.txt", "a.txt.attrs", "../a.txt", "other/a.txt"):
        plan = _plan(CURRENT, MULTI, OLD, {"docs": ["a.txt", name]})
        assert plan.error == {
            "code": "validation_invalid_file",
            "message": f"Invalid new files: [{name}].",
            "params": {"invalidFiles": [name]},
        }
        plan = _plan(POCKETBASE_022, MULTI, OLD, {"docs": ["a.txt", name]})
        assert plan.error == UNKNOWN_FILENAMES and plan.load_error
    # A new record has no file: any name is refused.
    assert _plan(CURRENT, SINGLE, None, {"doc": "a.txt"}).error is not None
    assert _plan(POCKETBASE_022, SINGLE, "", {"doc": "a.txt"}).load_error


def test_known_names_can_be_kept_removed_and_reordered() -> None:
    for semantics in (CURRENT, POCKETBASE_022):
        plan = _plan(semantics, MULTI, OLD, {"docs": ["c.txt", "a.txt", "a.txt"]})
        assert plan.error is None and plan.entries == ["c.txt", "a.txt"]
        # A single field keeps the last name only.
        plan = _plan(semantics, SINGLE, "a.txt", {"doc": ["x.txt", "a.txt"]})
        assert plan.error is None and file_value(plan, {}) == "a.txt"
        assert _plan(semantics, SINGLE, "a.txt", {"doc": ["a.txt", "x"]}).error
        plan = _plan(semantics, MULTI, OLD, {"docs-": ["b.txt", "zzz"]})
        assert plan.entries == ["a.txt", "c.txt"]
        assert _plan(semantics, MULTI, OLD, {"docs": None}).entries == []


def test_modifiers_and_uploads_differ_between_versions() -> None:
    data = {"docs+": ["x.txt"]}
    assert _plan(CURRENT, MULTI, OLD, data).error is not None
    assert _plan(POCKETBASE_022, MULTI, OLD, data).entries == OLD  # no-op
    assert _plan(POCKETBASE_022, MULTI, OLD, {"+docs": ["x.txt"]}).entries == OLD

    uploads = {"docs": 1}
    assert _plan(CURRENT, MULTI, OLD, {}, uploads).entries == [Upload("docs", 0)]
    assert _plan(POCKETBASE_022, MULTI, OLD, {}, uploads).entries == [
        *OLD,
        Upload("docs", 0),
    ]
    plan = _plan(CURRENT, MULTI, OLD, {"docs": "b.txt"}, uploads)
    assert plan.entries == ["b.txt", Upload("docs", 0)]
    plan = _plan(CURRENT, MULTI, OLD, {}, {"+docs": 1})
    assert plan.entries == [Upload("+docs", 0), *OLD]
    assert _plan(POCKETBASE_022, MULTI, OLD, {}, {"docs+": 1}).entries == OLD

    assert _plan(CURRENT, SINGLE, "a.txt", {}, {"doc": 2}).entries == [Upload("doc", 1)]
    assert _plan(POCKETBASE_022, SINGLE, "a.txt", {}, {"doc": 2}).entries == [
        Upload("doc", 0)
    ]


def test_too_many_files() -> None:
    plan = _plan(CURRENT, MULTI, OLD, {}, {"docs+": 2})
    assert plan.error == {
        "code": "validation_too_many_files",
        "message": "The maximum allowed files is 4.",
        "params": {"maxSelect": 4},
    }
    plan = _plan(POCKETBASE_022, MULTI, OLD, {}, {"docs": 2})
    assert plan.error == {
        "code": "validation_too_many_values",
        "message": "Select no more than 4.",
    }
    assert not plan.load_error


def test_legacy_022_deletions() -> None:
    plan = _plan(POCKETBASE_022, MULTI, OLD, {"docs.b.txt": "", "docs.0": None})
    assert plan.entries == ["c.txt"]
    # A non-empty value, an unknown name or an out-of-range index is ignored.
    data = {"docs.a.txt": "keep", "docs.x.txt": "", "docs.7": "", "docs.01": False}
    assert _plan(POCKETBASE_022, MULTI, OLD, data).entries == OLD
    # Not a modifier in the current semantics.
    assert _plan(CURRENT, MULTI, OLD, {"docs.b.txt": ""}).entries == OLD


def test_invalid_names_are_cut_like_go() -> None:
    name = "x" + "é" * 200  # 401 bytes; byte 250 starts a 2-byte character
    plan = _plan(CURRENT, MULTI, OLD, {"docs": [name]})
    (invalid,) = plan.error["params"]["invalidFiles"]
    assert invalid == "x" + "é" * 124 + "\ufffd"
    assert plan.error["message"] == f"Invalid new files: [{invalid}]."


def test_touched_fields() -> None:
    assert not is_touched("docs", {"doc": "a"}, {})
    for data, uploads in (
        ({"docs": []}, {}),
        ({"docs-": "a"}, {}),
        ({"+docs": "a"}, {}),
        ({"docs.0": ""}, {}),
        ({}, {"docs+": 1}),
    ):
        assert is_touched("docs", data, uploads)
