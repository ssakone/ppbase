"""Resolve the PostgreSQL client tools shipped with PPBase wheels.

The public ``PPBASE_BACKUP_*_PATH`` settings remain supported as explicit
advanced overrides.  Without an override, an installed platform wheel uses
the private binaries bundled below ``ppbase/_vendor/postgresql``.  A PATH
fallback is intentionally kept for editable source checkouts and contributors.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil


POSTGRES_TOOL_NAMES = ("pg_dump", "pg_restore", "psql")


class PostgresToolResolutionError(RuntimeError):
    """Raised when a required PostgreSQL client executable is unavailable."""


@dataclass(frozen=True, slots=True)
class PostgresTools:
    pg_dump: str
    pg_restore: str
    psql: str


def _bundled_tool_path(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return Path(__file__).resolve().parents[1] / "_vendor" / "postgresql" / "bin" / (
        name + suffix
    )


def _usable_executable(path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and os.access(path, os.X_OK)


def resolve_postgres_tool(name: str, configured: str | None = None) -> str:
    """Return an absolute executable path for one PostgreSQL client tool."""
    if name not in POSTGRES_TOOL_NAMES:
        raise ValueError(f"unsupported PostgreSQL client tool: {name}")
    selected = str(configured or "").strip()
    default_name = name + (".exe" if os.name == "nt" else "")

    # Bare default names mean automatic resolution.  This preserves the old
    # settings defaults while making bundled wheel contents authoritative.
    if not selected or selected in {name, default_name}:
        bundled = _bundled_tool_path(name)
        if _usable_executable(bundled):
            return os.fspath(bundled)
        discovered = shutil.which(default_name) or shutil.which(name)
        if discovered:
            return os.path.abspath(discovered)
        raise PostgresToolResolutionError(
            f"{name} is unavailable; reinstall a supported ppbase wheel or set "
            f"PPBASE_BACKUP_{name.upper()}_PATH to an executable override"
        )

    candidate = Path(selected).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        candidate = candidate.resolve(strict=False)
        if _usable_executable(candidate):
            return os.fspath(candidate)
        raise PostgresToolResolutionError(
            f"configured {name} executable is missing or unsafe: {candidate}"
        )

    discovered = shutil.which(selected)
    if discovered:
        return os.path.abspath(discovered)
    raise PostgresToolResolutionError(
        f"configured {name} executable was not found on PATH: {selected}"
    )


def resolve_postgres_tools(settings: object) -> PostgresTools:
    """Resolve all tools used by native backup and destructive restore."""
    return PostgresTools(
        pg_dump=resolve_postgres_tool(
            "pg_dump",
            getattr(settings, "backup_pg_dump_path", None),
        ),
        pg_restore=resolve_postgres_tool(
            "pg_restore",
            getattr(settings, "backup_pg_restore_path", None),
        ),
        psql=resolve_postgres_tool(
            "psql",
            getattr(settings, "backup_psql_path", None),
        ),
    )
