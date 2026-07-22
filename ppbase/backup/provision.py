"""Native PostgreSQL provisioning and diagnostics for backup/restore."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import secrets
import shutil
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from ppbase.backup.control import (
    ControlPlaneRoot,
    ControlPlaneSafetyError,
    absolute_path_without_symlink_resolution,
    fsync_directory,
    same_file_identity,
    validate_entry_name,
)
from ppbase.backup.postgres import (
    PostgresBackupError,
    detect_postgres_tool_version,
    preflight_dump_role,
    set_backup_control_search_path,
    sqlalchemy_url_to_libpq,
    validate_postgres_identifier,
)
from ppbase.services.process_control import can_self_restart


PROVISION_LOCK_KEY = 0x5050424153454250
DOCTOR_EXIT_READY = 0
DOCTOR_EXIT_NOT_READY = 2
DOCTOR_EXIT_ERROR = 3
INIT_PROJECT_MAX_LENGTH = 48
INIT_MARKER_VERSION = "ppbase-init:v1"
INIT_SECRET_KEYS = frozenset(
    {
        "PPBASE_DATABASE_URL",
        "PPBASE_BACKUP_DUMP_DATABASE_URL",
        "PPBASE_BACKUP_CREATOR_DATABASE_URL",
        "PPBASE_BACKUP_RESTORE_DATABASE_URL",
        "PPBASE_BACKUP_TARGET_OWNER",
    }
)
BACKUP_PROVISION_SECRET_KEYS = frozenset(
    {
        "PPBASE_BACKUP_DUMP_DATABASE_URL",
        "PPBASE_BACKUP_CREATOR_DATABASE_URL",
        "PPBASE_BACKUP_RESTORE_DATABASE_URL",
        "PPBASE_BACKUP_TARGET_OWNER",
    }
)
LEGACY_RUNTIME_SUPERUSER_CODE = "legacy_runtime_superuser"
LEGACY_RUNTIME_SUPERUSER_DETAIL = "PostgreSQL superuser runtime"


class BackupProvisionError(RuntimeError):
    """Raised when provisioning cannot proceed without weakening safety."""


@dataclass(frozen=True, slots=True)
class RoleNames:
    runtime: str
    dump: str
    creator: str
    restore: str
    owner: str

    def as_dict(self) -> dict[str, str]:
        return {
            "runtime": self.runtime,
            "dump": self.dump,
            "creator": self.creator,
            "restore": self.restore,
            "owner": self.owner,
        }


@dataclass(frozen=True, slots=True)
class PostgresInitSpec:
    """Deterministic database and role names for one PPBase project."""

    project: str
    database: str
    roles: RoleNames

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "database": self.database,
            "roles": self.roles.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class PostgresInitLayout:
    """Private local directories required by a default PPBase deployment."""

    data_dir: Path
    backup_root: Path
    control_dir: Path
    staging_root: Path
    target_root: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "dataDir": str(self.data_dir),
            "backupRoot": str(self.backup_root),
            "controlDir": str(self.control_dir),
            "stagingRoot": str(self.staging_root),
            "targetRoot": str(self.target_root),
        }


def resolve_postgres_init_spec(name: str) -> PostgresInitSpec:
    """Resolve one conservative project name into the complete role topology."""
    project = validate_postgres_identifier(name, label="project name")
    if len(project) > INIT_PROJECT_MAX_LENGTH:
        raise BackupProvisionError(
            f"project name must contain at most {INIT_PROJECT_MAX_LENGTH} characters"
        )
    if project.startswith("pg_") or project in {
        "postgres",
        "template0",
        "template1",
    }:
        raise BackupProvisionError("project name is reserved by PostgreSQL")
    roles = RoleNames(
        runtime=project,
        dump=validate_postgres_identifier(f"{project}_backup_dump"),
        creator=validate_postgres_identifier(f"{project}_backup_creator"),
        restore=validate_postgres_identifier(f"{project}_backup_restore"),
        owner=validate_postgres_identifier(f"{project}_backup_owner"),
    )
    return PostgresInitSpec(project=project, database=project, roles=roles)


def _postgres_init_role_marker(spec: PostgresInitSpec, role: str) -> str:
    role_kind = next(
        kind for kind, configured in spec.roles.as_dict().items() if configured == role
    )
    return f"{INIT_MARKER_VERSION}:{spec.project}:role:{role_kind}"


def _postgres_init_database_marker(spec: PostgresInitSpec) -> str:
    return f"{INIT_MARKER_VERSION}:{spec.project}:database"


def resolve_postgres_init_layout(settings: Any) -> PostgresInitLayout:
    """Resolve and globally validate the default/advanced filesystem layout."""
    staging = absolute_path_without_symlink_resolution(settings.backup_staging_root)
    target_value = str(getattr(settings, "backup_target_root", "") or "").strip()
    target = absolute_path_without_symlink_resolution(
        target_value or f"{staging}_targets"
    )
    layout = PostgresInitLayout(
        data_dir=absolute_path_without_symlink_resolution(settings.data_dir),
        backup_root=absolute_path_without_symlink_resolution(settings.backup_root),
        control_dir=absolute_path_without_symlink_resolution(
            settings.backup_control_dir
        ),
        staging_root=staging,
        target_root=target,
    )
    paths = list(layout.as_dict().items())
    for index, (left_name, left_value) in enumerate(paths):
        left = Path(left_value)
        for right_name, right_value in paths[index + 1 :]:
            right = Path(right_value)
            if (
                left == right
                or left.is_relative_to(right)
                or right.is_relative_to(left)
            ):
                raise BackupProvisionError(
                    f"{left_name} and {right_name} must not overlap"
                )
    return layout


def ensure_postgres_init_layout(settings: Any) -> tuple[PostgresInitLayout, list[str]]:
    """Create private roots without following symlinks or repairing unsafe roots."""
    layout = resolve_postgres_init_layout(settings)
    created: list[str] = []
    for label, raw_path in layout.as_dict().items():
        path = Path(raw_path)
        was_missing = not path.exists()
        try:
            with ControlPlaneRoot.open(path, create_missing=True):
                pass
        except ControlPlaneSafetyError as exc:
            raise BackupProvisionError(
                f"{label} could not be created as a private 0700 directory"
            ) from exc
        if was_missing:
            created.append(label)
    return layout, created


def _quote_identifier(value: str) -> str:
    return f'"{validate_postgres_identifier(value)}"'


def _sql_literal(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise BackupProvisionError("PostgreSQL secret contains a control character")
    return "'" + value.replace("'", "''") + "'"


def _redacted_url(value: str | URL) -> str:
    parsed = make_url(str(value)) if not isinstance(value, URL) else value
    return parsed.render_as_string(hide_password=True)


def _validated_bootstrap_url(value: str | URL) -> URL:
    try:
        sqlalchemy_url_to_libpq(value)
        return value if isinstance(value, URL) else make_url(value)
    except PostgresBackupError:
        raise BackupProvisionError("invalid PostgreSQL bootstrap DSN") from None


def _configured_role(url: str, fallback: str) -> str:
    if not str(url or "").strip():
        return validate_postgres_identifier(fallback)
    return validate_postgres_identifier(
        sqlalchemy_url_to_libpq(url).username,
        label="configured backup role",
    )


def resolve_role_names(settings: Any) -> RoleNames:
    runtime = validate_postgres_identifier(
        sqlalchemy_url_to_libpq(settings.database_url).username,
        label="runtime role",
    )
    roles = RoleNames(
        runtime=runtime,
        dump=_configured_role(
            str(getattr(settings, "backup_dump_database_url", "") or ""),
            "ppbase_backup_dump",
        ),
        creator=_configured_role(
            str(getattr(settings, "backup_creator_database_url", "") or ""),
            "ppbase_backup_creator",
        ),
        restore=_configured_role(
            str(getattr(settings, "backup_restore_database_url", "") or ""),
            "ppbase_backup_restore",
        ),
        owner=validate_postgres_identifier(
            str(getattr(settings, "backup_target_owner", "") or "")
            or "ppbase_backup_owner",
            label="target owner",
        ),
    )
    if len(set(roles.as_dict().values())) != 5:
        raise BackupProvisionError(
            "runtime, dump, creator, restore, and target owner roles must be distinct"
        )
    return roles


def _role_specs(roles: RoleNames) -> dict[str, dict[str, bool]]:
    return {
        roles.dump: {"login": True, "createdb": False, "inherit": False},
        roles.creator: {"login": True, "createdb": True, "inherit": False},
        roles.restore: {"login": True, "createdb": False, "inherit": False},
        roles.owner: {"login": False, "createdb": False, "inherit": False},
    }


def _init_role_specs(roles: RoleNames) -> dict[str, dict[str, bool]]:
    return {
        roles.runtime: {"login": True, "createdb": False, "inherit": True},
        **_role_specs(roles),
    }


async def _read_role_rows(
    connection: AsyncConnection,
    roles: RoleNames,
) -> dict[str, Mapping[str, Any]]:
    rows = (
        await connection.execute(
            text(
                "SELECT r.rolname, r.rolcanlogin, r.rolcreatedb, r.rolinherit, "
                "r.rolsuper, r.rolcreaterole, r.rolreplication, r.rolbypassrls, "
                "pg_catalog.shobj_description(r.oid, 'pg_authid') AS marker "
                "FROM pg_catalog.pg_roles AS r "
                "WHERE r.rolname = ANY(CAST(:roles AS text[])) "
                "ORDER BY r.rolname"
            ),
            {"roles": list(roles.as_dict().values())},
        )
    ).mappings().all()
    return {str(row["rolname"]): row for row in rows}


async def _read_memberships(
    connection: AsyncConnection,
    roles: RoleNames,
) -> list[Mapping[str, Any]]:
    return list(
        (
            await connection.execute(
                text(
                    "SELECT member_role.rolname AS member, "
                    "granted_role.rolname AS granted, membership.admin_option, "
                    "COALESCE((to_jsonb(membership)->>'set_option')::boolean, true) "
                    "AS set_option, "
                    "COALESCE((to_jsonb(membership)->>'inherit_option')::boolean, "
                    "member_role.rolinherit) AS inherit_option "
                    "FROM pg_catalog.pg_auth_members AS membership "
                    "JOIN pg_catalog.pg_roles AS member_role "
                    "ON member_role.oid = membership.member "
                    "JOIN pg_catalog.pg_roles AS granted_role "
                    "ON granted_role.oid = membership.roleid "
                    "WHERE member_role.rolname = ANY(CAST(:roles AS text[])) "
                    "OR granted_role.rolname = ANY(CAST(:roles AS text[])) "
                    "ORDER BY member, granted"
                ),
                {
                    "roles": list(roles.as_dict().values()),
                },
            )
        ).mappings().all()
    )


def _membership_collisions(
    rows: list[Mapping[str, Any]],
    roles: RoleNames,
) -> list[dict[str, str]]:
    expected = {
        roles.creator: (roles.owner, False),
        roles.restore: (roles.owner, False),
        roles.runtime: (roles.owner, True),
    }
    collisions: list[dict[str, str]] = []
    managed = set(roles.as_dict().values())
    for row in rows:
        member = str(row["member"])
        granted = str(row["granted"])
        if member in expected:
            expected_granted, expected_inherit = expected[member]
            valid = (
                granted == expected_granted
                and not bool(row["admin_option"])
                and bool(row["set_option"])
                and bool(row["inherit_option"]) == expected_inherit
            )
            if not valid:
                collisions.append(
                    {"role": member, "reason": f"unexpected membership in {granted}"}
                )
        elif member in {roles.dump, roles.owner} or granted in managed:
            collisions.append(
                {"role": member, "reason": f"unexpected membership in {granted}"}
            )
    # Missing expected memberships are planned grants, not collisions.
    return collisions


async def _require_safe_memberships(
    connection: AsyncConnection,
    roles: RoleNames,
) -> None:
    collisions = _membership_collisions(
        await _read_memberships(connection, roles),
        roles,
    )
    if collisions:
        rendered = "; ".join(
            f"{item['role']}: {item['reason']}" for item in collisions
        )
        raise BackupProvisionError(
            f"managed role membership graph changed unsafely: {rendered}"
        )


def _role_collision(
    row: Mapping[str, Any],
    expected: Mapping[str, bool],
) -> str | None:
    if any(
        bool(row[key])
        for key in ("rolsuper", "rolcreaterole", "rolreplication", "rolbypassrls")
    ):
        return "role has forbidden elevated cluster attributes"
    checks = {
        "rolcanlogin": expected["login"],
        "rolcreatedb": expected["createdb"],
        "rolinherit": expected["inherit"],
    }
    mismatched = [key for key, value in checks.items() if bool(row[key]) != value]
    return "role attributes differ: " + ", ".join(mismatched) if mismatched else None


def _runtime_role_assessment(
    row: Mapping[str, Any],
    *,
    role: str,
) -> tuple[str | None, dict[str, str] | None]:
    """Keep the ordinary runtime strict while tolerating legacy superusers."""
    if bool(row["rolsuper"]):
        if not bool(row["rolcanlogin"]):
            return "role attributes differ: rolcanlogin", None
        return None, {
            "code": LEGACY_RUNTIME_SUPERUSER_CODE,
            "detail": LEGACY_RUNTIME_SUPERUSER_DETAIL,
            "role": role,
        }
    return (
        _role_collision(
            row,
            {"login": True, "createdb": False, "inherit": True},
        ),
        None,
    )


async def build_provision_plan(settings: Any) -> dict[str, Any]:
    """Inspect with the runtime connection inside a read-only transaction."""
    roles = resolve_role_names(settings)
    runtime_url = str(settings.database_url)
    engine = create_async_engine(runtime_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                )
                await set_backup_control_search_path(connection)
                identity = (
                    await connection.execute(
                        text(
                            "SELECT current_user AS role, "
                            "pg_catalog.current_database() AS database, "
                            "pg_catalog.current_setting('server_version_num')::integer "
                            "AS server_version_num"
                        )
                    )
                ).mappings().one()
                rows = await _read_role_rows(connection, roles)
                memberships = await _read_memberships(connection, roles)
                rls_count = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_catalog.pg_class AS c "
                                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                                "WHERE c.relkind IN ('r', 'p') AND c.relrowsecurity "
                                "AND n.nspname <> 'information_schema' "
                                "AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'"
                            )
                        )
                    ).scalar_one()
                )
    finally:
        await engine.dispose()

    if str(identity["role"]) != roles.runtime:
        raise BackupProvisionError("runtime DSN authenticated as an unexpected role")
    if rls_count:
        raise BackupProvisionError(
            "row-level-security tables are unsupported for the strict dump role"
        )

    actions: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    collisions: list[dict[str, str]] = _membership_collisions(
        memberships,
        roles,
    )
    runtime_collision, runtime_warning = _runtime_role_assessment(
        rows[roles.runtime],
        role=roles.runtime,
    )
    if runtime_collision:
        collisions.append({"role": roles.runtime, "reason": runtime_collision})
    if runtime_warning:
        warnings.append(runtime_warning)
    for role, spec in _role_specs(roles).items():
        row = rows.get(role)
        if row is None:
            actions.append({"action": "create_role", "role": role})
            continue
        collision = _role_collision(row, spec)
        if collision:
            collisions.append({"role": role, "reason": collision})
        else:
            actions.append({"action": "noop_role", "role": role})
    actions.extend(
        (
            {"action": "grant_owner_memberships", "role": roles.owner},
            {"action": "normalize_database_acl", "database": str(identity["database"])},
            {"action": "grant_dump_read_access", "role": roles.dump},
            {"action": "write_runtime_secrets", "required": True},
        )
    )
    return {
        "formatVersion": 1,
        "mode": "production",
        "readOnly": True,
        "runtime": {
            "url": _redacted_url(runtime_url),
            "role": str(identity["role"]),
            "database": str(identity["database"]),
            "serverVersionNum": int(identity["server_version_num"]),
        },
        "roles": roles.as_dict(),
        "actions": actions,
        "collisions": collisions,
        "warnings": warnings,
        "executable": not collisions,
    }


def _generate_password() -> str:
    return secrets.token_urlsafe(36)


async def _ensure_role(
    connection: AsyncConnection,
    role: str,
    spec: Mapping[str, bool],
    *,
    password: str | None,
) -> bool:
    rows = await _read_role_rows(
        connection,
        RoleNames(role, role, role, role, role),
    )
    row = rows.get(role)
    if row is not None:
        collision = _role_collision(row, spec)
        if collision:
            raise BackupProvisionError(f"unsafe existing role {role!r}: {collision}")
        return False
    clauses = [
        "LOGIN" if spec["login"] else "NOLOGIN",
        "CREATEDB" if spec["createdb"] else "NOCREATEDB",
        "INHERIT" if spec["inherit"] else "NOINHERIT",
        "NOSUPERUSER",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ]
    if password is not None:
        clauses.append(f"PASSWORD {_sql_literal(password)}")
    await connection.execute(
        text(f"CREATE ROLE {_quote_identifier(role)} {' '.join(clauses)}")
    )
    return True


async def _grant_source_dump_access(
    connection: AsyncConnection,
    *,
    dump_role: str,
    future_owners: tuple[str, ...] = (),
) -> None:
    dump = _quote_identifier(dump_role)
    schemas = (
        await connection.execute(
            text(
                "SELECT n.nspname FROM pg_catalog.pg_namespace AS n "
                "WHERE n.nspname <> 'information_schema' "
                "AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' ORDER BY n.nspname"
            )
        )
    ).scalars().all()
    for raw_schema in schemas:
        schema = _quote_identifier(str(raw_schema))
        await connection.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {dump}"))
        await connection.execute(
            text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {dump}")
        )
        await connection.execute(
            text(f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {dump}")
        )
    owner_rows = (
        await connection.execute(
            text(
                "SELECT DISTINCT pg_catalog.pg_get_userbyid(c.relowner) AS owner, "
                "n.nspname AS schema FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r','p','S','v','m','f') "
                "AND n.nspname <> 'information_schema' "
                "AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                "ORDER BY owner, schema"
            )
        )
    ).mappings().all()
    owner_schemas = {
        (str(row["owner"]), str(row["schema"])) for row in owner_rows
    }
    owner_schemas.update(
        (owner, str(schema))
        for owner in future_owners
        for schema in schemas
    )
    for owner_name, schema_name in sorted(owner_schemas):
        owner = _quote_identifier(owner_name)
        schema = _quote_identifier(schema_name)
        await connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
                f"GRANT SELECT ON TABLES TO {dump}"
            )
        )
        await connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
                f"GRANT SELECT ON SEQUENCES TO {dump}"
            )
        )

    large_objects = (
        await connection.execute(
            text(
                "SELECT large_object.oid, "
                "pg_catalog.pg_get_userbyid(large_object.lomowner) AS owner "
                "FROM pg_catalog.pg_largeobject_metadata AS large_object "
                "ORDER BY large_object.oid"
            )
        )
    ).mappings().all()
    for row in large_objects:
        oid = int(row["oid"])
        await connection.execute(
            text(f"GRANT SELECT ON LARGE OBJECT {oid} TO {dump}")
        )


async def _grant_owner_memberships(
    connection: AsyncConnection,
    *,
    roles: RoleNames,
    server_version_num: int,
) -> None:
    owner = _quote_identifier(roles.owner)
    for member, inherit in (
        (roles.creator, False),
        (roles.restore, False),
        (roles.runtime, True),
    ):
        member_identifier = _quote_identifier(member)
        if server_version_num >= 160000:
            await connection.execute(
                text(
                    f"GRANT {owner} TO {member_identifier} WITH SET TRUE, "
                    f"ADMIN FALSE, INHERIT {'TRUE' if inherit else 'FALSE'}"
                )
            )
        else:
            await connection.execute(text(f"GRANT {owner} TO {member_identifier}"))


def _runtime_urls(
    settings: Any,
    roles: RoleNames,
    passwords: Mapping[str, str],
) -> dict[str, str]:
    runtime = make_url(str(settings.database_url))

    def role_url(role: str) -> str:
        password = passwords.get(role)
        if password is None:
            configured = {
                roles.dump: str(getattr(settings, "backup_dump_database_url", "") or ""),
                roles.creator: str(getattr(settings, "backup_creator_database_url", "") or ""),
                roles.restore: str(getattr(settings, "backup_restore_database_url", "") or ""),
            }[role]
            if not configured:
                raise BackupProvisionError(
                    f"existing role {role!r} requires its configured limited DSN"
                )
            return configured
        return runtime.set(username=role, password=password).render_as_string(
            hide_password=False
        )

    return {
        "PPBASE_BACKUP_DUMP_DATABASE_URL": role_url(roles.dump),
        "PPBASE_BACKUP_CREATOR_DATABASE_URL": role_url(roles.creator),
        "PPBASE_BACKUP_RESTORE_DATABASE_URL": role_url(roles.restore),
        "PPBASE_BACKUP_TARGET_OWNER": roles.owner,
    }


def write_secret_sink(path: str | Path, values: Mapping[str, str]) -> Path:
    destination = Path(path).expanduser().absolute()
    try:
        validate_entry_name(destination.name)
        parent = ControlPlaneRoot.open(
            destination.parent,
            create_missing=True,
            require_private=False,
        )
    except ControlPlaneSafetyError as exc:
        raise BackupProvisionError("secret sink parent is unsafe") from exc
    parent_info = os.fstat(parent.fileno())
    parent_mode = stat.S_IMODE(parent_info.st_mode)
    if (
        parent_info.st_uid not in {0, os.geteuid()}
        or (parent_mode & 0o022 and not parent_mode & stat.S_ISVTX)
    ):
        parent.close()
        raise BackupProvisionError("secret sink parent is not safely owned")

    temporary_name = f".{destination.name}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    temporary_exists = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent.fileno(),
        )
        temporary_exists = True
        payload = "".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}\n"
            for key, value in sorted(values.items())
        ).encode("utf-8")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise BackupProvisionError("secret sink write was incomplete")
            remaining = remaining[written:]
        os.fsync(descriptor)
        temporary_info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary_info.st_mode)
            or temporary_info.st_uid != os.geteuid()
            or stat.S_IMODE(temporary_info.st_mode) != 0o600
            or temporary_info.st_nlink != 1
        ):
            raise BackupProvisionError("temporary secret sink is unsafe")
        os.close(descriptor)
        descriptor = None
        parent.verify_attached()
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent.fileno(),
                dst_dir_fd=parent.fileno(),
                follow_symlinks=False,
            )
        except FileExistsError:
            raise BackupProvisionError(
                "secret sink already exists; overwrite is forbidden"
            ) from None
        fsync_directory(parent.fileno())
        published = os.stat(
            destination.name,
            dir_fd=parent.fileno(),
            follow_symlinks=False,
        )
        if (
            not same_file_identity(temporary_info, published)
            or not stat.S_ISREG(published.st_mode)
            or published.st_uid != os.geteuid()
            or stat.S_IMODE(published.st_mode) != 0o600
            or published.st_nlink != 2
        ):
            raise BackupProvisionError("published secret sink is unsafe")
        os.unlink(temporary_name, dir_fd=parent.fileno())
        temporary_exists = False
        fsync_directory(parent.fileno())
        final_info = os.stat(
            destination.name,
            dir_fd=parent.fileno(),
            follow_symlinks=False,
        )
        if (
            not same_file_identity(temporary_info, final_info)
            or final_info.st_nlink != 1
            or stat.S_IMODE(final_info.st_mode) != 0o600
        ):
            raise BackupProvisionError("secret sink publication was not durable")
        parent.verify_attached()
        return destination
    except BackupProvisionError:
        raise
    except OSError as exc:
        raise BackupProvisionError("secret sink could not be written safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent.fileno())
                fsync_directory(parent.fileno())
            except FileNotFoundError:
                pass
            except OSError:
                pass
        parent.close()


def _reconcile_secret_sink_publication(
    source: Path,
    opened: os.stat_result,
) -> None:
    """Finish the sole safe hardlink publication state left by a crash."""
    if opened.st_nlink != 2:
        return
    try:
        parent = ControlPlaneRoot.open(
            source.parent,
            create_missing=False,
            require_private=False,
        )
    except ControlPlaneSafetyError as exc:
        raise BackupProvisionError("secret sink parent is unsafe") from exc
    try:
        parent_info = os.fstat(parent.fileno())
        parent_mode = stat.S_IMODE(parent_info.st_mode)
        if (
            parent_info.st_uid not in {0, os.geteuid()}
            or (parent_mode & 0o022 and not parent_mode & stat.S_ISVTX)
        ):
            raise BackupProvisionError("secret sink parent is not safely owned")
        visible = os.stat(
            source.name,
            dir_fd=parent.fileno(),
            follow_symlinks=False,
        )
        if not same_file_identity(opened, visible) or visible.st_nlink != 2:
            raise BackupProvisionError("secret sink publication state changed")
        prefix = f".{source.name}."
        candidates: list[str] = []
        for entry in os.listdir(parent.fileno()):
            if not entry.startswith(prefix) or not entry.endswith(".tmp"):
                continue
            candidate = os.stat(
                entry,
                dir_fd=parent.fileno(),
                follow_symlinks=False,
            )
            if same_file_identity(opened, candidate):
                candidates.append(entry)
        if len(candidates) != 1:
            raise BackupProvisionError(
                "secret sink has an ambiguous interrupted publication"
            )
        os.unlink(candidates[0], dir_fd=parent.fileno())
        fsync_directory(parent.fileno())
        final_info = os.stat(
            source.name,
            dir_fd=parent.fileno(),
            follow_symlinks=False,
        )
        if (
            not same_file_identity(opened, final_info)
            or final_info.st_nlink != 1
            or final_info.st_uid != os.geteuid()
            or stat.S_IMODE(final_info.st_mode) != 0o600
        ):
            raise BackupProvisionError(
                "secret sink publication could not be reconciled safely"
            )
        parent.verify_attached()
    except FileNotFoundError as exc:
        raise BackupProvisionError("secret sink publication state changed") from exc
    except OSError as exc:
        raise BackupProvisionError(
            "secret sink publication could not be reconciled safely"
        ) from exc
    finally:
        parent.close()


def read_secret_sink(path: str | Path) -> dict[str, str] | None:
    """Read an existing exclusive env sink without following or accepting links."""
    source = Path(path).expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(source, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupProvisionError("secret sink could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if opened.st_nlink == 2:
            os.close(descriptor)
            descriptor = None
            _reconcile_secret_sink_publication(source, opened)
            descriptor = os.open(source, flags)
            opened = os.fstat(descriptor)
        visible = source.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(visible.st_mode)
            or opened.st_dev != visible.st_dev
            or opened.st_ino != visible.st_ino
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
        ):
            raise BackupProvisionError(
                "secret sink must be an owned, single-link, mode-0600 regular file"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 8192)
            if not chunk:
                break
            total += len(chunk)
            if total > 128 * 1024:
                raise BackupProvisionError("secret sink is unexpectedly large")
            chunks.append(chunk)
    finally:
        if descriptor is not None:
            os.close(descriptor)

    values: dict[str, str] = {}
    try:
        payload = b"".join(chunks).decode("utf-8")
        for line in payload.splitlines():
            if not line:
                continue
            key, raw_value = line.split("=", 1)
            if key in values:
                raise ValueError("duplicate key")
            value = json.loads(raw_value)
            if not isinstance(value, str):
                raise ValueError("non-string value")
            values[key] = value
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupProvisionError("secret sink is malformed") from exc
    return values


def _postgres_init_values(
    bootstrap_database_url: str | URL,
    spec: PostgresInitSpec,
    passwords: Mapping[str, str],
) -> dict[str, str]:
    bootstrap = _validated_bootstrap_url(bootstrap_database_url)

    def role_url(role: str) -> str:
        password = str(passwords.get(role, "") or "")
        if not password:
            raise BackupProvisionError(f"limited credential for {role!r} is missing")
        return bootstrap.set(
            username=role,
            password=password,
            database=spec.database,
        ).render_as_string(hide_password=False)

    return {
        "PPBASE_DATABASE_URL": role_url(spec.roles.runtime),
        "PPBASE_BACKUP_DUMP_DATABASE_URL": role_url(spec.roles.dump),
        "PPBASE_BACKUP_CREATOR_DATABASE_URL": role_url(spec.roles.creator),
        "PPBASE_BACKUP_RESTORE_DATABASE_URL": role_url(spec.roles.restore),
        "PPBASE_BACKUP_TARGET_OWNER": spec.roles.owner,
    }


def _validate_postgres_init_values(
    values: Mapping[str, str],
    bootstrap_database_url: str | URL,
    spec: PostgresInitSpec,
) -> dict[str, str]:
    if set(values) != INIT_SECRET_KEYS:
        raise BackupProvisionError(
            "secret sink must contain exactly the limited PPBase credentials"
        )
    bootstrap = _validated_bootstrap_url(bootstrap_database_url)
    bootstrap_connection = sqlalchemy_url_to_libpq(bootstrap)
    expected_roles = {
        "PPBASE_DATABASE_URL": spec.roles.runtime,
        "PPBASE_BACKUP_DUMP_DATABASE_URL": spec.roles.dump,
        "PPBASE_BACKUP_CREATOR_DATABASE_URL": spec.roles.creator,
        "PPBASE_BACKUP_RESTORE_DATABASE_URL": spec.roles.restore,
    }
    passwords: dict[str, str] = {}
    for key, expected_role in expected_roles.items():
        try:
            parsed = make_url(str(values[key]))
        except Exception:
            raise BackupProvisionError(
                f"secret sink contains an invalid limited DSN for {key}"
            ) from None
        parsed_connection = sqlalchemy_url_to_libpq(parsed)
        if (
            parsed.drivername != "postgresql+asyncpg"
            or parsed.username != expected_role
            or parsed.database != spec.database
            or parsed_connection.host != bootstrap_connection.host
            or (parsed_connection.port or "5432")
            != (bootstrap_connection.port or "5432")
            or not parsed.password
        ):
            raise BackupProvisionError(
                f"secret sink credential {key} does not match this project/server"
            )
        passwords[expected_role] = str(parsed.password)
    if values["PPBASE_BACKUP_TARGET_OWNER"] != spec.roles.owner:
        raise BackupProvisionError("secret sink target owner does not match the project")
    return passwords


def _postgres_error_sqlstate(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if value:
                return str(value)
        next_error = getattr(current, "orig", None) or current.__cause__
        current = next_error if isinstance(next_error, BaseException) else None
    return None


async def _probe_postgres_init_credentials(
    values: Mapping[str, str],
    spec: PostgresInitSpec,
    *,
    existing_roles: set[str],
    bootstrap_database: str,
    database_exists: bool,
) -> list[dict[str, str]]:
    expected = {
        "PPBASE_DATABASE_URL": spec.roles.runtime,
        "PPBASE_BACKUP_DUMP_DATABASE_URL": spec.roles.dump,
        "PPBASE_BACKUP_CREATOR_DATABASE_URL": spec.roles.creator,
        "PPBASE_BACKUP_RESTORE_DATABASE_URL": spec.roles.restore,
    }
    collisions: list[dict[str, str]] = []
    for key, role in expected.items():
        if role not in existing_roles:
            continue
        configured = make_url(str(values[key]))
        probe_url = configured if database_exists else configured.set(
            database=bootstrap_database
        )
        engine = create_async_engine(probe_url, poolclass=NullPool)
        authenticated = False
        try:
            try:
                async with engine.connect() as connection:
                    effective = str(
                        (
                            await connection.execute(text("SELECT current_user"))
                        ).scalar_one()
                    )
                    authenticated = effective == role
                    await connection.rollback()
            except Exception as exc:
                # PostgreSQL checks CONNECT after authentication. A 42501 here
                # therefore still proves that the stored password is valid.
                authenticated = _postgres_error_sqlstate(exc) == "42501"
        finally:
            await engine.dispose()
        if not authenticated:
            collisions.append(
                {
                    "resource": role,
                    "reason": f"stored credential {key} could not authenticate",
                }
            )
    return collisions


def _validate_backup_provision_values(
    values: Mapping[str, str],
    runtime_database_url: str | URL,
    roles: RoleNames,
) -> dict[str, str]:
    if set(values) != BACKUP_PROVISION_SECRET_KEYS:
        raise BackupProvisionError(
            "secret sink must contain exactly the limited backup credentials"
        )
    runtime_connection = sqlalchemy_url_to_libpq(runtime_database_url)
    expected_roles = {
        "PPBASE_BACKUP_DUMP_DATABASE_URL": roles.dump,
        "PPBASE_BACKUP_CREATOR_DATABASE_URL": roles.creator,
        "PPBASE_BACKUP_RESTORE_DATABASE_URL": roles.restore,
    }
    passwords: dict[str, str] = {}
    for key, expected_role in expected_roles.items():
        try:
            parsed = make_url(str(values[key]))
            parsed_connection = sqlalchemy_url_to_libpq(parsed)
        except Exception:
            raise BackupProvisionError(
                f"secret sink contains an invalid limited DSN for {key}"
            ) from None
        if (
            parsed.username != expected_role
            or parsed_connection.host != runtime_connection.host
            or parsed_connection.port != runtime_connection.port
            or not parsed.password
            or (
                key == "PPBASE_BACKUP_DUMP_DATABASE_URL"
                and parsed.database != runtime_connection.database
            )
        ):
            raise BackupProvisionError(
                f"secret sink credential {key} does not match this runtime/server"
            )
        passwords[expected_role] = str(parsed.password)
    if values["PPBASE_BACKUP_TARGET_OWNER"] != roles.owner:
        raise BackupProvisionError("secret sink target owner does not match settings")
    return passwords


async def _require_backup_provision_credentials(
    values: Mapping[str, str],
    roles: RoleNames,
    *,
    existing_roles: set[str],
) -> None:
    """Authenticate every pre-existing limited backup login before mutation."""
    expected = {
        "PPBASE_BACKUP_DUMP_DATABASE_URL": roles.dump,
        "PPBASE_BACKUP_CREATOR_DATABASE_URL": roles.creator,
        "PPBASE_BACKUP_RESTORE_DATABASE_URL": roles.restore,
    }
    for key, role in expected.items():
        if role not in existing_roles:
            continue
        engine = create_async_engine(str(values[key]), poolclass=NullPool)
        authenticated = False
        try:
            try:
                async with engine.connect() as connection:
                    identity = (
                        await connection.execute(
                            text(
                                "SELECT session_user AS session_role, "
                                "current_user AS effective_role"
                            )
                        )
                    ).mappings().one()
                    authenticated = (
                        str(identity["session_role"]) == role
                        and str(identity["effective_role"]) == role
                    )
                    await connection.rollback()
            except Exception as exc:
                # PostgreSQL checks database CONNECT only after password
                # authentication, so 42501 still proves this exact login secret.
                authenticated = _postgres_error_sqlstate(exc) == "42501"
        finally:
            await engine.dispose()
        if not authenticated:
            raise BackupProvisionError(
                f"stored credential {key} could not authenticate"
            )


async def _read_init_database_row(
    connection: AsyncConnection,
    database: str,
) -> Mapping[str, Any] | None:
    return (
        await connection.execute(
            text(
                "SELECT d.datname, owner.rolname AS owner, "
                "pg_catalog.pg_encoding_to_char(d.encoding) AS encoding, "
                "d.datcollate, d.datctype, d.datallowconn, d.datistemplate, "
                "pg_catalog.shobj_description(d.oid, 'pg_database') AS marker "
                "FROM pg_catalog.pg_database AS d "
                "JOIN pg_catalog.pg_roles AS owner ON owner.oid = d.datdba "
                "WHERE d.datname = :database"
            ),
            {"database": database},
        )
    ).mappings().one_or_none()


def _init_database_collision(
    row: Mapping[str, Any],
    spec: PostgresInitSpec,
) -> str | None:
    if str(row["owner"]) != spec.roles.runtime:
        return "database owner differs from the runtime role"
    if str(row["encoding"]).upper().replace("-", "") != "UTF8":
        return "database encoding is not UTF8"
    if not bool(row["datallowconn"]) or bool(row["datistemplate"]):
        return "database connection/template flags violate the application contract"
    marker = str(row.get("marker") or "")
    if marker != _postgres_init_database_marker(spec):
        return "database is not marked as created by PPBase init"
    return None


async def _audit_pristine_unmarked_init_database(
    bootstrap_database_url: str | URL,
    spec: PostgresInitSpec,
) -> str | None:
    """Recognize only the empty template0 state left before marker publication."""
    target_url = _validated_bootstrap_url(bootstrap_database_url).set(
        database=spec.database
    )
    engine = create_async_engine(target_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                )
                await set_backup_control_search_path(connection)
                database = (
                    await connection.execute(
                        text(
                            "SELECT owner.rolname AS owner, d.datacl IS NULL AS default_acl, "
                            "pg_catalog.shobj_description(d.oid, 'pg_database') AS marker "
                            "FROM pg_catalog.pg_database AS d "
                            "JOIN pg_catalog.pg_roles AS owner ON owner.oid = d.datdba "
                            "WHERE d.datname = pg_catalog.current_database()"
                        )
                    )
                ).mappings().one()
                if str(database["owner"]) != spec.roles.runtime:
                    return "unmarked database owner differs from the runtime role"
                if not bool(database["default_acl"]):
                    return "unmarked database has explicit database ACLs"
                if str(database.get("marker") or ""):
                    return "unmarked database marker changed during audit"

                schemas = (
                    await connection.execute(
                        text(
                            "SELECT n.nspname, "
                            "pg_catalog.pg_get_userbyid(n.nspowner) AS owner "
                            "FROM pg_catalog.pg_namespace AS n "
                            "WHERE n.nspname <> 'information_schema' "
                            "AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                            "ORDER BY n.nspname"
                        )
                    )
                ).mappings().all()
                if [str(row["nspname"]) for row in schemas] != ["public"]:
                    return "unmarked database contains non-template schemas"
                if str(schemas[0]["owner"]) != "pg_database_owner":
                    return "unmarked public schema has an unexpected owner"

                schema_acl = (
                    await connection.execute(
                        text(
                            "SELECT COALESCE(grantee.rolname, 'PUBLIC') AS grantee, "
                            "acl.privilege_type, acl.is_grantable "
                            "FROM pg_catalog.pg_namespace AS n "
                            "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(" 
                            "n.nspacl, pg_catalog.acldefault('n', n.nspowner))) AS acl "
                            "LEFT JOIN pg_catalog.pg_roles AS grantee "
                            "ON grantee.oid = acl.grantee "
                            "WHERE n.nspname = 'public' "
                            "ORDER BY grantee, acl.privilege_type"
                        )
                    )
                ).mappings().all()
                allowed_schema_acl = {
                    ("PUBLIC", "USAGE", False),
                    ("pg_database_owner", "CREATE", False),
                    ("pg_database_owner", "USAGE", False),
                }
                observed_schema_acl = {
                    (
                        str(row["grantee"]),
                        str(row["privilege_type"]),
                        bool(row["is_grantable"]),
                    )
                    for row in schema_acl
                }
                if not observed_schema_acl or not observed_schema_acl.issubset(
                    allowed_schema_acl
                ):
                    return "unmarked public schema ACL is not pristine"

                state = (
                    await connection.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM pg_catalog.pg_class AS c "
                            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                            "WHERE n.nspname <> 'information_schema' "
                            "AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\') AS relations, "
                            "(SELECT count(*) FROM pg_catalog.pg_proc AS p "
                            "JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace "
                            "WHERE n.nspname <> 'information_schema' "
                            "AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\') AS routines, "
                            "(SELECT count(*) FROM pg_catalog.pg_type AS t "
                            "JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace "
                            "WHERE n.nspname <> 'information_schema' "
                            "AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\') AS types, "
                            "(SELECT count(*) FROM pg_catalog.pg_largeobject_metadata) "
                            "AS large_objects, "
                            "(SELECT count(*) FROM pg_catalog.pg_default_acl) "
                            "AS default_acls, "
                            "(SELECT count(*) FROM pg_catalog.pg_extension "
                            "WHERE extname <> 'plpgsql') AS extra_extensions"
                        )
                    )
                ).mappings().one()
                if any(int(state[key]) for key in state):
                    return "unmarked database contains application or foreign objects"
        return None
    except Exception:
        return "unmarked database could not be audited as a pristine template0 database"
    finally:
        await engine.dispose()


async def execute_provision(
    settings: Any,
    *,
    bootstrap_database_url: str,
    secret_sink: str | Path,
) -> dict[str, Any]:
    plan = await build_provision_plan(settings)
    if not plan["executable"]:
        raise BackupProvisionError("provisioning plan contains unsafe role collisions")
    roles = resolve_role_names(settings)
    bootstrap = _validated_bootstrap_url(bootstrap_database_url)
    runtime = make_url(str(settings.database_url))
    bootstrap_connection = sqlalchemy_url_to_libpq(bootstrap)
    runtime_connection = sqlalchemy_url_to_libpq(runtime)
    if (
        bootstrap_connection.host != runtime_connection.host
        or bootstrap_connection.port != runtime_connection.port
    ):
        raise BackupProvisionError("bootstrap and runtime DSNs must target the same server")
    bootstrap_runtime = bootstrap.set(database=runtime.database)
    sink_path = Path(secret_sink).expanduser().absolute()
    values: dict[str, str] | None = None
    passwords: dict[str, str] = {}
    warnings: list[dict[str, str]] = []

    engine = create_async_engine(bootstrap_runtime, poolclass=NullPool)
    created: list[str] = []
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await set_backup_control_search_path(connection)
                await connection.execute(
                    text("SELECT pg_catalog.pg_advisory_xact_lock(:key)"),
                    {"key": PROVISION_LOCK_KEY},
                )
                values = read_secret_sink(sink_path)
                sink_missing = values is None
                existing_rows = await _read_role_rows(connection, roles)
                if values is None:
                    passwords = {
                        role: _generate_password()
                        for role, role_spec in _role_specs(roles).items()
                        if role_spec["login"] and role not in existing_rows
                    }
                    values = _runtime_urls(settings, roles, passwords)
                    _validate_backup_provision_values(values, runtime, roles)
                else:
                    passwords = _validate_backup_provision_values(
                        values,
                        runtime,
                        roles,
                    )
                await _require_backup_provision_credentials(
                    values,
                    roles,
                    existing_roles=set(existing_rows),
                )
                if sink_missing:
                    write_secret_sink(sink_path, values)
                effective = str(
                    (await connection.execute(text("SELECT current_user"))).scalar_one()
                )
                privileged = bool(
                    (
                        await connection.execute(
                            text(
                                "SELECT r.rolsuper OR r.rolcreaterole "
                                "FROM pg_catalog.pg_roles AS r WHERE r.rolname = :role"
                            ),
                            {"role": effective},
                        )
                    ).scalar_one()
                )
                if not privileged:
                    raise BackupProvisionError(
                        "bootstrap role requires CREATEROLE or superuser for provisioning"
                    )
                runtime_row = (
                    await connection.execute(
                        text(
                            "SELECT r.* FROM pg_catalog.pg_roles AS r "
                            "WHERE r.rolname = :role"
                        ),
                        {"role": roles.runtime},
                    )
                ).mappings().one_or_none()
                runtime_collision = None
                runtime_warning = None
                if runtime_row is not None:
                    runtime_collision, runtime_warning = _runtime_role_assessment(
                        runtime_row,
                        role=roles.runtime,
                    )
                if runtime_row is None or runtime_collision:
                    raise BackupProvisionError(
                        "runtime role is missing or violates the strict runtime contract"
                    )
                if runtime_warning:
                    warnings.append(runtime_warning)
                for role, spec in _role_specs(roles).items():
                    password = passwords.get(role) if spec["login"] else None
                    if await _ensure_role(
                        connection,
                        role,
                        spec,
                        password=password,
                    ):
                        created.append(role)

                server_version_num = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT current_setting('server_version_num')::integer"
                            )
                        )
                    ).scalar_one()
                )
                await _grant_owner_memberships(
                    connection,
                    roles=roles,
                    server_version_num=server_version_num,
                )
                await _require_safe_memberships(connection, roles)
                database = _quote_identifier(str(runtime.database))
                dump = _quote_identifier(roles.dump)
                await connection.execute(
                    text(f"REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC")
                )
                await connection.execute(
                    text(f"REVOKE ALL ON DATABASE {database} FROM {dump}")
                )
                await connection.execute(
                    text(f"GRANT CONNECT ON DATABASE {database} TO {dump}")
                )
                for role in (roles.creator, roles.restore):
                    await connection.execute(
                        text(
                            f"GRANT CONNECT ON DATABASE {database} "
                            f"TO {_quote_identifier(role)}"
                        )
                    )
                await _grant_source_dump_access(
                    connection,
                    dump_role=roles.dump,
                    future_owners=(roles.runtime,),
                )
    except BackupProvisionError:
        raise
    except Exception:
        raise BackupProvisionError(
            "backup provisioning failed; rerun the same command to resume"
        ) from None
    finally:
        await engine.dispose()

    if values is None:  # pragma: no cover - guarded by the locked setup above
        raise BackupProvisionError("limited backup credentials were not prepared")
    return {
        "formatVersion": 1,
        "createdRoles": sorted(created),
        "secretSink": str(sink_path),
        "configurationKeys": sorted(values),
        "bootstrapCredentialPersisted": False,
        "warnings": warnings,
    }


async def build_postgres_init_plan(
    settings: Any,
    *,
    bootstrap_database_url: str,
    project_name: str,
    secret_sink: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect a cluster for autonomous PPBase onboarding without mutations."""
    spec = resolve_postgres_init_spec(project_name)
    layout = resolve_postgres_init_layout(settings)
    bootstrap = _validated_bootstrap_url(bootstrap_database_url)
    configured_values = read_secret_sink(secret_sink) if secret_sink else None
    if configured_values is not None:
        _validate_postgres_init_values(configured_values, bootstrap, spec)

    engine = create_async_engine(bootstrap, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                )
                await set_backup_control_search_path(connection)
                identity = (
                    await connection.execute(
                        text(
                            "SELECT current_user AS role, "
                            "pg_catalog.current_database() AS database, "
                            "pg_catalog.current_setting('server_version_num')::integer "
                            "AS server_version_num, r.rolsuper, r.rolcreatedb, "
                            "r.rolcreaterole FROM pg_catalog.pg_roles AS r "
                            "WHERE r.rolname = current_user"
                        )
                    )
                ).mappings().one()
                rows = await _read_role_rows(connection, spec.roles)
                memberships = await _read_memberships(connection, spec.roles)
                database_row = await _read_init_database_row(
                    connection,
                    spec.database,
                )
    except BackupProvisionError:
        raise
    except Exception:
        raise BackupProvisionError(
            "PostgreSQL initialization plan inspection failed"
        ) from None
    finally:
        await engine.dispose()

    actions: list[dict[str, Any]] = []
    collisions: list[dict[str, str]] = []
    if configured_values is not None:
        collisions.extend(
            await _probe_postgres_init_credentials(
                configured_values,
                spec,
                existing_roles=set(rows),
                bootstrap_database=str(identity["database"]),
                database_exists=database_row is not None,
            )
        )
    privileged = bool(identity["rolsuper"]) or (
        bool(identity["rolcreatedb"]) and bool(identity["rolcreaterole"])
    )
    if not privileged:
        collisions.append(
            {
                "resource": "bootstrap_role",
                "reason": "bootstrap role requires both CREATEDB and CREATEROLE or superuser",
            }
        )

    all_role_markers_valid = True
    for role, role_spec in _init_role_specs(spec.roles).items():
        row = rows.get(role)
        if row is None:
            all_role_markers_valid = False
            actions.append({"action": "create_role", "role": role})
            continue
        collision = _role_collision(row, role_spec)
        if collision:
            all_role_markers_valid = False
            collisions.append({"resource": role, "reason": collision})
        elif str(row.get("marker") or "") != _postgres_init_role_marker(
            spec, role
        ):
            all_role_markers_valid = False
            collisions.append(
                {
                    "resource": role,
                    "reason": "role is not marked as created by PPBase init",
                }
            )
        elif role_spec["login"] and configured_values is None:
            all_role_markers_valid = False
            collisions.append(
                {
                    "resource": role,
                    "reason": "existing login role has no reusable credential sink",
                }
            )
        else:
            actions.append({"action": "noop_role", "role": role})

    collisions.extend(
        {
            "resource": item["role"],
            "reason": item["reason"],
        }
        for item in _membership_collisions(memberships, spec.roles)
    )
    if database_row is None:
        actions.append({"action": "create_database", "database": spec.database})
    elif (
        not str(database_row.get("marker") or "")
        and configured_values is not None
        and all_role_markers_valid
    ):
        structural_collision = _init_database_collision(database_row, spec)
        if structural_collision != "database is not marked as created by PPBase init":
            collisions.append(
                {
                    "resource": spec.database,
                    "reason": structural_collision
                    or "unmarked database state is ambiguous",
                }
            )
        else:
            pristine_collision = await _audit_pristine_unmarked_init_database(
                bootstrap,
                spec,
            )
            if pristine_collision:
                collisions.append(
                    {"resource": spec.database, "reason": pristine_collision}
                )
            else:
                actions.append(
                    {
                        "action": "resume_pristine_database",
                        "database": spec.database,
                    }
                )
    else:
        database_collision = _init_database_collision(
            database_row,
            spec,
        )
        if database_collision:
            collisions.append(
                {"resource": spec.database, "reason": database_collision}
            )
        else:
            actions.append(
                {"action": "noop_database", "database": spec.database}
            )

    actions.extend(
        (
            {"action": "grant_owner_memberships", "role": spec.roles.owner},
            {"action": "normalize_database_acl", "database": spec.database},
            {"action": "grant_dump_default_privileges", "role": spec.roles.dump},
            {
                "action": (
                    "reuse_runtime_secrets"
                    if configured_values is not None
                    else "write_runtime_secrets"
                ),
                "required": True,
            },
            {"action": "ensure_private_directories", "paths": layout.as_dict()},
        )
    )
    return {
        "formatVersion": 1,
        "mode": "postgres_init",
        "readOnly": True,
        "bootstrap": {
            "url": _redacted_url(bootstrap),
            "role": str(identity["role"]),
            "database": str(identity["database"]),
            "serverVersionNum": int(identity["server_version_num"]),
        },
        "project": spec.as_dict(),
        "directories": layout.as_dict(),
        "secretSink": (
            str(Path(secret_sink).expanduser().absolute()) if secret_sink else None
        ),
        "actions": actions,
        "collisions": collisions,
        "executable": not collisions,
    }


