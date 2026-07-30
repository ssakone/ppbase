from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ppbase.backup import doctor
from ppbase.backup.doctor import doctor_human
from ppbase.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://runtime:SECRET@localhost/app",
        data_dir=str(tmp_path / "pb_data"),
    )


def test_doctor_human_output_is_stable() -> None:
    report = {
        "ready": False,
        "checks": [
            {"name": "storage", "ready": True, "detail": "backend=local"},
            {"name": "backup_root", "ready": False, "detail": "missing"},
        ],
    }
    assert doctor_human(report) == (
        "PPBase native backup doctor\n"
        "[OK] storage: backend=local\n"
        "[FAIL] backup_root: missing\n"
        "Backup is not ready; fix the backup failures above."
    )


def test_doctor_backup_root_check_matches_runtime_layout(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    missing = doctor._backup_root_check(data_dir)
    assert missing["ready"] is True
    assert missing["status"] == "warn"

    data_dir.mkdir()
    (data_dir / "backups").mkdir()
    available = doctor._backup_root_check(data_dir)
    assert available["ready"] is True
    assert available["status"] == "pass"


def test_doctor_backup_root_check_rejects_symlinked_entry(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    outside = tmp_path / "outside"
    data_dir.mkdir()
    outside.mkdir()
    (data_dir / "backups").symlink_to(outside, target_is_directory=True)

    check = doctor._backup_root_check(data_dir)

    assert check["ready"] is False
    assert check["status"] == "fail"


@pytest.mark.asyncio
async def test_doctor_has_no_role_banner_or_setup_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)

    class Result:
        def __init__(self, *, row=None, rows=None):
            self._row = row
            self._rows = rows or []

        def mappings(self):
            return self

        def one(self):
            return self._row

        def all(self):
            return self._rows

    class Connection:
        async def execute(self, statement, _params=None):
            sql = str(statement)
            if "current_database() AS database" in sql:
                return Result(row={"database": "app", "version": 160004})
            if "active_role.rolsuper" in sql:
                return Result(
                    row={
                        "is_superuser": True,
                        "owns_database": True,
                        "owns_schema": True,
                    }
                )
            if "FROM pg_catalog.pg_extension" in sql:
                return Result(rows=[{"name": "plpgsql", "version": "1.0"}])
            return Result()

        async def rollback(self):
            return None

    class ConnectionContext:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return ConnectionContext()

        async def dispose(self):
            return None

    monkeypatch.setattr(doctor, "create_async_engine", lambda *_a, **_kw: Engine())
    monkeypatch.setattr(doctor, "can_self_restart", lambda: False)

    report = await doctor.backup_doctor(settings)

    assert report["backupReady"] is True
    assert report["restoreReady"] is True
    assert "warnings" not in report
    assert [check["name"] for check in report["checks"]] == [
        "storage",
        "backup_root",
        "restart",
        "runtime_database",
        "extensions",
    ]
    assert "provision" not in str(report).lower()


def test_cli_exposes_doctor_but_not_init_or_provision() -> None:
    root_help = subprocess.run(
        [sys.executable, "-m", "ppbase", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    backup_help = subprocess.run(
        [sys.executable, "-m", "ppbase", "backup", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "init" not in root_help.stdout
    assert "doctor" in backup_help.stdout
    assert "provision" not in backup_help.stdout


def test_backup_command_applies_db_and_dir_to_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ppbase.__main__ as cli

    captured: dict[str, Settings] = {}

    async def fake_doctor(settings, *, server_url=None):
        captured["settings"] = settings
        assert server_url is None
        return {
            "ready": True,
            "fullyVerified": True,
            "exitCode": 0,
            "checks": [],
        }

    monkeypatch.setattr(doctor, "backup_doctor", fake_doctor)
    args = SimpleNamespace(
        action="doctor",
        db="postgresql+asyncpg://runtime:SECRET@localhost/app",
        data_dir=str(tmp_path / "custom-data"),
        server=None,
        json=True,
    )

    cli._cmd_backup(args)

    assert captured["settings"].data_dir == str(tmp_path / "custom-data")
    assert "SECRET" not in capsys.readouterr().out
