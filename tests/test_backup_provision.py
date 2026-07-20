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
    build_provision_plan,
    doctor_human,
    ensure_postgres_init_layout,
    resolve_postgres_init_spec,
    resolve_role_names,
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


def test_secret_sink_is_exclusive_0600_and_contains_only_limited_credentials(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "private" / "backup.env"
    written = write_secret_sink(
        destination,
        {
            "PPBASE_BACKUP_DUMP_DATABASE_URL": "postgresql://dump:DUMP@db/app",
            "PPBASE_BACKUP_TARGET_OWNER": "ppbase_backup_owner",
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


def test_role_resolution_uses_existing_manual_configuration_and_refuses_overlap(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "backup_dump_database_url": "postgresql+asyncpg://dump@localhost/app",
            "backup_creator_database_url": "postgresql+asyncpg://creator@localhost/app",
            "backup_restore_database_url": "postgresql+asyncpg://restore@localhost/app",
            "backup_target_owner": "owner",
        }
    )
    assert resolve_role_names(settings).as_dict() == {
        "runtime": "runtime",
        "dump": "dump",
        "creator": "creator",
        "restore": "restore",
        "owner": "owner",
    }

    settings.backup_target_owner = "runtime"
    with pytest.raises(BackupProvisionError, match="must be distinct"):
        resolve_role_names(settings)


@pytest.mark.asyncio
async def test_provision_plan_is_read_only_and_redacts_runtime_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Result:
        def __init__(self, *, one=None, rows=None, scalar=None):
            self._one = one
            self._rows = rows or []
            self._scalar = scalar

        def mappings(self):
            return self

        def one(self):
            return self._one

        def all(self):
            return self._rows

        def scalar_one(self):
            return self._scalar

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
            if "AS server_version_num" in sql:
                return Result(one={
                    "role": "runtime",
                    "database": "app",
                    "server_version_num": 160004,
                })
            if "FROM pg_catalog.pg_auth_members" in sql:
                return Result(rows=[])
            if "FROM pg_catalog.pg_roles AS r" in sql:
                return Result(rows=[{
                    "rolname": "runtime",
                    "rolcanlogin": True,
                    "rolcreatedb": False,
                    "rolinherit": True,
                    "rolsuper": False,
                    "rolcreaterole": False,
                    "rolreplication": False,
                    "rolbypassrls": False,
                }])
            if "c.relrowsecurity" in sql:
                return Result(scalar=0)
            return Result()

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

    monkeypatch.setattr(provision, "create_async_engine", lambda *_args, **_kwargs: Engine())
    plan = await build_provision_plan(_settings(tmp_path))

    assert plan["readOnly"] is True
    assert plan["executable"] is True
    assert plan["warnings"] == []
    assert "RUNTIME_SECRET" not in json.dumps(plan)
    assert any(action["action"] == "create_role" for action in plan["actions"])
    assert not any(
        token in statement.upper()
        for statement in statements
        for token in ("CREATE ROLE", "GRANT ", "REVOKE ", "ALTER ROLE")
    )


@pytest.mark.asyncio
async def test_provision_plan_accepts_runtime_superuser_with_stable_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, *, one=None, scalar=None):
            self._one = one
            self._scalar = scalar

        def mappings(self):
            return self

        def one(self):
            return self._one

        def scalar_one(self):
            return self._scalar

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
            if "AS server_version_num" in sql:
                return Result(
                    one={
                        "role": "runtime",
                        "database": "app",
                        "server_version_num": 160004,
                    }
                )
            if "c.relrowsecurity" in sql:
                return Result(scalar=0)
            return Result()

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

    runtime_row = {
        "rolname": "runtime",
        "rolcanlogin": True,
        "rolcreatedb": True,
        "rolinherit": False,
        "rolsuper": True,
        "rolcreaterole": True,
        "rolreplication": True,
        "rolbypassrls": True,
    }

    async def read_roles(_connection, _roles):
        return {"runtime": runtime_row}

    async def read_memberships(_connection, _roles):
        return []

    monkeypatch.setattr(provision, "create_async_engine", lambda *_a, **_kw: Engine())
    monkeypatch.setattr(provision, "_read_role_rows", read_roles)
    monkeypatch.setattr(provision, "_read_memberships", read_memberships)

    plan = await build_provision_plan(_settings(tmp_path))

    assert plan["executable"] is True
    assert plan["collisions"] == []
    assert plan["warnings"] == [
        {
            "code": "legacy_runtime_superuser",
            "detail": "PostgreSQL superuser runtime",
            "role": "runtime",
        }
    ]

    managed_collision = provision._role_collision(
        runtime_row,
        {"login": True, "createdb": False, "inherit": False},
    )
    assert managed_collision == "role has forbidden elevated cluster attributes"


@pytest.mark.asyncio
async def test_execute_reassesses_runtime_superuser_under_lock_without_altering_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "backup_dump_database_url": (
                "postgresql+asyncpg://dump:DUMP@localhost/app"
            ),
            "backup_creator_database_url": (
                "postgresql+asyncpg://creator:CREATOR@localhost/app"
            ),
            "backup_restore_database_url": (
                "postgresql+asyncpg://restore:RESTORE@localhost/app"
            ),
            "backup_target_owner": "owner",
        }
    )
    values = {
        "PPBASE_BACKUP_DUMP_DATABASE_URL": settings.backup_dump_database_url,
        "PPBASE_BACKUP_CREATOR_DATABASE_URL": (
            settings.backup_creator_database_url
        ),
        "PPBASE_BACKUP_RESTORE_DATABASE_URL": (
            settings.backup_restore_database_url
        ),
        "PPBASE_BACKUP_TARGET_OWNER": settings.backup_target_owner,
    }
    statements: list[str] = []

    class Result:
        def __init__(self, *, scalar=None, row=None):
            self._scalar = scalar
            self._row = row

        def scalar_one(self):
            return self._scalar

        def mappings(self):
            return self

        def one_or_none(self):
            return self._row

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    runtime_row = {
        "rolname": "runtime",
        "rolcanlogin": True,
        "rolcreatedb": True,
        "rolinherit": True,
        "rolsuper": True,
        "rolcreaterole": True,
        "rolreplication": True,
        "rolbypassrls": True,
    }

    class Connection:
        def begin(self):
            return Transaction()

        async def execute(self, statement, _params=None):
            sql = str(statement)
            statements.append(sql)
            if sql == "SELECT current_user":
                return Result(scalar="clusteradmin")
            if "r.rolsuper OR r.rolcreaterole" in sql:
                return Result(scalar=True)
            if "SELECT r.* FROM pg_catalog.pg_roles" in sql:
                return Result(row=runtime_row)
            if "server_version_num" in sql:
                return Result(scalar=160000)
            return Result()

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

    async def plan_before_lock(_settings_value):
        return {"executable": True, "collisions": [], "warnings": []}

    async def read_roles(_connection, roles):
        return {
            role: {"rolname": role}
            for role in roles.as_dict().values()
        }

    async def noop(*_args, **_kwargs):
        return None

    async def existing_role(*_args, **_kwargs):
        return False

    monkeypatch.setattr(provision, "build_provision_plan", plan_before_lock)
    monkeypatch.setattr(provision, "create_async_engine", lambda *_a, **_kw: Engine())
    monkeypatch.setattr(provision, "read_secret_sink", lambda _path: values)
    monkeypatch.setattr(provision, "_read_role_rows", read_roles)
    monkeypatch.setattr(provision, "_require_backup_provision_credentials", noop)
    monkeypatch.setattr(provision, "_ensure_role", existing_role)
    monkeypatch.setattr(provision, "_grant_owner_memberships", noop)
    monkeypatch.setattr(provision, "_require_safe_memberships", noop)
    monkeypatch.setattr(provision, "_grant_source_dump_access", noop)

    result = await provision.execute_provision(
        settings,
        bootstrap_database_url=(
            "postgresql+asyncpg://clusteradmin:BOOTSTRAP@localhost/postgres"
        ),
        secret_sink=tmp_path / "backup.env",
    )

    assert result["createdRoles"] == []
    assert result["warnings"] == [
        {
            "code": "legacy_runtime_superuser",
            "detail": "PostgreSQL superuser runtime",
            "role": "runtime",
        }
    ]
    assert not any("ALTER ROLE" in statement.upper() for statement in statements)