async def _create_postgres_init_database(
    engine: Any,
    spec: PostgresInitSpec,
    *,
    bootstrap_database_url: str | URL,
    allow_pristine_resume: bool,
) -> tuple[bool, bool]:
    async with engine.connect() as raw_connection:
        connection = await raw_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        row = await _read_init_database_row(connection, spec.database)
        if row is not None:
            collision = _init_database_collision(row, spec)
            if (
                collision == "database is not marked as created by PPBase init"
                and allow_pristine_resume
                and not str(row.get("marker") or "")
            ):
                collision = await _audit_pristine_unmarked_init_database(
                    bootstrap_database_url,
                    spec,
                )
                if collision is None:
                    await connection.execute(
                        text(
                            f"COMMENT ON DATABASE {_quote_identifier(spec.database)} IS "
                            f"{_sql_literal(_postgres_init_database_marker(spec))}"
                        )
                    )
                    return False, True
            if collision:
                raise BackupProvisionError(
                    f"unsafe existing database {spec.database!r}: {collision}"
                )
            return False, False
        await connection.execute(
            text(
                f"CREATE DATABASE {_quote_identifier(spec.database)} "
                f"WITH OWNER {_quote_identifier(spec.roles.runtime)} "
                "TEMPLATE template0 ENCODING 'UTF8'"
            )
        )
        await connection.execute(
            text(
                f"COMMENT ON DATABASE {_quote_identifier(spec.database)} IS "
                f"{_sql_literal(_postgres_init_database_marker(spec))}"
            )
        )
        return True, True


