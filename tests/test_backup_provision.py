from __future__ import annotations

import importlib.metadata
import json
import subprocess
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ppbase.backup import provision
from ppbase.backup.provision import (
    BackupProvisionError,
    build_postgres_init_plan,
    doctor_human,
    ensure_postgres_init_layout,
    resolve_postgres_init_spec,
    write_secret_sink,
)
from ppbase.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://runtime:RUNTIME_SECRET@localhost/app",
        data_dir=str(tmp_path / "data"),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        backup_target_root=str(tmp_path / "targets"),
    )


def test_legacy_restore_environment_settings_still_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPBASE_BACKUP_STAGING_ROOT", "/legacy/staging")
    monkeypatch.setenv("PPBASE_BACKUP_TARGET_ROOT", "/legacy/targets")
    monkeypatch.setenv(
        "PPBASE_BACKUP_CREATOR_DATABASE_URL",
        "postgresql+asyncpg://creator@localhost/postgres",
    )
    monkeypatch.setenv(
        "PPBASE_BACKUP_RESTORE_DATABASE_URL",
        "postgresql+asyncpg://restore@localhost/app",
    )
    monkeypatch.setenv("PPBASE_BACKUP_TARGET_OWNER", "owner")

    settings = Settings(_env_file=None)

    assert settings.backup_staging_root == "/legacy/staging"
    assert settings.backup_target_root == "/legacy/targets"
    assert settings.backup_creator_database_url.endswith("/postgres")
    assert settings.backup_restore_database_url.endswith("/app")
    assert settings.backup_target_owner == "owner"