@pytest.mark.asyncio
async def test_postgres_16_memberships_use_exact_inherit_options() -> None:
    statements: list[str] = []

    class Connection:
        async def execute(self, statement):
            statements.append(str(statement))
            return object()

    roles = resolve_role_names(_settings(Path("/tmp/provision-memberships")))
    await provision._grant_owner_memberships(
        Connection(),
        roles=roles,
        server_version_num=160000,
    )

    joined = "\n".join(statements)
    assert (
        'GRANT "ppbase_backup_owner" TO "ppbase_backup_creator" '
        "WITH SET TRUE, ADMIN FALSE, INHERIT FALSE"
    ) in joined
    assert (
        'GRANT "ppbase_backup_owner" TO "ppbase_backup_restore" '
        "WITH SET TRUE, ADMIN FALSE, INHERIT FALSE"
    ) in joined
    assert (
        'GRANT "ppbase_backup_owner" TO "runtime" '
        "WITH SET TRUE, ADMIN FALSE, INHERIT TRUE"
    ) in joined


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
        "Not ready; fix the failed checks above."
    )


def test_doctor_human_reports_non_blocking_runtime_security_warning() -> None:
    report = {
        "ready": True,
        "fullyVerified": False,
        "warnings": [
            {
                "code": "legacy_runtime_superuser",
                "detail": "PostgreSQL superuser runtime",
                "role": "runtime",
            }
        ],
        "checks": [
            {
                "name": "runtime_role",
                "ready": True,
                "status": "warn",
                "code": "legacy_runtime_superuser",
                "detail": "PostgreSQL superuser runtime",
            }
        ],
    }
    assert doctor_human(report) == (
        "PPBase native backup doctor\n"
        "[WARN] runtime_role: PostgreSQL superuser runtime\n"
        "Ready with security warnings."
    )


