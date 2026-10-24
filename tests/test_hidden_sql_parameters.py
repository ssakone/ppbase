"""Every SQLAlchemy engine of PPBase hides bound parameters from errors and logs."""

from __future__ import annotations

import ast
from pathlib import Path

PPBASE_ROOT = Path(__file__).resolve().parents[1] / "ppbase"


def test_every_sqlalchemy_engine_hides_bound_parameters() -> None:
    """Bound values (hashes, token keys, secrets) never reach error messages."""
    calls = []
    for path in sorted(PPBASE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in {"create_async_engine", "create_engine"}:
                continue
            hide = [kw for kw in node.keywords if kw.arg == "hide_parameters"]
            calls.append(
                (
                    f"{path.relative_to(PPBASE_ROOT.parent)}:{node.lineno}",
                    bool(hide)
                    and isinstance(hide[0].value, ast.Constant)
                    and hide[0].value.value is True,
                )
            )
    assert len(calls) >= 3, calls
    assert [location for location, hidden in calls if not hidden] == []