def test_secret_sink_is_exclusive_0600_and_contains_only_limited_credentials(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "private" / "backup.env"
    written = write_secret_sink(
        destination,
        {
            "PPBASE_BACKUP_DUMP_DATABASE_URL": "postgresql://dump:DUMP@db/app",
        },
    )

    assert written == destination
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    payload = destination.read_text(encoding="utf-8")
    assert "DUMP" in payload
    assert "BOOTSTRAP" not in payload
    with pytest.raises(BackupProvisionError, match="already exists"):
        write_secret_sink(destination, {"A": "B"})


def test_secret_sink_publish_never_replaces_a_racing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "private" / "backup.env"
    real_link = provision.os.link

    def publish_after_racer(source, target, **kwargs):
        descriptor = provision.os.open(
            target,
            provision.os.O_WRONLY | provision.os.O_CREAT | provision.os.O_EXCL,
            0o600,
            dir_fd=kwargs["dst_dir_fd"],
        )
        try:
            provision.os.write(descriptor, b"FOREIGN=\"preserve-me\"\n")
            provision.os.fsync(descriptor)
        finally:
            provision.os.close(descriptor)
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(provision.os, "link", publish_after_racer)
    with pytest.raises(BackupProvisionError, match="overwrite is forbidden"):
        write_secret_sink(destination, {"A": "B"})
    assert destination.read_text(encoding="utf-8") == 'FOREIGN="preserve-me"\n'


def test_secret_sink_recovers_interrupted_hardlink_publication(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "private" / "backup.env"
    write_secret_sink(destination, {"LIMITED": "credential"})
    interrupted_temp = destination.parent / ".backup.env.0123456789abcdef.tmp"
    interrupted_temp.hardlink_to(destination)
    assert destination.stat().st_nlink == 2

    assert provision.read_secret_sink(destination) == {"LIMITED": "credential"}
    assert destination.stat().st_nlink == 1
    assert not interrupted_temp.exists()


@pytest.mark.asyncio
async def test_absent_runtime_role_is_created_without_cluster_privileges() -> None:
    statements: list[str] = []

    class Result:
        def mappings(self):
            return self

        def one_or_none(self):
            return None

    class Connection:
        async def execute(self, statement, _params=None):
            statements.append(str(statement))
            return Result()

    created = await provision._ensure_role(
        Connection(),
        "ledger",
        {"login": True, "createdb": False, "inherit": True},
        password="RUNTIME_SECRET",
    )

    assert created is True
    create_sql = next(sql for sql in statements if sql.startswith("CREATE ROLE"))
    for clause in (
        "LOGIN",
        "NOCREATEDB",
        "INHERIT",
        "NOSUPERUSER",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ):
        assert clause in create_sql
    assert " CREATEDB" not in create_sql
    assert " SUPERUSER" not in create_sql
    assert " CREATEROLE" not in create_sql
    assert " REPLICATION" not in create_sql
    assert " BYPASSRLS" not in create_sql
    assert "backup_dump" not in create_sql
    assert "backup_creator" not in create_sql
    assert "backup_restore" not in create_sql
    assert "backup_owner" not in create_sql


def test_doctor_human_output_is_stable() -> None:
    report = {
        "ready": False,
        "checks": [
            {"name": "storage", "ready": True, "detail": "backend=local"},
            {"name": "dump_role", "ready": False, "detail": "missing"},
        ],
    }
    assert doctor_human(report) == (
        "PPBase native backup doctor\n"
        "[OK] storage: backend=local\n"
        "[FAIL] dump_role: missing\n"
        "Backup is not ready; fix the backup failures above."
    )


def test_doctor_human_reports_non_blocking_runtime_security_warning() -> None:
    report = {
        "ready": True,
        "fullyVerified": False,
        "warnings": [
            {
                "code": "runtime_superuser",
                "detail": "PostgreSQL superuser runtime",
                "role": "runtime",
            }
        ],
        "checks": [
            {
                "name": "runtime_role",
                "ready": True,
                "status": "warn",
                "code": "runtime_superuser",
                "detail": "PostgreSQL superuser runtime",
            }
        ],
    }
    assert doctor_human(report) == (
        "PPBase native backup doctor\n"
        "[WARN] runtime_role: PostgreSQL superuser runtime\n"
        "Backup and destructive restore are ready with security warnings."
    )


@pytest.mark.asyncio
async def test_doctor_reports_restore_blocker_without_blocking_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    for name in ("backups", "control"):
        (tmp_path / name).mkdir(mode=0o700)

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
            if "AS version" in sql:
                return Result(
                    row={
                        "role": "runtime",
                        "database": "app",
                        "server_address": "127.0.0.1",
                        "server_port": 5432,
                        "postmaster_started_at": "1000.0",
                        "version": 160004,
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

    async def restore_preflight(_connection):
        return SimpleNamespace(
            ok=False,
            errors=("runtime role must own the active PostgreSQL database",),
            warnings=(),
        )

    monkeypatch.setattr(
        provision,
        "create_async_engine",
        lambda *_a, **_kw: Engine(),
    )
    monkeypatch.setattr(
        provision,
        "preflight_destructive_restore_role",
        restore_preflight,
    )
    monkeypatch.setattr(provision, "can_self_restart", lambda: False)

    report = await provision.backup_doctor(settings)

    assert report["ready"] is True
    assert report["backupReady"] is True
    assert report["restoreReady"] is False
    assert report["exitCode"] == 0
    assert [
        check["name"]
        for check in report["checks"]
        if check["name"] == "runtime_database"
    ] == ["runtime_database"]
    assert doctor_human(report).endswith(
        "Backup is ready; destructive restore is not ready. "
        "Fix the restore-specific failures above."
    )


def test_postgres_init_name_contract_is_deterministic_and_bounded() -> None:
    spec = resolve_postgres_init_spec("ledger")
    assert spec.database == "ledger"
    assert spec.runtime_role == "ledger"
    assert spec.as_dict()["roles"] == {"runtime": "ledger"}
    with pytest.raises(BackupProvisionError, match="reserved"):
        resolve_postgres_init_spec("postgres")
    with pytest.raises(BackupProvisionError, match="at most 48"):
        resolve_postgres_init_spec("a" * 49)


def test_postgres_init_secret_contains_only_runtime_database_url() -> None:
    spec = resolve_postgres_init_spec("ledger")
    values = provision._postgres_init_values(
        "postgresql+asyncpg://clusteradmin:BOOTSTRAP@localhost/postgres",
        spec,
        "RUNTIME_SECRET",
    )

    assert set(values) == {"PPBASE_DATABASE_URL"}
    assert "ledger:RUNTIME_SECRET" in values["PPBASE_DATABASE_URL"]
    assert not any(key.startswith("PPBASE_BACKUP_") for key in values)


def test_postgres_init_layout_creates_private_defaults_idempotently(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first_layout, first_created = ensure_postgres_init_layout(settings)
    first_inodes = {
        name: Path(path).stat().st_ino
        for name, path in first_layout.as_dict().items()
    }
    assert set(first_layout.as_dict()) == {"dataDir", "backupRoot", "controlDir"}
    assert set(first_created) == set(first_layout.as_dict())
    for path in first_layout.as_dict().values():
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o700

    second_layout, second_created = ensure_postgres_init_layout(settings)
    assert second_created == []
    assert {
        name: Path(path).stat().st_ino
        for name, path in second_layout.as_dict().items()
    } == first_inodes
    assert not Path(settings.backup_staging_root).exists()
    assert not Path(settings.backup_target_root).exists()


def test_doctor_root_check_matches_runtime_backup_root_policy(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private_check = provision._root_check(private, label="private")
    assert private_check["ready"] is True
    assert private_check["status"] == "pass"

    non_private = tmp_path / "non-private"
    non_private.mkdir(mode=0o755)
    assert provision._root_check(non_private, label="non_private")["ready"] is False

    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    assert provision._root_check(link, label="link")["ready"] is False

    missing = provision._root_check(tmp_path / "missing", label="missing")
    assert missing["ready"] is True
    assert missing["status"] == "warn"
    assert not (tmp_path / "missing").exists()
    assert provision._root_check(Path("/"), label="root")["ready"] is False


def test_doctor_root_check_refuses_missing_root_under_unwritable_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_access = provision.os.access

    def access(path, mode):
        if Path(path) == tmp_path:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(provision.os, "access", access)

    check = provision._root_check(tmp_path / "missing", label="missing")

    assert check["ready"] is False


def test_doctor_root_check_refuses_symlinked_and_writable_ancestry(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    linked_check = provision._root_check(
        linked / "backups",
        label="linked",
    )

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    writable_check = provision._root_check(
        unsafe / "backups",
        label="writable",
    )

    assert linked_check["ready"] is False
    assert writable_check["ready"] is False
    assert not (outside / "backups").exists()
    assert not (unsafe / "backups").exists()


def test_doctor_root_layout_rejects_data_and_backup_overlap(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"backup_root": str(tmp_path / "data" / "backups")}
    )

    check = provision._root_layout_check(settings)

    assert check["ready"] is False
    assert "backup_root overlaps data_dir" in check["detail"]


@pytest.mark.asyncio
async def test_postgres_init_plan_is_read_only_and_redacts_bootstrap_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Result:
        def __init__(self, *, one=None, rows=None):
            self._one = one
            self._rows = rows or []

        def mappings(self):
            return self

        def one(self):
            return self._one

        def all(self):
            return self._rows

        def one_or_none(self):
            return self._one

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def begin(self):
            return Transaction()

        async def execute(self, statement, _params=None):
            sql = str(statement)
            statements.append(sql)
            if "r.rolsuper, r.rolcreatedb" in sql:
                return Result(
                    one={
                        "role": "clusteradmin",
                        "database": "postgres",
                        "server_version_num": 160004,
                        "rolsuper": True,
                        "rolcreatedb": True,
                        "rolcreaterole": True,
                    }
                )
            return Result(rows=[])

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

    monkeypatch.setattr(provision, "create_async_engine", lambda *_a, **_kw: Engine())
    settings = _settings(tmp_path)
    plan = await build_postgres_init_plan(
        settings,
        bootstrap_database_url=(
            "postgresql+asyncpg://clusteradmin:BOOTSTRAP_SECRET@localhost/postgres"
        ),
        project_name="ledger",
        secret_sink=tmp_path / "ledger.env",
    )

    assert plan["readOnly"] is True
    assert plan["executable"] is True
    assert "BOOTSTRAP_SECRET" not in json.dumps(plan)
    assert plan["project"]["roles"] == {"runtime": "ledger"}
    assert "backup_dump" not in json.dumps(plan)
    assert "backup_creator" not in json.dumps(plan)
    assert "backup_restore" not in json.dumps(plan)
    assert "backup_owner" not in json.dumps(plan)
    assert not any(Path(path).exists() for path in plan["directories"].values())
    assert not any(
        token in statement.upper()
        for statement in statements
        for token in ("CREATE ROLE", "CREATE DATABASE", "GRANT ", "REVOKE ")
    )


@pytest.mark.asyncio
async def test_postgres_init_plan_rejects_createdb_createrole_without_superuser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, *, one=None, rows=None):
            self._one = one
            self._rows = rows or []

        def mappings(self):
            return self

        def one(self):
            return self._one

        def one_or_none(self):
            return self._one

        def all(self):
            return self._rows

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def begin(self):
            return Transaction()

        async def execute(self, statement, _params=None):
            if "r.rolsuper, r.rolcreatedb" in str(statement):
                return Result(
                    one={
                        "role": "delegated_admin",
                        "database": "postgres",
                        "server_version_num": 160004,
                        "rolsuper": False,
                        "rolcreatedb": True,
                        "rolcreaterole": True,
                    }
                )
            return Result(rows=[])

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

    monkeypatch.setattr(provision, "create_async_engine", lambda *_a, **_kw: Engine())

    plan = await build_postgres_init_plan(
        _settings(tmp_path),
        bootstrap_database_url=(
            "postgresql+asyncpg://delegated_admin:SECRET@localhost/postgres"
        ),
        project_name="ledger",
        secret_sink=tmp_path / "ledger.env",
    )

    assert plan["executable"] is False
    assert plan["collisions"] == [
        {
            "resource": "bootstrap_role",
            "reason": "init postgres requires an ephemeral PostgreSQL superuser DSN",
        }
    ]


def test_doctor_human_renders_partial_restart_checks() -> None:
    report = {
        "ready": True,
        "fullyVerified": False,
        "checks": [
            {
                "name": "restart",
                "ready": True,
                "status": "skip",
                "detail": "not observable",
            },
            {
                "name": "server",
                "ready": True,
                "status": "warn",
                "detail": "inconclusive",
            },
        ],
    }
    assert doctor_human(report) == (
        "PPBase native backup doctor\n"
        "[SKIP] restart: not observable\n"
        "[WARN] server: inconclusive\n"
        "No confirmed blocker; readiness is partial."
    )


@pytest.mark.parametrize(
    ("configured", "status", "ready"),
    ((True, "pass", True), (False, "fail", False)),
)
def test_server_restart_probe_is_authoritative_only_for_explicit_boolean(
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
    status: str,
    ready: bool,
) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(
                {
                    "code": 200,
                    "message": "Backup restart capability inspected.",
                    "data": {"restart": {"configured": configured}},
                }
            ).encode()

        def geturl(self):
            return "http://127.0.0.1:8090/api/health/backup-restart"

    monkeypatch.setattr(provision.urllib.request, "urlopen", lambda *_a, **_kw: Response())
    check = provision._probe_server_restart("http://127.0.0.1:8090")
    assert check["status"] == status
    assert check["ready"] is ready


@pytest.mark.parametrize("mode", ("unreachable", "invalid_json"))
def test_server_restart_probe_fails_when_live_server_is_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    if mode == "unreachable":
        def fail(*_args, **_kwargs):
            raise provision.urllib.error.URLError("connection refused")

        monkeypatch.setattr(provision.urllib.request, "urlopen", fail)
    else:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b"not-json"

            def geturl(self):
                return "http://127.0.0.1:9/api/health/backup-restart"

        monkeypatch.setattr(
            provision.urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: Response(),
        )

    check = provision._probe_server_restart("http://127.0.0.1:9")
    assert check["status"] == "fail"
    assert check["ready"] is False


@pytest.mark.parametrize(
    "mode",
    ("redirect", "truncated", "missing_envelope"),
)
def test_server_restart_probe_rejects_non_authoritative_http(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            if mode == "redirect":
                return "http://other.test/api/health/backup-restart"
            return "http://127.0.0.1:8090/api/health/backup-restart"

        def read(self, _limit):
            if mode == "truncated":
                raise provision.http.client.IncompleteRead(b"{")
            if mode == "missing_envelope":
                return json.dumps(
                    {"data": {"restart": {"configured": True}}}
                ).encode()
            return json.dumps(
                {
                    "code": 200,
                    "message": "Backup restart capability inspected.",
                    "data": {"restart": {"configured": True}},
                }
            ).encode()

    monkeypatch.setattr(
        provision.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    check = provision._probe_server_restart("http://127.0.0.1:8090")
    assert check["status"] == "fail"
    assert check["ready"] is False


def test_installed_console_script_matches_python_module_help() -> None:
    entry_points = {
        item.name: item.value
        for item in importlib.metadata.entry_points(group="console_scripts")
    }
    assert entry_points["ppbase"] == "ppbase.__main__:main"
    script = Path(sys.executable).with_name("ppbase")
    module = subprocess.run(
        [sys.executable, "-m", "ppbase", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    console = subprocess.run(
        [str(script), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert console.stdout == module.stdout
    assert "init" in console.stdout


@pytest.mark.parametrize(
    "argv",
    (
        [
            "ppbase",
            "backup",
            "doctor",
            "--db",
            "postgresql+asyncpg://runtime:SECRET@localhost/app",
            "--dir",
            "/tmp/ppbase-cli-data",
        ],
    ),
)
def test_backup_subcommands_parse_db_and_dir_like_serve(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ppbase.__main__ as cli

    captured = None

    def capture(args):
        nonlocal captured
        captured = args

    monkeypatch.setattr(cli, "_cmd_backup", capture)
    monkeypatch.setattr(sys, "argv", argv)

    cli.main()

    assert captured is not None
    assert captured.db == "postgresql+asyncpg://runtime:SECRET@localhost/app"
    assert captured.data_dir == "/tmp/ppbase-cli-data"


def test_backup_commands_apply_db_and_dir_to_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ppbase.__main__ as cli

    database_url = "postgresql+asyncpg://legacy:SECRET@localhost/app"
    data_dir = str(tmp_path / "custom-data")
    captured: dict[str, Settings] = {}

    async def doctor(settings, *, server_url=None):
        captured["settings"] = settings
        assert server_url is None
        return {
            "ready": True,
            "fullyVerified": True,
            "exitCode": 0,
            "checks": [],
            "warnings": [],
        }

    monkeypatch.setattr(provision, "backup_doctor", doctor)
    args = SimpleNamespace(
        action="doctor",
        db=database_url,
        data_dir=data_dir,
        server=None,
        json=True,
        local=False,
        execute=False,
        output_env=None,
        bootstrap_dsn_file=None,
    )

    cli._cmd_backup(args)

    assert captured["settings"].database_url == database_url
    assert captured["settings"].data_dir == data_dir
    assert "SECRET" not in capsys.readouterr().out
