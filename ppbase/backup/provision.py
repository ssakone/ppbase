"""Native PostgreSQL provisioning and diagnostics for backup/restore."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import secrets
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
    preflight_destructive_restore_role,
    preflight_dump_role,
    set_backup_control_search_path,
    sqlalchemy_url_to_libpq,
    validate_postgres_identifier,
)
from ppbase.backup.tools import (
    PostgresToolResolutionError,
    resolve_postgres_tool,
)
from ppbase.services.process_control import can_self_restart


PROVISION_LOCK_KEY = 0x5050424153454250
DOCTOR_EXIT_READY = 0
DOCTOR_EXIT_NOT_READY = 2
DOCTOR_EXIT_ERROR = 3
INIT_PROJECT_MAX_LENGTH = 48
INIT_MARKER_VERSION = "ppbase-init:v1"
INIT_SECRET_KEYS = frozenset({"PPBASE_DATABASE_URL"})
DUMP_PROVISION_SECRET_KEYS = frozenset({"PPBASE_BACKUP_DUMP_DATABASE_URL"})
RUNTIME_SUPERUSER_CODE = "runtime_superuser"
RUNTIME_SUPERUSER_DETAIL = "PostgreSQL superuser runtime"


class BackupProvisionError(RuntimeError):
    """Raised when provisioning cannot proceed without weakening safety."""


@dataclass(frozen=True, slots=True)
class PostgresInitSpec:
    """Deterministic database and runtime role for one PPBase project."""

    project: str
    database: str
    runtime_role: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "database": self.database,
            "roles": {"runtime": self.runtime_role},
        }


@dataclass(frozen=True, slots=True)
class PostgresInitLayout:
    """Private local directories required by a default PPBase deployment."""

    data_dir: Path
    backup_root: Path
    control_dir: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "dataDir": str(self.data_dir),
            "backupRoot": str(self.backup_root),
            "controlDir": str(self.control_dir),
        }


def resolve_postgres_init_spec(name: str) -> PostgresInitSpec:
    """Resolve one conservative project name into its database/runtime role."""
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
    return PostgresInitSpec(
        project=project,
        database=project,
        runtime_role=project,
    )


def _postgres_init_role_marker(spec: PostgresInitSpec) -> str:
    return f"{INIT_MARKER_VERSION}:{spec.project}:role:runtime"


def _postgres_init_database_marker(spec: PostgresInitSpec) -> str:
    return f"{INIT_MARKER_VERSION}:{spec.project}:database"


def resolve_postgres_init_layout(settings: Any) -> PostgresInitLayout:
    """Resolve and globally validate the three active filesystem roots."""
    layout = PostgresInitLayout(
        data_dir=absolute_path_without_symlink_resolution(settings.data_dir),
        backup_root=absolute_path_without_symlink_resolution(settings.backup_root),
        control_dir=absolute_path_without_symlink_resolution(
            settings.backup_control_dir
        ),
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


def resolve_dump_role_name(settings: Any) -> str:
    """Resolve the sole optional role managed by ``backup provision``."""
    runtime = validate_postgres_identifier(
        sqlalchemy_url_to_libpq(settings.database_url).username,
        label="runtime role",
    )
    dump = _configured_role(
        str(getattr(settings, "backup_dump_database_url", "") or ""),
        "ppbase_backup_dump",
    )
    if dump == runtime:
        raise BackupProvisionError(
            "backup provision is only for a separate dump role; "
            "the normal backup path already uses PPBASE_DATABASE_URL"
        )
    return dump


async def _read_role_row(
    connection: AsyncConnection,
    role: str,
) -> Mapping[str, Any] | None:
    return (
        await connection.execute(
            text(
                "SELECT r.rolname, r.rolcanlogin, r.rolcreatedb, r.rolinherit, "
                "r.rolsuper, r.rolcreaterole, r.rolreplication, r.rolbypassrls, "
                "pg_catalog.shobj_description(r.oid, 'pg_authid') AS marker "
                "FROM pg_catalog.pg_roles AS r WHERE r.rolname = :role"
            ),
            {"role": role},
        )
    ).mappings().one_or_none()


async def _read_role_memberships(
    connection: AsyncConnection,
    role: str,
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
                    "WHERE member_role.rolname = :role "
                    "OR granted_role.rolname = :role "
                    "ORDER BY member, granted"
                ),
                {"role": role},
            )
        ).mappings().all()
    )


def _membership_collisions(
    rows: list[Mapping[str, Any]],
    role: str,
) -> list[dict[str, str]]:
    collisions: list[dict[str, str]] = []
    for row in rows:
        member = str(row["member"])
        granted = str(row["granted"])
        collisions.append(
            {
                "role": role,
                "reason": f"unexpected membership {member} -> {granted}",
            }
        )
    return collisions


async def _require_no_role_memberships(
    connection: AsyncConnection,
    role: str,
) -> None:
    collisions = _membership_collisions(
        await _read_role_memberships(connection, role),
        role,
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


async def _dump_role_confinement_violations(
    connection: AsyncConnection,
    dump_role: str,
) -> list[str]:
    """Return capabilities that exceed the dedicated public-read contract."""
    rows = (
        await connection.execute(
            text(
                r"""
                WITH target AS (
                    SELECT r.oid
                    FROM pg_catalog.pg_roles AS r
                    WHERE r.rolname = :role
                ),
                violations AS (
                    SELECT 'owns database'::text AS kind,
                           pg_catalog.quote_ident(d.datname) AS object
                    FROM pg_catalog.pg_database AS d
                    CROSS JOIN target
                    WHERE d.datdba = target.oid

                    UNION ALL
                    SELECT 'database privilege',
                           pg_catalog.quote_ident(d.datname) || ' ' || acl.privilege_type
                    FROM pg_catalog.pg_database AS d
                    CROSS JOIN target
                    CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) AS acl
                    WHERE acl.grantee = target.oid
                      AND NOT (
                          d.datname = pg_catalog.current_database()
                          AND acl.privilege_type = 'CONNECT'
                          AND NOT acl.is_grantable
                      )

                    UNION ALL
                    SELECT 'effective database privilege',
                           pg_catalog.quote_ident(d.datname) || ' ' || privilege.name
                    FROM pg_catalog.pg_database AS d
                    CROSS JOIN target
                    CROSS JOIN LATERAL unnest(
                        ARRAY['CONNECT', 'CREATE', 'TEMPORARY']::text[]
                    ) AS privilege(name)
                    WHERE pg_catalog.has_database_privilege(
                              target.oid, d.oid, privilege.name
                          )
                      AND NOT (
                          privilege.name = 'CONNECT'
                          AND d.datname = pg_catalog.current_database()
                      )

                    UNION ALL
                    SELECT 'owns schema', pg_catalog.quote_ident(n.nspname)
                    FROM pg_catalog.pg_namespace AS n
                    CROSS JOIN target
                    WHERE n.nspowner = target.oid

                    UNION ALL
                    SELECT 'schema privilege',
                           pg_catalog.quote_ident(n.nspname) || ' ' || acl.privilege_type
                    FROM pg_catalog.pg_namespace AS n
                    CROSS JOIN target
                    CROSS JOIN LATERAL pg_catalog.aclexplode(n.nspacl) AS acl
                    WHERE acl.grantee = target.oid
                      AND NOT (
                          n.nspname = 'public'
                          AND acl.privilege_type = 'USAGE'
                          AND NOT acl.is_grantable
                      )

                    UNION ALL
                    SELECT 'effective public routine execute',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(p.proname) ||
                           '(' || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')'
                    FROM pg_catalog.pg_proc AS p
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                    CROSS JOIN target
                    WHERE n.nspname = 'public'
                      AND pg_catalog.has_function_privilege(
                          target.oid, p.oid, 'EXECUTE'
                      )

                    UNION ALL
                    SELECT 'effective non-public schema access',
                           pg_catalog.quote_ident(n.nspname)
                    FROM pg_catalog.pg_namespace AS n
                    CROSS JOIN target
                    WHERE n.nspname <> 'public'
                      AND n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
                      AND (
                          pg_catalog.has_schema_privilege(target.oid, n.oid, 'USAGE')
                          OR pg_catalog.has_schema_privilege(target.oid, n.oid, 'CREATE')
                      )

                    UNION ALL
                    SELECT 'owns relation',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(c.relname)
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN target
                    WHERE c.relowner = target.oid

                    UNION ALL
                    SELECT 'relation privilege',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(c.relname) || ' ' || acl.privilege_type
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN target
                    CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) AS acl
                    WHERE acl.grantee = target.oid
                      AND NOT (
                          n.nspname = 'public'
                          AND acl.privilege_type = 'SELECT'
                          AND NOT acl.is_grantable
                      )

                    UNION ALL
                    SELECT 'column privilege',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(c.relname) || '.' ||
                           pg_catalog.quote_ident(a.attname) || ' ' || acl.privilege_type
                    FROM pg_catalog.pg_attribute AS a
                    JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN target
                    CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) AS acl
                    WHERE acl.grantee = target.oid

                    UNION ALL
                    SELECT 'effective public table write',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(c.relname) || ' ' || privilege.name
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN target
                    CROSS JOIN LATERAL unnest(
                        CASE
                            WHEN pg_catalog.current_setting(
                                'server_version_num'
                            )::integer >= 170000
                            THEN ARRAY[
                                'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
                                'REFERENCES', 'TRIGGER', 'MAINTAIN'
                            ]::text[]
                            ELSE ARRAY[
                                'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
                                'REFERENCES', 'TRIGGER'
                            ]::text[]
                        END
                    ) AS privilege(name)
                    WHERE n.nspname = 'public'
                      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                      AND pg_catalog.has_table_privilege(
                          target.oid, c.oid, privilege.name
                      )

                    UNION ALL
                    SELECT 'effective public sequence write',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(c.relname) || ' ' || privilege.name
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN target
                    CROSS JOIN LATERAL unnest(
                        ARRAY['USAGE', 'UPDATE']::text[]
                    ) AS privilege(name)
                    WHERE n.nspname = 'public'
                      AND c.relkind = 'S'
                      AND pg_catalog.has_sequence_privilege(
                          target.oid, c.oid, privilege.name
                      )

                    UNION ALL
                    SELECT 'effective database create',
                           pg_catalog.quote_ident(pg_catalog.current_database())
                    FROM target
                    WHERE pg_catalog.has_database_privilege(
                        target.oid, pg_catalog.current_database(), 'CREATE'
                    )

                    UNION ALL
                    SELECT 'effective public schema create',
                           pg_catalog.quote_ident('public')
                    FROM pg_catalog.pg_namespace AS n
                    CROSS JOIN target
                    WHERE n.nspname = 'public'
                      AND pg_catalog.has_schema_privilege(
                          target.oid, n.oid, 'CREATE'
                      )

                    UNION ALL
                    SELECT 'owns routine',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(p.proname)
                    FROM pg_catalog.pg_proc AS p
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                    CROSS JOIN target
                    WHERE p.proowner = target.oid

                    UNION ALL
                    SELECT 'routine privilege',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(p.proname) || ' ' || acl.privilege_type
                    FROM pg_catalog.pg_proc AS p
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                    CROSS JOIN target
                    CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) AS acl
                    WHERE acl.grantee = target.oid

                    UNION ALL
                    SELECT 'owns type',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(t.typname)
                    FROM pg_catalog.pg_type AS t
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
                    CROSS JOIN target
                    WHERE t.typowner = target.oid
                      AND t.typtype <> 'p'

                    UNION ALL
                    SELECT 'type privilege',
                           pg_catalog.quote_ident(n.nspname) || '.' ||
                           pg_catalog.quote_ident(t.typname) || ' ' || acl.privilege_type
                    FROM pg_catalog.pg_type AS t
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
                    CROSS JOIN target
                    CROSS JOIN LATERAL pg_catalog.aclexplode(t.typacl) AS acl
                    WHERE acl.grantee = target.oid

                    UNION ALL
                    SELECT 'default privilege',
                           COALESCE(pg_catalog.quote_ident(n.nspname), '<all schemas>') ||
                           ' ' || da.defaclobjtype::text || ' ' || acl.privilege_type
                    FROM pg_catalog.pg_default_acl AS da
                    LEFT JOIN pg_catalog.pg_namespace AS n
                           ON n.oid = da.defaclnamespace
                    CROSS JOIN target
                    CROSS JOIN LATERAL pg_catalog.aclexplode(da.defaclacl) AS acl
                    WHERE acl.grantee = target.oid
                      AND NOT (
                          n.nspname = 'public'
                          AND da.defaclobjtype IN ('r', 'S')
                          AND acl.privilege_type = 'SELECT'
                          AND NOT acl.is_grantable
                      )

                    UNION ALL
                    SELECT 'owns large object', lom.oid::text
                    FROM pg_catalog.pg_largeobject_metadata AS lom
                    CROSS JOIN target
                    WHERE lom.lomowner = target.oid

                    UNION ALL
                    SELECT 'large object privilege',
                           lom.oid::text || ' ' || acl.privilege_type
                    FROM pg_catalog.pg_largeobject_metadata AS lom
                    CROSS JOIN target
                    CROSS JOIN LATERAL pg_catalog.aclexplode(lom.lomacl) AS acl
                    WHERE acl.grantee = target.oid
                )
                SELECT kind, object
                FROM violations
                ORDER BY kind, object
                LIMIT 50
                """
            ),
            {"role": dump_role},
        )
    ).mappings().all()
    return [f"{row['kind']}: {row['object']}" for row in rows]


_REPAIRABLE_DUMP_CONFINEMENT_PREFIXES = (
    "database privilege:",
    "effective database privilege:",
    "effective database create:",
    "effective public schema create:",
    "effective public table write:",
    "effective public sequence write:",
    "effective public routine execute:",
)


def _repairable_dump_confinement_violation(violation: str) -> bool:
    return violation.startswith(_REPAIRABLE_DUMP_CONFINEMENT_PREFIXES)


async def build_provision_plan(settings: Any) -> dict[str, Any]:
    """Plan optional hardening with one read-only dump role.

    The normal backup and destructive-restore path needs no provisioning. This
    command exists only for operators that want pg_dump to authenticate as a
    separate least-privilege login.
    """
    dump_role = resolve_dump_role_name(settings)
    runtime_url = str(settings.database_url)
    runtime_role = validate_postgres_identifier(
        sqlalchemy_url_to_libpq(runtime_url).username,
        label="runtime role",
    )
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
                            "AS server_version_num, r.rolsuper AS runtime_superuser "
                            "FROM pg_catalog.pg_roles AS r "
                            "WHERE r.rolname = current_user"
                        )
                    )
                ).mappings().one()
                dump_row = await _read_role_row(connection, dump_role)
                memberships = await _read_role_memberships(connection, dump_role)
                confinement_violations = (
                    await _dump_role_confinement_violations(connection, dump_role)
                    if dump_row is not None
                    else []
                )
                confinement_violations = [
                    violation
                    for violation in confinement_violations
                    if not _repairable_dump_confinement_violation(violation)
                ]
                rls_count = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_catalog.pg_class AS c "
                                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                                "WHERE c.relkind IN ('r', 'p') AND c.relrowsecurity "
                                "AND n.nspname = 'public'"
                            )
                        )
                    ).scalar_one()
                )
    finally:
        await engine.dispose()

    if str(identity["role"]) != runtime_role:
        raise BackupProvisionError("runtime DSN authenticated as an unexpected role")

    actions: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    collisions: list[dict[str, str]] = []
    if bool(identity["runtime_superuser"]):
        warnings.append(
            {
                "code": RUNTIME_SUPERUSER_CODE,
                "detail": RUNTIME_SUPERUSER_DETAIL,
                "role": str(identity["role"]),
            }
        )
    if memberships:
        rendered = ", ".join(
            f"{row['member']} -> {row['granted']}" for row in memberships
        )
        collisions.append(
            {
                "role": dump_role,
                "reason": f"dump role has unexpected memberships: {rendered}",
            }
        )
    collisions.extend(
        {"role": dump_role, "reason": violation}
        for violation in confinement_violations
    )
    if rls_count:
        collisions.append(
            {
                "role": dump_role,
                "reason": (
                    "row-level-security tables require PPBASE_DATABASE_URL "
                    "for complete dumps"
                ),
            }
        )
    dump_spec = {"login": True, "createdb": False, "inherit": False}
    if dump_row is None:
        actions.append({"action": "create_dump_role", "role": dump_role})
    else:
        collision = _role_collision(dump_row, dump_spec)
        if collision:
            collisions.append({"role": dump_role, "reason": collision})
        else:
            actions.append({"action": "reuse_dump_role", "role": dump_role})
    actions.extend(
        (
            {"action": "normalize_public_database_acl", "role": dump_role},
            {"action": "grant_dump_read_access", "role": dump_role},
            {"action": "write_dump_credential", "required": True},
        )
    )
    return {
        "formatVersion": 1,
        "mode": "optional_dump_hardening",
        "readOnly": True,
        "runtime": {
            "url": _redacted_url(runtime_url),
            "role": str(identity["role"]),
            "database": str(identity["database"]),
            "serverVersionNum": int(identity["server_version_num"]),
        },
        "roles": {
            "runtime": str(identity["role"]),
            "dump": dump_role,
        },
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
    row = await _read_role_row(connection, role)
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
    schema = _quote_identifier("public")
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
                "SELECT DISTINCT pg_catalog.pg_get_userbyid(c.relowner) AS owner "
                "FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r','p','S','v','m','f') "
                "AND n.nspname = 'public' ORDER BY owner"
            )
        )
    ).mappings().all()
    owners = {str(row["owner"]) for row in owner_rows}
    owners.update(future_owners)
    for owner_name in sorted(owners):
        owner = _quote_identifier(owner_name)
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


async def _normalize_source_dump_access(
    connection: AsyncConnection,
    *,
    dump_role: str,
    future_owners: tuple[str, ...] = (),
) -> None:
    """Normalize access for the public schema selected by ``pg_dump``."""
    dump = _quote_identifier(dump_role)
    schema = _quote_identifier("public")
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {schema} FROM {dump}")
    )
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC")
    )
    await connection.execute(
        text(
            f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {schema} "
            f"FROM {dump}"
        )
    )
    await connection.execute(
        text(
            f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {schema} "
            "FROM PUBLIC"
        )
    )
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON SCHEMA {schema} FROM {dump}")
    )
    await connection.execute(
        text(f"REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC")
    )
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA {schema} FROM {dump}")
    )
    await connection.execute(
        text(f"REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA {schema} FROM PUBLIC")
    )

    owner_rows = (
        await connection.execute(
            text(
                "SELECT DISTINCT owner FROM ("
                "SELECT pg_catalog.pg_get_userbyid(n.nspowner) AS owner "
                "FROM pg_catalog.pg_namespace AS n WHERE n.nspname = 'public' "
                "UNION ALL "
                "SELECT pg_catalog.pg_get_userbyid(c.relowner) AS owner "
                "FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r','p','S','v','m','f') "
                "AND n.nspname = 'public' "
                "UNION ALL "
                "SELECT pg_catalog.pg_get_userbyid(p.proowner) AS owner "
                "FROM pg_catalog.pg_proc AS p "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public'"
                ") AS public_owners ORDER BY owner"
            )
        )
    ).mappings().all()
    owners = {str(row["owner"]) for row in owner_rows}
    owners.update(future_owners)
    for owner_name in sorted(owners):
        owner = _quote_identifier(owner_name)
        await connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
                f"REVOKE ALL PRIVILEGES ON TABLES FROM {dump}"
            )
        )
        await connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
                f"REVOKE ALL PRIVILEGES ON SEQUENCES FROM {dump}"
            )
        )
        await connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
                "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC"
            )
        )
        await connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
                "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC"
            )
        )
        await connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
                "REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC"
            )
        )

    await _grant_source_dump_access(
        connection,
        dump_role=dump_role,
        future_owners=future_owners,
    )


async def _normalize_dump_database_access(
    connection: AsyncConnection,
    *,
    dump_role: str,
    active_database: str,
) -> None:
    """Remove inherited cluster database ACLs, then allow only the active DB."""
    dump = _quote_identifier(dump_role)
    rows = (
        await connection.execute(
            text("SELECT d.datname FROM pg_catalog.pg_database AS d ORDER BY d.datname")
        )
    ).mappings().all()
    for row in rows:
        database = _quote_identifier(str(row["datname"]))
        await connection.execute(
            text(f"REVOKE ALL PRIVILEGES ON DATABASE {database} FROM {dump}")
        )
        await connection.execute(
            text(f"REVOKE ALL PRIVILEGES ON DATABASE {database} FROM PUBLIC")
        )
    await connection.execute(
        text(
            f"GRANT CONNECT ON DATABASE {_quote_identifier(active_database)} "
            f"TO {dump}"
        )
    )


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
    password: str,
) -> dict[str, str]:
    bootstrap = _validated_bootstrap_url(bootstrap_database_url)
    if not password:
        raise BackupProvisionError("runtime credential is missing")
    return {
        "PPBASE_DATABASE_URL": bootstrap.set(
            username=spec.runtime_role,
            password=password,
            database=spec.database,
        ).render_as_string(hide_password=False),
    }


def _validate_postgres_init_values(
    values: Mapping[str, str],
    bootstrap_database_url: str | URL,
    spec: PostgresInitSpec,
) -> str:
    if set(values) != INIT_SECRET_KEYS:
        raise BackupProvisionError(
            "secret sink must contain exactly PPBASE_DATABASE_URL"
        )
    bootstrap = _validated_bootstrap_url(bootstrap_database_url)
    bootstrap_connection = sqlalchemy_url_to_libpq(bootstrap)
    try:
        parsed = make_url(str(values["PPBASE_DATABASE_URL"]))
        parsed_connection = sqlalchemy_url_to_libpq(parsed)
    except Exception:
        raise BackupProvisionError(
            "secret sink contains an invalid PPBASE_DATABASE_URL"
        ) from None
    if (
        parsed.drivername != "postgresql+asyncpg"
        or parsed.username != spec.runtime_role
        or parsed.database != spec.database
        or parsed_connection.host != bootstrap_connection.host
        or (parsed_connection.port or "5432")
        != (bootstrap_connection.port or "5432")
        or not parsed.password
    ):
        raise BackupProvisionError(
            "secret sink runtime credential does not match this project/server"
        )
    return str(parsed.password)


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
    role_exists: bool,
    bootstrap_database: str,
    database_exists: bool,
) -> list[dict[str, str]]:
    if not role_exists:
        return []
    configured = make_url(str(values["PPBASE_DATABASE_URL"]))
    probe_url = configured if database_exists else configured.set(
        database=bootstrap_database
    )
    engine = create_async_engine(probe_url, poolclass=NullPool)
    authenticated = False
    try:
        try:
            async with engine.connect() as connection:
                effective = str(
                    (await connection.execute(text("SELECT current_user"))).scalar_one()
                )
                authenticated = effective == spec.runtime_role
                await connection.rollback()
        except Exception as exc:
            # PostgreSQL checks CONNECT after authentication. A 42501 here
            # therefore still proves that the stored password is valid.
            authenticated = _postgres_error_sqlstate(exc) == "42501"
    finally:
        await engine.dispose()
    if authenticated:
        return []
    return [
        {
            "resource": spec.runtime_role,
            "reason": "stored credential PPBASE_DATABASE_URL could not authenticate",
        }
    ]


def _dump_provision_values(
    settings: Any,
    *,
    dump_role: str,
    password: str | None,
) -> dict[str, str]:
    configured = str(
        getattr(settings, "backup_dump_database_url", "") or ""
    ).strip()
    if password is None:
        if not configured:
            raise BackupProvisionError(
                f"existing role {dump_role!r} requires its configured dump DSN"
            )
        value = configured
    else:
        value = make_url(str(settings.database_url)).set(
            username=dump_role,
            password=password,
        ).render_as_string(hide_password=False)
    return {"PPBASE_BACKUP_DUMP_DATABASE_URL": value}


def _validate_dump_provision_values(
    values: Mapping[str, str],
    runtime_database_url: str | URL,
    dump_role: str,
) -> str:
    if set(values) != DUMP_PROVISION_SECRET_KEYS:
        raise BackupProvisionError(
            "secret sink must contain exactly PPBASE_BACKUP_DUMP_DATABASE_URL"
        )
    runtime = make_url(str(runtime_database_url))
    try:
        configured = make_url(str(values["PPBASE_BACKUP_DUMP_DATABASE_URL"]))
        runtime_connection = sqlalchemy_url_to_libpq(runtime)
        configured_connection = sqlalchemy_url_to_libpq(configured)
    except Exception:
        raise BackupProvisionError("secret sink contains an invalid dump DSN") from None
    if (
        configured.drivername != runtime.drivername
        or configured.username != dump_role
        or configured.database != runtime.database
        or configured_connection.host != runtime_connection.host
        or (configured_connection.port or "5432")
        != (runtime_connection.port or "5432")
        or not configured.password
    ):
        raise BackupProvisionError(
            "secret sink dump credential does not match this runtime/server"
        )
    return str(configured.password)


async def _require_dump_provision_credential(
    database_url: str,
    *,
    dump_role: str,
) -> None:
    """Authenticate a pre-existing role before changing any of its grants."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        try:
            async with engine.connect() as connection:
                effective = str(
                    (
                        await connection.execute(
                            text(
                                "SELECT session_user AS session_role, "
                                "current_user AS effective_role"
                            )
                        )
                    ).mappings().one()["session_role"]
                )
                await connection.rollback()
        except Exception:
            raise BackupProvisionError(
                f"stored credential for existing role {dump_role!r} could not authenticate"
            ) from None
    finally:
        await engine.dispose()
    if effective != dump_role:
        raise BackupProvisionError(
            f"stored credential authenticated as {effective!r}, not {dump_role!r}"
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
    if str(row["owner"]) != spec.runtime_role:
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
                if str(database["owner"]) != spec.runtime_role:
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
    """Create/configure only the optional least-privilege dump login."""
    plan = await build_provision_plan(settings)
    if not plan["executable"]:
        raise BackupProvisionError("provisioning plan contains unsafe role collisions")
    dump_role = resolve_dump_role_name(settings)
    runtime_role = validate_postgres_identifier(
        sqlalchemy_url_to_libpq(settings.database_url).username,
        label="runtime role",
    )
    bootstrap = _validated_bootstrap_url(bootstrap_database_url)
    runtime = make_url(str(settings.database_url))
    bootstrap_connection = sqlalchemy_url_to_libpq(bootstrap)
    runtime_connection = sqlalchemy_url_to_libpq(runtime)
    if (
        bootstrap_connection.host != runtime_connection.host
        or (bootstrap_connection.port or "5432")
        != (runtime_connection.port or "5432")
    ):
        raise BackupProvisionError("bootstrap and runtime DSNs must target the same server")
    bootstrap_runtime = bootstrap.set(database=runtime.database)
    sink_path = Path(secret_sink).expanduser().absolute()
    values: dict[str, str] | None = None
    warnings = list(plan.get("warnings", []))

    engine = create_async_engine(bootstrap_runtime, poolclass=NullPool)
    created = False
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await set_backup_control_search_path(connection)
                await connection.execute(
                    text("SELECT pg_catalog.pg_advisory_xact_lock(:key)"),
                    {"key": PROVISION_LOCK_KEY},
                )
                effective = str(
                    (await connection.execute(text("SELECT current_user"))).scalar_one()
                )
                is_superuser = bool(
                    (
                        await connection.execute(
                            text(
                                "SELECT r.rolsuper "
                                "FROM pg_catalog.pg_roles AS r WHERE r.rolname = :role"
                            ),
                            {"role": effective},
                        )
                    ).scalar_one()
                )
                if not is_superuser:
                    raise BackupProvisionError(
                        "backup provision requires an ephemeral PostgreSQL superuser DSN"
                    )

                existing_row = (
                    await connection.execute(
                        text(
                            "SELECT r.rolname, r.rolcanlogin, r.rolcreatedb, "
                            "r.rolinherit, r.rolsuper, r.rolcreaterole, "
                            "r.rolreplication, r.rolbypassrls "
                            "FROM pg_catalog.pg_roles AS r "
                            "WHERE r.rolname = :role"
                        ),
                        {"role": dump_role},
                    )
                ).mappings().one_or_none()
                values = read_secret_sink(sink_path)
                sink_missing = values is None
                if values is None:
                    values = _dump_provision_values(
                        settings,
                        dump_role=dump_role,
                        password=(
                            None if existing_row is not None else _generate_password()
                        ),
                    )
                password = _validate_dump_provision_values(
                    values,
                    runtime,
                    dump_role,
                )
                if existing_row is not None:
                    await _require_dump_provision_credential(
                        values["PPBASE_BACKUP_DUMP_DATABASE_URL"],
                        dump_role=dump_role,
                    )
                if sink_missing:
                    write_secret_sink(sink_path, values)
                created = await _ensure_role(
                    connection,
                    dump_role,
                    {"login": True, "createdb": False, "inherit": False},
                    password=password,
                )
                memberships = (
                    await connection.execute(
                        text(
                            "SELECT member_role.rolname AS member, "
                            "granted_role.rolname AS granted "
                            "FROM pg_catalog.pg_auth_members AS membership "
                            "JOIN pg_catalog.pg_roles AS member_role "
                            "ON member_role.oid = membership.member "
                            "JOIN pg_catalog.pg_roles AS granted_role "
                            "ON granted_role.oid = membership.roleid "
                            "WHERE member_role.rolname = :role "
                            "OR granted_role.rolname = :role"
                        ),
                        {"role": dump_role},
                    )
                ).mappings().all()
                if memberships:
                    raise BackupProvisionError(
                        "dump role has unexpected PostgreSQL memberships"
                    )
                await _normalize_dump_database_access(
                    connection,
                    dump_role=dump_role,
                    active_database=str(runtime.database),
                )
                await _normalize_source_dump_access(
                    connection,
                    dump_role=dump_role,
                    future_owners=(runtime_role,),
                )
                confinement_violations = await _dump_role_confinement_violations(
                    connection,
                    dump_role,
                )
                if confinement_violations:
                    raise BackupProvisionError(
                        "dump role exceeds the public read-only contract: "
                        + "; ".join(confinement_violations)
                    )
    except BackupProvisionError:
        raise
    except Exception:
        raise BackupProvisionError(
            "backup provisioning failed; rerun the same command to resume"
        ) from None
    finally:
        await engine.dispose()

    if values is None:
        raise BackupProvisionError("limited dump credential was not prepared")

    dump_engine = create_async_engine(
        values["PPBASE_BACKUP_DUMP_DATABASE_URL"],
        poolclass=NullPool,
    )
    try:
        async with dump_engine.connect() as connection:
            report = await preflight_dump_role(connection)
            await connection.rollback()
    except Exception:
        raise BackupProvisionError(
            "dump role verification failed after provisioning"
        ) from None
    finally:
        await dump_engine.dispose()
    if report.errors:
        raise BackupProvisionError(
            f"dump role verification failed: {'; '.join(report.errors)}"
        )
    warnings.extend(
        {"code": "dump_role_warning", "detail": str(detail)}
        for detail in report.warnings
    )

    return {
        "formatVersion": 1,
        "mode": "optional_dump_hardening",
        "createdRoles": [dump_role] if created else [],
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
    """Inspect creation of one database and one runtime role without mutations."""
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
                runtime_row = await _read_role_row(connection, spec.runtime_role)
                memberships = await _read_role_memberships(
                    connection,
                    spec.runtime_role,
                )
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
                role_exists=runtime_row is not None,
                bootstrap_database=str(identity["database"]),
                database_exists=database_row is not None,
            )
        )
    privileged = bool(identity["rolsuper"])
    if not privileged:
        collisions.append(
            {
                "resource": "bootstrap_role",
                "reason": "init postgres requires an ephemeral PostgreSQL superuser DSN",
            }
        )

    runtime_marker_valid = True
    runtime_spec = {"login": True, "createdb": False, "inherit": True}
    if runtime_row is None:
        runtime_marker_valid = False
        actions.append(
            {"action": "create_runtime_role", "role": spec.runtime_role}
        )
    else:
        collision = _role_collision(runtime_row, runtime_spec)
        if collision:
            runtime_marker_valid = False
            collisions.append(
                {"resource": spec.runtime_role, "reason": collision}
            )
        elif str(runtime_row.get("marker") or "") != _postgres_init_role_marker(
            spec
        ):
            runtime_marker_valid = False
            collisions.append(
                {
                    "resource": spec.runtime_role,
                    "reason": "role is not marked as created by PPBase init",
                }
            )
        elif configured_values is None:
            runtime_marker_valid = False
            collisions.append(
                {
                    "resource": spec.runtime_role,
                    "reason": "existing login role has no reusable credential sink",
                }
            )
        else:
            actions.append(
                {"action": "noop_runtime_role", "role": spec.runtime_role}
            )

    collisions.extend(
        {
            "resource": item["role"],
            "reason": item["reason"],
        }
        for item in _membership_collisions(memberships, spec.runtime_role)
    )
    if database_row is None:
        actions.append({"action": "create_database", "database": spec.database})
    elif (
        not str(database_row.get("marker") or "")
        and configured_values is not None
        and runtime_marker_valid
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
            {"action": "normalize_database_acl", "database": spec.database},
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
                f"WITH OWNER {_quote_identifier(spec.runtime_role)} "
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
    """Apply the runtime-owned database contract without backup-only roles."""
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
    finally:
        await bootstrap_engine.dispose()

    runtime_engine = create_async_engine(values["PPBASE_DATABASE_URL"], poolclass=NullPool)
    try:
        async with runtime_engine.begin() as connection:
            await set_backup_control_search_path(connection)
            effective = str(
                (await connection.execute(text("SELECT current_user"))).scalar_one()
            )
            if effective != spec.runtime_role:
                raise BackupProvisionError(
                    "runtime credential authenticated as an unexpected role"
                )
            await connection.execute(
                text("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            )
            restore_report = await preflight_destructive_restore_role(connection)
            if not restore_report.ok:
                raise BackupProvisionError(
                    "runtime role preflight failed: "
                    + "; ".join(restore_report.errors)
                )
    finally:
        await runtime_engine.dispose()
    return False


async def execute_postgres_init(
    settings: Any,
    *,
    bootstrap_database_url: str,
    project_name: str,
    secret_sink: str | Path,
) -> dict[str, Any]:
    """Create or resume one runtime role and its owned PPBase database."""
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
            password = _generate_password()
            values = _postgres_init_values(bootstrap, spec, password)
            write_secret_sink(sink_path, values)
        else:
            password = _validate_postgres_init_values(values, bootstrap, spec)

        created_roles: list[str] = []
        async with engine.begin() as connection:
            await set_backup_control_search_path(connection)
            created = await _ensure_role(
                connection,
                spec.runtime_role,
                {"login": True, "createdb": False, "inherit": True},
                password=password,
            )
            if created:
                created_roles.append(spec.runtime_role)
                await connection.execute(
                    text(
                        f"COMMENT ON ROLE {_quote_identifier(spec.runtime_role)} IS "
                        f"{_sql_literal(_postgres_init_role_marker(spec))}"
                    )
                )
            marked_row = await _read_role_row(connection, spec.runtime_role)
            if str((marked_row or {}).get("marker") or "") != (
                _postgres_init_role_marker(spec)
            ):
                raise BackupProvisionError(
                    f"managed role {spec.runtime_role!r} is detached from "
                    "the PPBase init marker"
                )
            await _require_no_role_memberships(connection, spec.runtime_role)

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
            await _require_no_role_memberships(connection, spec.runtime_role)
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
        if selected == Path(selected.anchor):
            raise ControlPlaneSafetyError("filesystem root is not a private root")
        current = Path(selected.anchor)
        for index, component in enumerate(selected.parts[1:]):
            parent = os.lstat(current)
            parent_mode = stat.S_IMODE(parent.st_mode)
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid not in {0, os.geteuid()}
                or (parent_mode & 0o022 and not parent_mode & stat.S_ISVTX)
            ):
                raise ControlPlaneSafetyError("unsafe directory ancestry")
            candidate = current / component
            try:
                opened = os.lstat(candidate)
            except FileNotFoundError:
                if not os.access(current, os.W_OK | os.X_OK):
                    raise ControlPlaneSafetyError(
                        "nearest existing parent is not writable"
                    )
                return {
                    "name": label,
                    "ready": True,
                    "status": "warn",
                    "detail": "directory is absent and will be created as private 0700",
                    "path": str(selected),
                }
            if stat.S_ISLNK(opened.st_mode) or not stat.S_ISDIR(opened.st_mode):
                raise ControlPlaneSafetyError("unsafe symlink or directory entry")
            if parent_mode & stat.S_ISVTX and opened.st_uid != os.geteuid():
                raise ControlPlaneSafetyError("unsafe sticky-directory ownership")
            current = candidate
            if index == len(selected.parts[1:]) - 1 and (
                opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise ControlPlaneSafetyError("directory is not owned private 0700")
        detail = "owned private 0700 directory available"
        ready = True
    except (ControlPlaneSafetyError, OSError):
        detail = "directory is unsafe, symlinked, or not private 0700"
        ready = False
    return {
        "name": label,
        "ready": ready,
        "detail": detail,
        "path": str(selected),
    }


def _root_layout_check(settings: Any) -> dict[str, Any]:
    """Mirror the service's no-overlap invariant without creating paths."""
    try:
        data_dir = Path(settings.data_dir).expanduser().resolve(strict=False)
        roots = {
            "backup_root": Path(settings.backup_root).expanduser().resolve(strict=False),
            "control_plane": Path(settings.backup_control_dir)
            .expanduser()
            .resolve(strict=False),
        }
        conflicts: list[str] = []
        for name, root in roots.items():
            if (
                root == data_dir
                or root.is_relative_to(data_dir)
                or data_dir.is_relative_to(root)
            ):
                conflicts.append(f"{name} overlaps data_dir")
        items = list(roots.items())
        for index, (left_name, left) in enumerate(items):
            for right_name, right in items[index + 1 :]:
                if (
                    left == right
                    or left.is_relative_to(right)
                    or right.is_relative_to(left)
                ):
                    conflicts.append(f"{left_name} overlaps {right_name}")
        ready = not conflicts
        detail = (
            "data_dir, backup_root, and control_plane do not overlap"
            if ready
            else "; ".join(conflicts)
        )
    except (OSError, RuntimeError):
        ready = False
        detail = "filesystem root layout could not be resolved safely"
    return {
        "name": "root_layout",
        "ready": ready,
        "detail": detail,
    }


async def _postgres_server_identity(connection: AsyncConnection) -> dict[str, Any]:
    """Return fields that bind a DSN to one database on one server instance."""
    await set_backup_control_search_path(connection)
    row = (
        await connection.execute(
            text(
                "SELECT current_user AS role, "
                "pg_catalog.current_database() AS database, "
                "COALESCE(pg_catalog.inet_server_addr()::text, '') AS server_address, "
                "COALESCE(pg_catalog.inet_server_port(), 0) AS server_port, "
                "EXTRACT(EPOCH FROM pg_catalog.pg_postmaster_start_time())::text "
                "AS postmaster_started_at, "
                "pg_catalog.current_setting('server_version_num')::integer AS version"
            )
        )
    ).mappings().one()
    return {
        "role": str(row["role"]),
        "database": str(row["database"]),
        "server_address": str(row["server_address"]),
        "server_port": int(row["server_port"]),
        "postmaster_started_at": str(row["postmaster_started_at"]),
        "version": int(row["version"]),
    }


def _postgres_server_instance_key(identity: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        identity["server_address"],
        identity["server_port"],
        identity["postmaster_started_at"],
        identity["version"],
    )


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
        (parsed.scheme, parsed.netloc, "/api/health/backup-restart", "", "")
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
            != "Backup restart capability inspected."
        ):
            raise KeyError("invalid PPBase health envelope")
        configured = decoded["data"]["restart"]["configured"]
    except (KeyError, TypeError):
        configured = None
    if configured is True:
        return {
            "name": "restart",
            "ready": True,
            "status": "pass",
            "detail": "running PPBase server confirms destructive-restore restart support",
        }
    if configured is False:
        return {
            "name": "restart",
            "ready": False,
            "status": "fail",
            "detail": "running PPBase server confirms destructive-restore restart is unavailable",
        }
    return {
        "name": "restart",
        "ready": False,
        "status": "fail",
        "detail": "running server returned no authoritative restart capability",
    }


async def backup_doctor(
    settings: Any,
    *,
    server_url: str | None = None,
) -> dict[str, Any]:
    """Check the normal runtime path plus optional dump-role hardening."""
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
    checks.extend(
        (
            _root_check(settings.backup_root, label="backup_root"),
            _root_check(settings.backup_control_dir, label="control_plane"),
            _root_layout_check(settings),
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

    runtime_url = str(settings.database_url)
    runtime_engine = create_async_engine(runtime_url, poolclass=NullPool)
    server_major: int | None = None
    runtime_identity: dict[str, Any] | None = None
    try:
        async with runtime_engine.connect() as connection:
            runtime_identity = await _postgres_server_identity(connection)
            server_major = int(runtime_identity["version"]) // 10000
            try:
                restore_report = await preflight_destructive_restore_role(connection)
            except Exception:
                runtime_check: dict[str, Any] = {
                    "name": "runtime_role",
                    "ready": False,
                    "detail": "destructive-restore ownership preflight failed",
                }
            else:
                runtime_check = {
                    "name": "runtime_role",
                    "ready": restore_report.ok,
                    "detail": (
                        "ready for in-place destructive restore"
                        if restore_report.ok
                        else "; ".join(restore_report.errors)
                    ),
                }
                if restore_report.ok and restore_report.warnings:
                    runtime_check["status"] = "warn"
                    runtime_check["detail"] = "; ".join(restore_report.warnings)
                for detail in restore_report.warnings:
                    warning = {
                        "code": "runtime_restore_warning",
                        "detail": str(detail),
                        "role": str(runtime_identity["role"]),
                    }
                    if "superuser" in str(detail).lower():
                        warning["code"] = RUNTIME_SUPERUSER_CODE
                        warning["detail"] = RUNTIME_SUPERUSER_DETAIL
                    warnings.append(warning)

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
            await connection.rollback()
            checks.extend(
                (
                    {
                        "name": "runtime_database",
                        "ready": True,
                        "detail": (
                            f"{runtime_identity['role']}@{runtime_identity['database']} "
                            f"PostgreSQL {runtime_identity['version']}"
                        ),
                    },
                    runtime_check,
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
                    },
                )
            )
    except Exception:
        checks.append(
            {
                "name": "runtime_database",
                "ready": False,
                "detail": "runtime database connection or inspection failed",
            }
        )
    finally:
        await runtime_engine.dispose()

    tool_map = {
        "pg_dump": getattr(settings, "backup_pg_dump_path", None),
        "pg_restore": getattr(settings, "backup_pg_restore_path", None),
        "psql": getattr(settings, "backup_psql_path", None),
    }
    for name, configured in tool_map.items():
        try:
            resolved = resolve_postgres_tool(name, configured)
        except PostgresToolResolutionError as exc:
            checks.append({"name": name, "ready": False, "detail": str(exc)})
            continue
        try:
            version = await detect_postgres_tool_version(resolved)
            compatible = server_major is not None and version.major >= server_major
            checks.append(
                {
                    "name": name,
                    "ready": compatible,
                    "detail": (
                        f"client major {version.major} supports server major {server_major}"
                        if compatible
                        else (
                            f"client major {version.major} is older than "
                            f"server major {server_major}"
                        )
                    ),
                    "path": resolved,
                }
            )
        except PostgresBackupError:
            checks.append(
                {"name": name, "ready": False, "detail": "version detection failed"}
            )

    configured_dump_url = str(
        getattr(settings, "backup_dump_database_url", "") or ""
    ).strip()
    dump_url = configured_dump_url or runtime_url
    dump_source = (
        "dedicated PPBASE_BACKUP_DUMP_DATABASE_URL"
        if configured_dump_url
        else "PPBASE_DATABASE_URL"
    )
    dump_engine = create_async_engine(dump_url, poolclass=NullPool)
    try:
        async with dump_engine.connect() as connection:
            dump_identity = await _postgres_server_identity(connection)
            configured_dump_role = validate_postgres_identifier(
                sqlalchemy_url_to_libpq(dump_url).username,
                label="dump role",
            )
            identity_errors: list[str] = []
            if runtime_identity is None:
                identity_errors.append("runtime database identity is unavailable")
            else:
                if dump_identity["database"] != runtime_identity["database"]:
                    identity_errors.append("DSN reached a different database")
                if _postgres_server_instance_key(
                    dump_identity
                ) != _postgres_server_instance_key(runtime_identity):
                    identity_errors.append("DSN reached a different PostgreSQL server")
            if dump_identity["role"] != configured_dump_role:
                identity_errors.append("DSN login does not match current_user")
            report = await preflight_dump_role(connection)
            dump_check: dict[str, Any] = {
                "name": "dump_role",
                "ready": report.ok and not identity_errors,
                "detail": (
                    f"{dump_source}: ready"
                    if report.ok and not identity_errors
                    else f"{dump_source}: "
                    + "; ".join((*identity_errors, *report.errors))
                ),
            }
            if report.ok and not identity_errors and report.warnings:
                dump_check["status"] = "warn"
                dump_check["detail"] = (
                    f"{dump_source}: " + "; ".join(report.warnings)
                )
            checks.append(dump_check)
            for detail in report.warnings:
                warnings.append(
                    {
                        "code": "dump_role_warning",
                        "detail": str(detail),
                    }
                )
            await connection.rollback()
    except Exception:
        checks.append(
            {
                "name": "dump_role",
                "ready": False,
                "detail": f"{dump_source}: connection or privilege preflight failed",
            }
        )
    finally:
        await dump_engine.dispose()

    backup_checks = {
        "storage",
        "backup_root",
        "control_plane",
        "root_layout",
        "runtime_database",
        "extensions",
        "pg_dump",
        "dump_role",
    }
    restore_checks = {
        "storage",
        "backup_root",
        "control_plane",
        "root_layout",
        "restart",
        "runtime_database",
        "runtime_role",
        "extensions",
        "pg_restore",
        "psql",
    }
    for check in checks:
        check.setdefault("status", "pass" if check.get("ready") else "fail")
        name = str(check.get("name", ""))
        check["operations"] = [
            operation
            for operation, names in (
                ("backup", backup_checks),
                ("restore", restore_checks),
            )
            if name in names
        ]

    def operation_ready(names: set[str]) -> bool:
        return all(
            str(check["status"]) != "fail"
            for check in checks
            if str(check.get("name", "")) in names
        )

    def operation_fully_verified(names: set[str]) -> bool:
        return all(
            str(check["status"]) == "pass"
            for check in checks
            if str(check.get("name", "")) in names
        )

    backup_ready = operation_ready(backup_checks)
    restore_ready = operation_ready(restore_checks)
    backup_fully_verified = operation_fully_verified(backup_checks)
    restore_fully_verified = operation_fully_verified(restore_checks)
    return {
        "formatVersion": 1,
        # Keep the historical top-level fields scoped to the command's backup
        # exit status. Restore readiness is independent and must not make a
        # healthy backup path look unconfigured.
        "ready": backup_ready,
        "fullyVerified": backup_fully_verified,
        "backupReady": backup_ready,
        "backupFullyVerified": backup_fully_verified,
        "restoreReady": restore_ready,
        "restoreFullyVerified": restore_fully_verified,
        "exitCode": DOCTOR_EXIT_READY if backup_ready else DOCTOR_EXIT_NOT_READY,
        "checks": checks,
        "warnings": warnings,
        "commands": {
            "optionalDumpRolePlan": "ppbase backup provision --plan",
            "optionalDumpRoleExecute": (
                "ppbase backup provision --execute "
                "--output-env ./ppbase-backup.env"
            ),
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
    backup_ready = bool(report.get("backupReady", report.get("ready")))
    restore_ready = bool(report.get("restoreReady", report.get("ready")))
    if not backup_ready:
        lines.append("Backup is not ready; fix the backup failures above.")
    elif not restore_ready:
        lines.append(
            "Backup is ready; destructive restore is not ready. "
            "Fix the restore-specific failures above."
        )
    elif report.get("warnings"):
        lines.append("Backup and destructive restore are ready with security warnings.")
    elif (
        report.get("backupFullyVerified", report.get("fullyVerified", True))
        and report.get("restoreFullyVerified", report.get("fullyVerified", True))
    ):
        lines.append("Backup and destructive restore are ready.")
    else:
        lines.append("No confirmed blocker; readiness is partial.")
    return "\n".join(lines)