@pytest.mark.asyncio
async def test_doctor_is_ready_with_runtime_superuser_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "backup_dump_database_url": (
                "postgresql+asyncpg://dump:DUMP@localhost/app"
            ),
            "backup_creator_database_url": (
                "postgresql+asyncpg://creator:CREATOR@localhost/app"
            ),
            "backup_restore_database_url": (
                "postgresql+asyncpg://restore:RESTORE@localhost/app"
            ),
            "backup_target_owner": "owner",
            "backup_pg_dump_path": sys.executable,
            "backup_pg_restore_path": sys.executable,
            "backup_psql_path": sys.executable,
        }
    )
    for name in ("backups", "control", "staging", "targets"):
        (tmp_path / name).mkdir(mode=0o700)

    class Result:
        def __init__(self, *, row=None, rows=None, scalar=None):
            self._row = row
            self._rows = rows or []
            self._scalar = scalar

        def mappings(self):
            return self

        def one(self):
            return self._row

        def all(self):
            return self._rows

        def scalar_one(self):
            return self._scalar

    class Connection:
        def __init__(self, role: str):
            self.role = role

        async def execute(self, statement, _params=None):
            sql = str(statement)
            if "AS version" in sql:
                return Result(
                    row={
                        "role": "runtime",
                        "database": "app",
                        "version": 160004,
                        "rolcanlogin": True,
                        "rolcreatedb": True,
                        "rolinherit": True,
                        "rolsuper": True,
                        "rolcreaterole": True,
                        "rolreplication": True,
                        "rolbypassrls": True,
                    }
                )
            if "FROM pg_catalog.pg_extension" in sql:
                return Result(rows=[{"name": "plpgsql", "version": "1.0"}])
            if sql == "SELECT current_user":
                return Result(scalar=self.role)
            return Result()

        async def rollback(self):
            return None

    class ConnectionContext:
        def __init__(self, role: str):
            self.role = role

        async def __aenter__(self):
            return Connection(self.role)

        async def __aexit__(self, *_args):
            return None

    class Engine:
        def __init__(self, role: str):
            self.role = role

        def connect(self):
            return ConnectionContext(self.role)

        async def dispose(self):
            return None

    def engine_for(url, **_kwargs):
        return Engine(str(url).split("//", 1)[1].split(":", 1)[0])

    async def matching_version(executable: str):
        return SimpleNamespace(executable=executable, version="16", major=16)

    async def strict_plan(_settings_value):
        return {
            "executable": True,
            "collisions": [],
            "warnings": [
                {
                    "code": "legacy_runtime_superuser",
                    "detail": "PostgreSQL superuser runtime",
                    "role": "runtime",
                }
            ],
        }

    monkeypatch.setattr(provision, "create_async_engine", engine_for)
    monkeypatch.setattr(provision, "detect_postgres_tool_version", matching_version)

    async def dump_preflight(_connection):
        return SimpleNamespace(ok=True, errors=())

    monkeypatch.setattr(provision, "preflight_dump_role", dump_preflight)
    monkeypatch.setattr(provision, "build_provision_plan", strict_plan)
    monkeypatch.setattr(provision, "can_self_restart", lambda: False)

    report = await provision.backup_doctor(settings)

    runtime_check = next(
        check for check in report["checks"] if check["name"] == "runtime_role"
    )
    assert report["ready"] is True
    assert report["exitCode"] == 0
    assert runtime_check == {
        "name": "runtime_role",
        "ready": True,
        "status": "warn",
        "code": "legacy_runtime_superuser",
        "detail": "PostgreSQL superuser runtime",
        "role": "runtime",
    }
    assert report["warnings"] == [
        {
            "code": "legacy_runtime_superuser",
            "detail": "PostgreSQL superuser runtime",
            "role": "runtime",
        }
    ]


