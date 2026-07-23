from __future__ import annotations

from pathlib import Path

import pytest

import ppbase.backup.tools as postgres_tools
from ppbase.backup.tools import (
    PostgresToolResolutionError,
    resolve_postgres_tool,
    resolve_postgres_tools,
)


def test_resolve_postgres_tool_prefers_bundled_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundled = tmp_path / "pg_dump"
    bundled.write_text("binary", encoding="utf-8")
    bundled.chmod(0o700)
    monkeypatch.setattr(postgres_tools, "_bundled_tool_path", lambda _name: bundled)
    monkeypatch.setattr(
        postgres_tools.shutil,
        "which",
        lambda _name: pytest.fail("PATH fallback must not run"),
    )

    assert resolve_postgres_tool("pg_dump", "pg_dump") == str(bundled)


def test_resolve_postgres_tool_keeps_explicit_override(
    tmp_path: Path,
) -> None:
    override = tmp_path / "custom-pg-restore"
    override.write_text("binary", encoding="utf-8")
    override.chmod(0o700)

    assert resolve_postgres_tool("pg_restore", str(override)) == str(override)


def test_resolve_postgres_tool_explains_missing_wheel_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        postgres_tools,
        "_bundled_tool_path",
        lambda _name: tmp_path / "missing",
    )
    monkeypatch.setattr(postgres_tools.shutil, "which", lambda _name: None)

    with pytest.raises(PostgresToolResolutionError) as rejected:
        resolve_postgres_tool("psql")

    assert "reinstall a supported ppbase wheel" in str(rejected.value)
    assert "PPBASE_BACKUP_PSQL_PATH" in str(rejected.value)


def test_resolve_postgres_tools_uses_one_bundled_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for name in ("pg_dump", "pg_restore", "psql"):
        executable = bin_directory / name
        executable.write_text(name, encoding="utf-8")
        executable.chmod(0o700)
    monkeypatch.setattr(
        postgres_tools,
        "_bundled_tool_path",
        lambda name: bin_directory / name,
    )
    monkeypatch.setattr(
        postgres_tools.shutil,
        "which",
        lambda _name: pytest.fail("PATH fallback must not run"),
    )

    settings = type(
        "Settings",
        (),
        {
            "backup_pg_dump_path": "pg_dump",
            "backup_pg_restore_path": "pg_restore",
            "backup_psql_path": "psql",
        },
    )()
    resolved = resolve_postgres_tools(settings)

    assert resolved.pg_dump == str(bin_directory / "pg_dump")
    assert resolved.pg_restore == str(bin_directory / "pg_restore")
    assert resolved.psql == str(bin_directory / "psql")


def test_resolve_postgres_tool_rejects_partial_bundled_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = tmp_path / "actual-psql"
    actual.write_text("binary", encoding="utf-8")
    actual.chmod(0o700)
    bundled = tmp_path / "psql"
    bundled.symlink_to(actual)
    monkeypatch.setattr(postgres_tools, "_bundled_tool_path", lambda _name: bundled)
    monkeypatch.setattr(postgres_tools.shutil, "which", lambda _name: None)

    with pytest.raises(PostgresToolResolutionError):
        resolve_postgres_tool("psql")


def test_resolve_postgres_tool_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unsupported PostgreSQL client tool"):
        resolve_postgres_tool("postgres")