async def _configure_postgres_init_database(
    bootstrap_database_url: str | URL,
    values: Mapping[str, str],
    spec: PostgresInitSpec,
) -> bool:
    bootstrap = _validated_bootstrap_url(bootstrap_database_url).set(
        database=spec.database
    )
    database = _quote_identifier(spec.database)
    bootstrap_engine = create_async_engine(bootstrap, poolclass=NullPool)
    try:
        async with bootstrap_engine.begin() as connection:
            await set_backup_control_search_path(connection)
            await connection.execute(
                text(f"REVOKE ALL ON DATABASE {database} FROM PUBLIC")
            )
            for role in (spec.roles.dump, spec.roles.creator, spec.roles.restore):
                identifier = _quote_identifier(role)
                await connection.execute(
                    text(f"REVOKE ALL ON DATABASE {database} FROM {identifier}")
                )
                await connection.execute(
                    text(f"GRANT CONNECT ON DATABASE {database} TO {identifier}")
                )
    finally:
        await bootstrap_engine.dispose()

    runtime_engine = create_async_engine(values["PPBASE_DATABASE_URL"], poolclass=NullPool)
    acl_changed = False
    try:
        async with runtime_engine.begin() as connection:
            await set_backup_control_search_path(connection)
            effective = str(
                (await connection.execute(text("SELECT current_user"))).scalar_one()
            )
            if effective != spec.roles.runtime:
                raise BackupProvisionError(
                    "runtime credential authenticated as an unexpected role"
                )
            dump = _quote_identifier(spec.roles.dump)
            runtime = _quote_identifier(spec.roles.runtime)
            await connection.execute(
                text("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            )
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {dump}"))
            await connection.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {runtime} IN SCHEMA public "
                    f"GRANT SELECT ON TABLES TO {dump}"
                )
            )
            await connection.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {runtime} IN SCHEMA public "
                    f"GRANT SELECT ON SEQUENCES TO {dump}"
                )
            )
            large_objects = (
                await connection.execute(
                    text(
                        "SELECT large_object.oid, "
                        "pg_catalog.pg_get_userbyid(large_object.lomowner) AS owner, "
                        "pg_catalog.pg_has_role(dump_role.oid, "
                        "large_object.lomowner, 'USAGE') OR EXISTS ("
                        "SELECT 1 FROM pg_catalog.aclexplode(COALESCE("
                        "large_object.lomacl, "
                        "pg_catalog.acldefault('L', large_object.lomowner)"
                        ")) AS privilege WHERE privilege.privilege_type = 'SELECT' "
                        "AND (privilege.grantee = 0 OR pg_catalog.pg_has_role("
                        "dump_role.oid, privilege.grantee, 'USAGE'))) AS can_select "
                        "FROM pg_catalog.pg_largeobject_metadata AS large_object "
                        "CROSS JOIN (SELECT role.oid FROM pg_catalog.pg_roles AS role "
                        "WHERE role.rolname = :dump_role) AS dump_role "
                        "ORDER BY large_object.oid"
                    ),
                    {"dump_role": spec.roles.dump},
                )
            ).mappings().all()
            for row in large_objects:
                if str(row["owner"]) != spec.roles.runtime:
                    raise BackupProvisionError(
                        "existing large object is not owned by the runtime role"
                    )
                if not bool(row["can_select"]):
                    await connection.execute(
                        text(
                            f"GRANT SELECT ON LARGE OBJECT {int(row['oid'])} TO {dump}"
                        )
                    )
                    acl_changed = True
    finally:
        await runtime_engine.dispose()

    for key, expected_role in (
        ("PPBASE_BACKUP_DUMP_DATABASE_URL", spec.roles.dump),
        ("PPBASE_BACKUP_CREATOR_DATABASE_URL", spec.roles.creator),
        ("PPBASE_BACKUP_RESTORE_DATABASE_URL", spec.roles.restore),
    ):
        engine = create_async_engine(values[key], poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                effective = str(
                    (await connection.execute(text("SELECT current_user"))).scalar_one()
                )
                if effective != expected_role:
                    raise BackupProvisionError(
                        f"{key} authenticated as an unexpected role"
                    )
                if key == "PPBASE_BACKUP_DUMP_DATABASE_URL":
                    report = await preflight_dump_role(connection)
                    if not report.ok:
                        raise BackupProvisionError(
                            "dump role preflight failed: " + "; ".join(report.errors)
                        )
                await connection.rollback()
        finally:
            await engine.dispose()
    return acl_changed


async def execute_postgres_init(
    settings: Any,
    *,
    bootstrap_database_url: str,
    project_name: str,
    secret_sink: str | Path,
) -> dict[str, Any]:
    """Create or resume a complete PPBase PostgreSQL deployment contract."""
    spec = resolve_postgres_init_spec(project_name)
    sink_path = Path(secret_sink).expanduser().absolute()
    bootstrap = _validated_bootstrap_url(bootstrap_database_url)
    engine = create_async_engine(bootstrap, poolclass=NullPool)
    lock_connection: AsyncConnection | None = None
    try:
        lock_connection = await engine.connect()
        await lock_connection.execute(
            text("SELECT pg_catalog.pg_advisory_lock(:key)"),
            {"key": PROVISION_LOCK_KEY},
        )
        await lock_connection.commit()

        plan = await build_postgres_init_plan(
            settings,
            bootstrap_database_url=bootstrap_database_url,
            project_name=project_name,
            secret_sink=sink_path,
        )
        if not plan["executable"]:
            raise BackupProvisionError(
                "PostgreSQL initialization plan contains unsafe collisions"
            )
        _layout, created_directories = ensure_postgres_init_layout(settings)

        values = read_secret_sink(sink_path)
        sink_created = values is None
        if values is None:
            passwords = {
                role: _generate_password()
                for role, role_spec in _init_role_specs(spec.roles).items()
                if role_spec["login"]
            }
            values = _postgres_init_values(bootstrap, spec, passwords)
            write_secret_sink(sink_path, values)
        else:
            passwords = _validate_postgres_init_values(values, bootstrap, spec)

        created_roles: list[str] = []
        async with engine.begin() as connection:
            await set_backup_control_search_path(connection)
            for role, role_spec in _init_role_specs(spec.roles).items():
                password = passwords.get(role) if role_spec["login"] else None
                if await _ensure_role(
                    connection,
                    role,
                    role_spec,
                    password=password,
                ):
                    created_roles.append(role)
                    await connection.execute(
                        text(
                            f"COMMENT ON ROLE {_quote_identifier(role)} IS "
                            f"{_sql_literal(_postgres_init_role_marker(spec, role))}"
                        )
                    )
            marked_rows = await _read_role_rows(connection, spec.roles)
            for role in spec.roles.as_dict().values():
                if str(marked_rows.get(role, {}).get("marker") or "") != (
                    _postgres_init_role_marker(spec, role)
                ):
                    raise BackupProvisionError(
                        f"managed role {role!r} is detached from the PPBase init marker"
                    )
            server_version_num = int(
                (
                    await connection.execute(
                        text(
                            "SELECT pg_catalog.current_setting("
                            "'server_version_num')::integer"
                        )
                    )
                ).scalar_one()
            )
            await _grant_owner_memberships(
                connection,
                roles=spec.roles,
                server_version_num=server_version_num,
            )
            await _require_safe_memberships(connection, spec.roles)

        allow_pristine_resume = any(
            action.get("action") == "resume_pristine_database"
            for action in plan["actions"]
        )
        database_created, database_marker_published = (
            await _create_postgres_init_database(
                engine,
                spec,
                bootstrap_database_url=bootstrap_database_url,
                allow_pristine_resume=allow_pristine_resume,
            )
        )
        acl_changed = await _configure_postgres_init_database(
            bootstrap_database_url,
            values,
            spec,
        )
        async with engine.begin() as connection:
            await set_backup_control_search_path(connection)
            await _require_safe_memberships(connection, spec.roles)
        changed = bool(
            sink_created
            or created_roles
            or database_created
            or database_marker_published
            or created_directories
            or acl_changed
        )
        return {
            "formatVersion": 1,
            "project": spec.as_dict(),
            "createdRoles": sorted(created_roles),
            "databaseCreated": database_created,
            "createdDirectories": sorted(created_directories),
            "secretSink": str(sink_path),
            "configurationKeys": sorted(values),
            "bootstrapCredentialPersisted": False,
            "noOp": not changed,
        }
    except BackupProvisionError:
        raise
    except Exception:
        raise BackupProvisionError(
            "PostgreSQL initialization failed; rerun the same command to resume"
        ) from None
    finally:
        if lock_connection is not None:
            try:
                await lock_connection.execute(
                    text("SELECT pg_catalog.pg_advisory_unlock(:key)"),
                    {"key": PROVISION_LOCK_KEY},
                )
                await lock_connection.commit()
            except Exception:
                pass
            await lock_connection.close()
        await engine.dispose()


def _root_check(path: str | Path, *, label: str) -> dict[str, Any]:
    selected = absolute_path_without_symlink_resolution(path)
    try:
        with ControlPlaneRoot.open(selected, create_missing=False):
            pass
        ready = True
        detail = "owned private 0700 directory available"
    except ControlPlaneSafetyError:
        ready = False
        detail = "directory is missing, unsafe, symlinked, detached, or not private 0700"
    return {"name": label, "ready": ready, "detail": detail, "path": str(selected)}


def _probe_server_restart(server_url: str) -> dict[str, Any]:
    """Probe the live process without requiring a Dashboard/admin credential."""
    parsed = urllib.parse.urlsplit(str(server_url).strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BackupProvisionError(
            "--server must be an http(s) origin without credentials, query, or path"
        )
    endpoint = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/api/health/backup-activation", "", "")
    )
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            status = int(getattr(response, "status", 0))
            observed_url = (
                str(response.geturl())
                if callable(getattr(response, "geturl", None))
                else ""
            )
            if status < 200 or status >= 300 or observed_url != endpoint:
                raise ValueError("non-authoritative HTTP response")
            payload = response.read(64 * 1024 + 1)
            if len(payload) > 64 * 1024:
                raise ValueError("oversized response")
            decoded = json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {
            "name": "restart",
            "ready": False,
            "status": "fail",
            "detail": f"running PPBase server probe failed (HTTP {exc.code})",
        }
    except (
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        http.client.HTTPException,
    ):
        return {
            "name": "restart",
            "ready": False,
            "status": "fail",
            "detail": "running PPBase server is unreachable or returned invalid JSON",
        }
    try:
        if (
            not isinstance(decoded, dict)
            or decoded.get("code") != 200
            or decoded.get("message")
            != "Backup activation capability inspected."
        ):
            raise KeyError("invalid PPBase health envelope")
        configured = decoded["data"]["activation"]["configured"]
    except (KeyError, TypeError):
        configured = None
    if configured is True:
        return {
            "name": "restart",
            "ready": True,
            "status": "pass",
            "detail": "running PPBase server confirms activation restart support",
        }
    if configured is False:
        return {
            "name": "restart",
            "ready": False,
            "status": "fail",
            "detail": "running PPBase server confirms activation restart is unavailable",
        }
    return {
        "name": "restart",
        "ready": False,
        "status": "fail",
        "detail": "running server returned no authoritative activation capability",
    }


async def backup_doctor(
    settings: Any,
    *,
    server_url: str | None = None,
) -> dict[str, Any]:
    """Run non-secret readiness checks without a cluster-admin credential."""
    checks: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    storage_backend = str(getattr(settings, "storage_backend", "local") or "local")
    checks.append(
        {
            "name": "storage",
            "ready": storage_backend == "local",
            "detail": f"backend={storage_backend}",
        }
    )
    staging = str(settings.backup_staging_root)
    target = str(getattr(settings, "backup_target_root", "") or f"{staging}_targets")
    checks.extend(
        (
            _root_check(settings.backup_root, label="backup_root"),
            _root_check(settings.backup_control_dir, label="control_plane"),
            _root_check(staging, label="staging_root"),
            _root_check(target, label="target_root"),
        )
    )
    if server_url:
        checks.append(await asyncio.to_thread(_probe_server_restart, server_url))
    else:
        restart_visible = can_self_restart()
        checks.append(
            {
                "name": "restart",
                "ready": True,
                "status": "warn" if restart_visible else "skip",
                "detail": (
                    "restart command is visible locally but does not prove the running server"
                    if restart_visible
                    else "not observable from a standalone process; use --server URL"
                ),
            }
        )
    tool_map = {
        "pg_dump": settings.backup_pg_dump_path,
        "pg_restore": settings.backup_pg_restore_path,
        "psql": settings.backup_psql_path,
    }

    runtime_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    server_major: int | None = None
    try:
        async with runtime_engine.connect() as connection:
            await set_backup_control_search_path(connection)
            identity = (
                await connection.execute(
                    text(
                        "SELECT current_user AS role, current_database() AS database, "
                        "current_setting('server_version_num')::integer AS version, "
                        "r.rolcanlogin, r.rolcreatedb, r.rolinherit, r.rolsuper, "
                        "r.rolcreaterole, r.rolreplication, r.rolbypassrls "
                        "FROM pg_catalog.pg_roles AS r WHERE r.rolname = current_user"
                    )
                )
            ).mappings().one()
            checks.append(
                {
                    "name": "runtime_database",
                    "ready": True,
                    "detail": f"{identity['role']}@{identity['database']} PostgreSQL {identity['version']}",
                }
            )
            runtime_collision, runtime_warning = _runtime_role_assessment(
                identity,
                role=str(identity["role"]),
            )
            if runtime_collision:
                checks.append(
                    {
                        "name": "runtime_role",
                        "ready": False,
                        "status": "fail",
                        "detail": runtime_collision,
                    }
                )
            elif runtime_warning:
                warnings.append(runtime_warning)
                checks.append(
                    {
                        "name": "runtime_role",
                        "ready": True,
                        "status": "warn",
                        **runtime_warning,
                    }
                )
            else:
                checks.append(
                    {
                        "name": "runtime_role",
                        "ready": True,
                        "detail": "strict runtime role",
                    }
                )
            server_major = int(identity["version"]) // 10000
            allowed_extensions = {
                tuple(str(item).split("=", 1))
                for item in (getattr(settings, "backup_allowed_extensions", ()) or ())
                if "=" in str(item)
            }
            installed_extensions = {
                (str(row["name"]), str(row["version"]))
                for row in (
                    await connection.execute(
                        text(
                            "SELECT e.extname AS name, e.extversion AS version "
                            "FROM pg_catalog.pg_extension AS e ORDER BY e.extname"
                        )
                    )
                ).mappings().all()
            }
            unsupported_extensions = sorted(
                installed_extensions - allowed_extensions
            )
            checks.append(
                {
                    "name": "extensions",
                    "ready": not unsupported_extensions,
                    "detail": (
                        "installed extensions match the allowlist"
                        if not unsupported_extensions
                        else "unsupported installed extensions: "
                        + ", ".join(
                            f"{name}={version}"
                            for name, version in unsupported_extensions
                        )
                    ),
                }
            )
            await connection.rollback()
    except Exception:
        checks.append(
            {"name": "runtime_database", "ready": False, "detail": "connection or catalog inspection failed"}
        )
    finally:
        await runtime_engine.dispose()

    for name, executable in tool_map.items():
        resolved = shutil.which(str(executable))
        if resolved is None:
            checks.append(
                {"name": name, "ready": False, "detail": "executable not found"}
            )
            continue
        try:
            version = await detect_postgres_tool_version(resolved)
            matches = server_major is not None and version.major == server_major
            checks.append(
                {
                    "name": name,
                    "ready": matches,
                    "detail": (
                        f"major {version.major} matches server"
                        if matches
                        else f"tool major {version.major} does not match server major {server_major}"
                    ),
                }
            )
        except PostgresBackupError:
            checks.append(
                {"name": name, "ready": False, "detail": "version detection failed"}
            )

    dump_url = str(getattr(settings, "backup_dump_database_url", "") or "").strip()
    if not dump_url:
        checks.append(
            {"name": "dump_role", "ready": False, "detail": "dedicated dump DSN missing"}
        )
    else:
        dump_engine = create_async_engine(dump_url, poolclass=NullPool)
        try:
            async with dump_engine.connect() as connection:
                report = await preflight_dump_role(connection)
                checks.append(
                    {
                        "name": "dump_role",
                        "ready": report.ok,
                        "detail": "ready" if report.ok else "; ".join(report.errors),
                    }
                )
                await connection.rollback()
        except Exception:
            checks.append(
                {"name": "dump_role", "ready": False, "detail": "connection or privilege preflight failed"}
            )
        finally:
            await dump_engine.dispose()

    configured_restore = all(
        str(getattr(settings, field, "") or "").strip()
        for field in (
            "backup_creator_database_url",
            "backup_restore_database_url",
            "backup_target_owner",
        )
    )
    checks.append(
        {
            "name": "restore_roles",
            "ready": configured_restore,
            "detail": "configured" if configured_restore else "creator/restore/owner configuration missing",
        }
    )
    if configured_restore:
        role_connections_ready = True
        role_details: list[str] = []
        for attribute, expected_role in (
            ("backup_creator_database_url", resolve_role_names(settings).creator),
            ("backup_restore_database_url", resolve_role_names(settings).restore),
        ):
            engine = create_async_engine(getattr(settings, attribute), poolclass=NullPool)
            try:
                async with engine.connect() as connection:
                    effective = str(
                        (await connection.execute(text("SELECT current_user"))).scalar_one()
                    )
                    if effective != expected_role:
                        role_connections_ready = False
                        role_details.append(f"{attribute} authenticated as another role")
                    await connection.rollback()
            except Exception:
                role_connections_ready = False
                role_details.append(f"{attribute} connection failed")
            finally:
                await engine.dispose()
        try:
            role_plan = await build_provision_plan(settings)
            if role_plan["collisions"]:
                role_connections_ready = False
                role_details.append("role attributes or memberships violate the contract")
        except (BackupProvisionError, PostgresBackupError):
            role_connections_ready = False
            role_details.append("role catalog inspection failed")
        checks.append(
            {
                "name": "postgres_role_contract",
                "ready": role_connections_ready,
                "detail": "ready" if role_connections_ready else "; ".join(role_details),
            }
        )
    for check in checks:
        check.setdefault("status", "pass" if check.get("ready") else "fail")
    ready = all(str(check["status"]) != "fail" for check in checks)
    fully_verified = all(str(check["status"]) == "pass" for check in checks)
    return {
        "formatVersion": 1,
        "ready": ready,
        "fullyVerified": fully_verified,
        "exitCode": DOCTOR_EXIT_READY if ready else DOCTOR_EXIT_NOT_READY,
        "checks": checks,
        "warnings": warnings,
        "commands": {
            "plan": "ppbase backup provision --plan",
            "execute": "ppbase backup provision --execute --output-env ./ppbase-backup.env",
            "doctor": (
                f"ppbase backup doctor --server {server_url}"
                if server_url
                else "ppbase backup doctor"
            ),
        },
    }


def doctor_human(report: Mapping[str, Any]) -> str:
    lines = ["PPBase native backup doctor"]
    for check in report.get("checks", []):
        status = str(
            check.get("status")
            or ("pass" if check.get("ready") else "fail")
        ).upper()
        marker = "OK" if status == "PASS" else status
        lines.append(f"[{marker}] {check.get('name')}: {check.get('detail')}")
    if not report.get("ready"):
        lines.append("Not ready; fix the failed checks above.")
    elif report.get("warnings"):
        lines.append("Ready with security warnings.")
    elif report.get("fullyVerified", True):
        lines.append("Ready.")
    else:
        lines.append("No confirmed blocker; readiness is partial.")
    return "\n".join(lines)