def test_postgres_init_name_contract_is_deterministic_and_bounded() -> None:
    spec = resolve_postgres_init_spec("ledger")
    assert spec.database == "ledger"
    assert spec.roles.as_dict() == {
        "runtime": "ledger",
        "dump": "ledger_backup_dump",
        "creator": "ledger_backup_creator",
        "restore": "ledger_backup_restore",
        "owner": "ledger_backup_owner",
    }
    with pytest.raises(BackupProvisionError, match="reserved"):
        resolve_postgres_init_spec("postgres")
    with pytest.raises(BackupProvisionError, match="at most 48"):
        resolve_postgres_init_spec("a" * 49)


def test_membership_collisions_reject_incoming_access_to_managed_roles(
    tmp_path: Path,
) -> None:
    roles = resolve_role_names(_settings(tmp_path))
    collisions = provision._membership_collisions(
        [
            {
                "member": "external_operator",
                "granted": roles.runtime,
                "admin_option": False,
                "set_option": True,
                "inherit_option": False,
            }
        ],
        roles,
    )
    assert collisions == [
        {
            "role": "external_operator",
            "reason": f"unexpected membership in {roles.runtime}",
        }
    ]


def test_postgres_init_layout_creates_private_defaults_idempotently(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first_layout, first_created = ensure_postgres_init_layout(settings)
    first_inodes = {
        name: Path(path).stat().st_ino
        for name, path in first_layout.as_dict().items()
    }
    assert set(first_created) == set(first_layout.as_dict())
    for path in first_layout.as_dict().values():
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o700

    second_layout, second_created = ensure_postgres_init_layout(settings)
    assert second_created == []
    assert {
        name: Path(path).stat().st_ino
        for name, path in second_layout.as_dict().items()
    } == first_inodes


def test_doctor_root_check_refuses_symlink_and_non_private_directory(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    assert provision._root_check(private, label="private")["ready"] is True

    non_private = tmp_path / "non-private"
    non_private.mkdir(mode=0o755)
    assert provision._root_check(non_private, label="non_private")["ready"] is False

    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    assert provision._root_check(link, label="link")["ready"] is False


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
    assert not any(Path(path).exists() for path in plan["directories"].values())
    assert not any(
        token in statement.upper()
        for statement in statements
        for token in ("CREATE ROLE", "CREATE DATABASE", "GRANT ", "REVOKE ")
    )


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
                    "message": "Backup activation capability inspected.",
                    "data": {"activation": {"configured": configured}},
                }
            ).encode()

        def geturl(self):
            return "http://127.0.0.1:8090/api/health/backup-activation"

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
                return "http://127.0.0.1:9/api/health/backup-activation"

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
                return "http://other.test/api/health/backup-activation"
            return "http://127.0.0.1:8090/api/health/backup-activation"

        def read(self, _limit):
            if mode == "truncated":
                raise provision.http.client.IncompleteRead(b"{")
            if mode == "missing_envelope":
                return json.dumps(
                    {"data": {"activation": {"configured": True}}}
                ).encode()
            return json.dumps(
                {
                    "code": 200,
                    "message": "Backup activation capability inspected.",
                    "data": {"activation": {"configured": True}},
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
            "provision",
            "--plan",
            "--db",
            "postgresql+asyncpg://runtime:SECRET@localhost/app",
            "--dir",
            "/tmp/ppbase-cli-data",
        ],
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


@pytest.mark.parametrize("action", ("provision", "doctor"))
def test_backup_commands_apply_db_and_dir_to_settings(
    action: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ppbase.__main__ as cli

    database_url = "postgresql+asyncpg://legacy:SECRET@localhost/app"
    data_dir = str(tmp_path / "custom-data")
    captured: dict[str, Settings] = {}

    async def plan(settings):
        captured["settings"] = settings
        return {"executable": True, "warnings": [], "collisions": []}

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

    monkeypatch.setattr(provision, "build_provision_plan", plan)
    monkeypatch.setattr(provision, "backup_doctor", doctor)
    args = SimpleNamespace(
        action=action,
        db=database_url,
        data_dir=data_dir,
        server=None,
        json=True,
        local=False,
        plan=action == "provision",
        execute=False,
        output_env=None,
        bootstrap_dsn_file=None,
    )

    cli._cmd_backup(args)

    assert captured["settings"].database_url == database_url
    assert captured["settings"].data_dir == data_dir
    assert "SECRET" not in capsys.readouterr().out
