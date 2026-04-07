"""PPBase -- a Python reimplementation of PocketBase using PostgreSQL."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _resolve_version() -> str:
    try:
        return version("ppbase")
    except PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if pyproject_path.exists():
            try:
                import tomllib

                data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
                project = data.get("project", {})
                resolved = project.get("version")
                if isinstance(resolved, str) and resolved.strip():
                    return resolved.strip()
            except Exception:
                pass

    return "0.1.0"


__version__ = _resolve_version()

from ppbase.ext.flask_like_pb import FlaskLikePB


class PPBase(FlaskLikePB):
    """Main PPBase facade."""


# Process-wide Flask-like singleton facade.
pb = PPBase()

__all__ = ["PPBase", "pb", "__version__"]
