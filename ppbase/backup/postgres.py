"""PostgreSQL primitives for native backup and destructive in-place restore.

This module deliberately stays below the PPBase application layer.  It never
starts the application, loads hooks, or talks to storage backends.  Its public
functions are small building blocks for an orchestrator which already owns the
backup write-barrier lease.

Passwords are parsed from SQLAlchemy ``postgresql+asyncpg`` URLs, but are never
included in a command line or libpq conninfo string.  PostgreSQL client tools
receive credentials through a short-lived, mode-0600 ``PGPASSFILE`` instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection


_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_TRUE_VALUES = frozenset({"1", "on", "t", "true", "yes"})
_SECURE_LOCAL_SEARCH_PATH_SQL = "SET LOCAL search_path = pg_catalog, pg_temp"
_SECURE_SESSION_SEARCH_PATH_SQL = "SET search_path = pg_catalog, pg_temp"
_LIBPQ_QUERY_PARAMETERS = frozenset(
    {
        "application_name",
        "channel_binding",
        "connect_timeout",
        "gssencmode",
        "hostaddr",
        "keepalives",
        "keepalives_count",
        "keepalives_idle",
        "keepalives_interval",
        "krbsrvname",
        "options",
        "requirepeer",
        "sslcert",
        "sslcrl",
        "sslcrldir",
        "sslkey",
        "sslmode",
        "sslnegotiation",
        "sslrootcert",
        "target_session_attrs",
        "tcp_user_timeout",
    }
)


class PostgresBackupError(RuntimeError):
    """Base class for safe-to-surface PostgreSQL backup errors."""


class InvalidDatabaseUrlError(PostgresBackupError, ValueError):
    """Raised when an application URL cannot safely become libpq conninfo."""


class InvalidPostgresIdentifierError(PostgresBackupError, ValueError):
    """Raised when a database or role name is outside the supported subset."""


class PostgresContractError(PostgresBackupError):
    """Raised when a source or target violates the supported DB contract."""


@dataclass(frozen=True)
class LibpqConnectionInfo:
    """Credential-separated representation of a PostgreSQL application URL."""

    conninfo: str
    host: str
    port: str
    database: str
    username: str
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ExtensionContract:
    name: str
    version: str
    schema: str


@dataclass(frozen=True)
class CollationContract:
    schema: str
    name: str
    provider: str
    collate: str | None
    ctype: str | None
    locale: str | None
    rules: str | None
    version: str | None
    actual_version: str | None


@dataclass(frozen=True)
class ObjectSecuritySummary:
    object_kind: str
    object_count: int
    owners: tuple[str, ...]
    acl_object_count: int


@dataclass(frozen=True)
class DatabaseContract:
    """Portable subset of source PostgreSQL database metadata."""

    database: str
    server_version_num: int
    server_major: int
    encoding: str
    locale_provider: str
    collate: str
    ctype: str
    icu_locale: str | None
    icu_rules: str | None
    collation_version: str | None
    actual_collation_version: str | None
    database_owner: str
    database_acl: str | None
    extensions: tuple[ExtensionContract, ...]
    collations: tuple[CollationContract, ...]
    object_security: tuple[ObjectSecuritySummary, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, JSON-compatible manifest representation."""
        return {
            "database": self.database,
            "server_version_num": self.server_version_num,
            "server_major": self.server_major,
            "encoding": self.encoding,
            "locale_provider": self.locale_provider,
            "collate": self.collate,
            "ctype": self.ctype,
            "icu_locale": self.icu_locale,
            "icu_rules": self.icu_rules,
            "collation_version": self.collation_version,
            "actual_collation_version": self.actual_collation_version,
            "database_owner": self.database_owner,
            "database_acl": self.database_acl,
            "extensions": [
                {
                    "name": item.name,
                    "version": item.version,
                    "schema": item.schema,
                }
                for item in self.extensions
            ],
            "collations": [
                {
                    "schema": item.schema,
                    "name": item.name,
                    "provider": item.provider,
                    "collate": item.collate,
                    "ctype": item.ctype,
                    "locale": item.locale,
                    "rules": item.rules,
                    "version": item.version,
                    "actual_version": item.actual_version,
                }
                for item in self.collations
            ],
            "object_security": [
                {
                    "object_kind": item.object_kind,
                    "object_count": item.object_count,
                    "owners": list(item.owners),
                    "acl_object_count": item.acl_object_count,
                }
                for item in self.object_security
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DatabaseContract:
        """Rebuild a contract from trusted, schema-validated manifest data."""
        try:
            extensions = tuple(
                ExtensionContract(
                    name=str(item["name"]),
                    version=str(item["version"]),
                    schema=str(item["schema"]),
                )
                for item in value["extensions"]
            )
            collations = tuple(
                CollationContract(
                    schema=str(item["schema"]),
                    name=str(item["name"]),
                    provider=str(item["provider"]),
                    collate=_optional_str(item.get("collate")),
                    ctype=_optional_str(item.get("ctype")),
                    locale=_optional_str(item.get("locale")),
                    rules=_optional_str(item.get("rules")),
                    version=_optional_str(item.get("version")),
                    actual_version=_optional_str(item.get("actual_version")),
                )
                for item in value["collations"]
            )
            security = tuple(
                ObjectSecuritySummary(
                    object_kind=str(item["object_kind"]),
                    object_count=int(item["object_count"]),
                    owners=tuple(str(owner) for owner in item["owners"]),
                    acl_object_count=int(item["acl_object_count"]),
                )
                for item in value["object_security"]
            )
            return cls(
                database=str(value["database"]),
                server_version_num=int(value["server_version_num"]),
                server_major=int(value["server_major"]),
                encoding=str(value["encoding"]),
                locale_provider=str(value["locale_provider"]),
                collate=str(value["collate"]),
                ctype=str(value["ctype"]),
                icu_locale=_optional_str(value.get("icu_locale")),
                icu_rules=_optional_str(value.get("icu_rules")),
                collation_version=_optional_str(value.get("collation_version")),
                actual_collation_version=_optional_str(
                    value.get("actual_collation_version")
                ),
                database_owner=str(value["database_owner"]),
                database_acl=_optional_str(value.get("database_acl")),
                extensions=extensions,
                collations=collations,
                object_security=security,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PostgresContractError("invalid database contract manifest data") from exc


@dataclass(frozen=True)
class PreflightReport:
    """Non-mutating contract check result."""

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def require_ok(self) -> None:
        if self.errors:
            raise PostgresContractError("; ".join(self.errors))


async def set_backup_control_search_path(connection: AsyncConnection) -> None:
    """Restrict control-plane SQL name resolution for the current transaction.

    ``pg_temp`` must be named explicitly after ``pg_catalog``. PostgreSQL
    otherwise searches the temporary schema first for relation and type names,
    even when ``pg_catalog`` is the only configured search-path entry.
    """
    await connection.execute(text(_SECURE_LOCAL_SEARCH_PATH_SQL))


def validate_postgres_identifier(value: str, *, label: str = "identifier") -> str:
    """Validate a conservative, unquoted PostgreSQL identifier.

    Native restore plans generate their database and role identifiers, so the
    intentionally narrow lowercase ASCII subset avoids truncation, folding,
    confusable characters, and SQL interpolation ambiguity.
    """
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise InvalidPostgresIdentifierError(
            f"{label} must match [a-z_][a-z0-9_]{{0,62}}"
        )
    return value


def sqlalchemy_url_to_libpq(database_url: str | URL) -> LibpqConnectionInfo:
    """Convert an asyncpg SQLAlchemy URL into password-free libpq conninfo."""
    try:
        url = database_url if isinstance(database_url, URL) else make_url(database_url)
    except Exception as exc:
        # SQLAlchemy parse errors may echo their input, so do not chain or copy it.
        raise InvalidDatabaseUrlError("invalid PostgreSQL database URL") from None

    if url.drivername != "postgresql+asyncpg":
        raise InvalidDatabaseUrlError(
            "database URL must use the postgresql+asyncpg driver"
        )
    if not url.database:
        raise InvalidDatabaseUrlError("database URL must name a database")
    if not url.username:
        raise InvalidDatabaseUrlError("database URL must name a login role")

    components: dict[str, str] = {
        "dbname": _checked_conninfo_value(url.database, "database"),
        "user": _checked_conninfo_value(url.username, "username"),
    }
    if url.host:
        components["host"] = _checked_conninfo_value(url.host, "host")
    if url.port is not None:
        components["port"] = str(url.port)

    for key, raw_value in url.query.items():
        if isinstance(raw_value, tuple):
            raise InvalidDatabaseUrlError(
                f"database URL parameter {key!r} must have exactly one value"
            )
        value = str(raw_value)
        normalized_key = "sslmode" if key == "ssl" else key
        if normalized_key not in _LIBPQ_QUERY_PARAMETERS:
            raise InvalidDatabaseUrlError(
                f"database URL parameter {key!r} is not approved for libpq tools"
            )
        if normalized_key in components:
            raise InvalidDatabaseUrlError(
                f"database URL parameter {key!r} is duplicated"
            )
        if key == "ssl":
            value = _normalize_asyncpg_ssl(value)
        components[normalized_key] = _checked_conninfo_value(value, key)

    ordered_keys = ("host", "port", "dbname", "user")
    conninfo_parts = [
        f"{key}={_quote_conninfo_value(components[key])}"
        for key in ordered_keys
        if key in components
    ]
    conninfo_parts.extend(
        f"{key}={_quote_conninfo_value(value)}"
        for key, value in sorted(components.items())
        if key not in ordered_keys
    )
    password = None if url.password is None else str(url.password)
    if password is not None:
        _checked_conninfo_value(password, "password")

    return LibpqConnectionInfo(
        conninfo=" ".join(conninfo_parts),
        host=url.host or "localhost",
        port=str(url.port or 5432),
        database=url.database,
        username=url.username,
        password=password,
    )


async def inspect_database_contract(
    connection: AsyncConnection,
) -> DatabaseContract:
    """Collect the source DB contract on the caller's existing lock lease."""
    await set_backup_control_search_path(connection)
    database_row = (
        await connection.execute(
            text(
                """
                SELECT
                    current_setting('server_version_num')::integer AS server_version_num,
                    d.datname AS database,
                    pg_encoding_to_char(d.encoding) AS encoding,
                    d.datlocprovider::text AS locale_provider,
                    d.datcollate AS collate,
                    d.datctype AS ctype,
                    COALESCE(
                        to_jsonb(d)->>'datlocale',
                        to_jsonb(d)->>'daticulocale'
                    ) AS icu_locale,
                    to_jsonb(d)->>'daticurules' AS icu_rules,
                    to_jsonb(d)->>'datcollversion' AS collation_version,
                    pg_get_userbyid(d.datdba) AS database_owner,
                    d.datacl::text AS database_acl
                FROM pg_database AS d
                WHERE d.datname = current_database()
                """
            )
        )
    ).mappings().one()

    server_version_num = int(database_row["server_version_num"])
    actual_collation_version: str | None = None
    has_database_version_function = bool(
        (
            await connection.execute(
                text(
                    """
                    SELECT to_regprocedure(
                        'pg_catalog.pg_database_collation_actual_version(oid)'
                    ) IS NOT NULL
                    """
                )
            )
        ).scalar_one()
    )
    if has_database_version_function:
        actual_collation_version = (
            await connection.execute(
                text(
                    """
                    SELECT pg_catalog.pg_database_collation_actual_version(oid)
                    FROM pg_database
                    WHERE datname = current_database()
                    """
                )
            )
        ).scalar_one_or_none()

    extension_rows = (
        await connection.execute(
            text(
                """
                SELECT e.extname AS name, e.extversion AS version,
                       n.nspname AS schema
                FROM pg_extension AS e
                JOIN pg_namespace AS n ON n.oid = e.extnamespace
                ORDER BY e.extname
                """
            )
        )
    ).mappings().all()

    collation_rows = (
        await connection.execute(
            text(
                """
                SELECT DISTINCT
                    cn.nspname AS schema,
                    c.collname AS name,
                    c.collprovider::text AS provider,
                    to_jsonb(c)->>'collcollate' AS collate,
                    to_jsonb(c)->>'collctype' AS ctype,
                    COALESCE(
                        to_jsonb(c)->>'colllocale',
                        to_jsonb(c)->>'colliculocale'
                    ) AS locale,
                    to_jsonb(c)->>'collicurules' AS rules,
                    c.collversion AS version,
                    pg_collation_actual_version(c.oid) AS actual_version
                FROM pg_attribute AS a
                JOIN pg_class AS r ON r.oid = a.attrelid
                JOIN pg_namespace AS rn ON rn.oid = r.relnamespace
                JOIN pg_collation AS c ON c.oid = a.attcollation
                JOIN pg_namespace AS cn ON cn.oid = c.collnamespace
                WHERE a.attnum > 0
                  AND NOT a.attisdropped
                  AND a.attcollation <> 0
                  AND rn.nspname <> 'information_schema'
                  AND rn.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                ORDER BY cn.nspname, c.collname
                """
            )
        )
    ).mappings().all()

    security_rows = (
        await connection.execute(
            text(
                """
                WITH user_objects AS (
                    SELECT 'schema'::text AS object_kind,
                           pg_get_userbyid(n.nspowner) AS owner,
                           n.nspacl IS NOT NULL AS has_acl
                    FROM pg_namespace AS n
                    WHERE n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                    UNION ALL
                    SELECT CASE c.relkind
                               WHEN 'S' THEN 'sequence'
                               WHEN 'v' THEN 'view'
                               WHEN 'm' THEN 'materialized_view'
                               WHEN 'f' THEN 'foreign_table'
                               ELSE 'relation'
                           END,
                           pg_get_userbyid(c.relowner),
                           c.relacl IS NOT NULL
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
                      AND n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                    UNION ALL
                    SELECT 'routine', pg_get_userbyid(p.proowner),
                           p.proacl IS NOT NULL
                    FROM pg_proc AS p
                    JOIN pg_namespace AS n ON n.oid = p.pronamespace
                    WHERE n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                )
                SELECT object_kind, count(*)::integer AS object_count,
                       array_agg(DISTINCT owner ORDER BY owner) AS owners,
                       count(*) FILTER (WHERE has_acl)::integer AS acl_object_count
                FROM user_objects
                GROUP BY object_kind
                ORDER BY object_kind
                """
            )
        )
    ).mappings().all()

    return DatabaseContract(
        database=str(database_row["database"]),
        server_version_num=server_version_num,
        server_major=server_version_num // 10000,
        encoding=str(database_row["encoding"]),
        locale_provider=_normalize_provider(database_row["locale_provider"]),
        collate=str(database_row["collate"]),
        ctype=str(database_row["ctype"]),
        icu_locale=_optional_str(database_row["icu_locale"]),
        icu_rules=_optional_str(database_row["icu_rules"]),
        collation_version=_optional_str(database_row["collation_version"]),
        actual_collation_version=_optional_str(actual_collation_version),
        database_owner=str(database_row["database_owner"]),
        database_acl=_optional_str(database_row["database_acl"]),
        extensions=tuple(
            ExtensionContract(
                name=str(row["name"]),
                version=str(row["version"]),
                schema=str(row["schema"]),
            )
            for row in extension_rows
        ),
        collations=tuple(
            CollationContract(
                schema=str(row["schema"]),
                name=str(row["name"]),
                provider=_normalize_provider(row["provider"]),
                collate=_optional_str(row["collate"]),
                ctype=_optional_str(row["ctype"]),
                locale=_optional_str(row["locale"]),
                rules=_optional_str(row["rules"]),
                version=_optional_str(row["version"]),
                actual_version=_optional_str(row["actual_version"]),
            )
            for row in collation_rows
        ),
        object_security=tuple(
            ObjectSecuritySummary(
                object_kind=str(row["object_kind"]),
                object_count=int(row["object_count"]),
                owners=tuple(str(owner) for owner in (row["owners"] or ())),
                acl_object_count=int(row["acl_object_count"]),
            )
            for row in security_rows
        ),
    )


async def preflight_destructive_restore_role(
    connection: AsyncConnection,
) -> PreflightReport:
    """Verify that the active login can replace the objects it owns in-place."""
    await set_backup_control_search_path(connection)
    row = (
        await connection.execute(
            text(
                """
                SELECT current_user AS role,
                       runtime_role.rolsuper AS is_superuser,
                       pg_catalog.pg_has_role(
                           runtime_role.oid,
                           database.datdba,
                           'USAGE'
                       ) AS owns_database,
                       pg_catalog.pg_has_role(
                           runtime_role.oid,
                           namespace.nspowner,
                           'USAGE'
                       ) AS owns_schema
                FROM pg_catalog.pg_roles AS runtime_role
                JOIN pg_catalog.pg_database AS database
                  ON database.datname = pg_catalog.current_database()
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.nspname = 'public'
                WHERE runtime_role.rolname = current_user
                """
            )
        )
    ).mappings().one()
    role = str(row["role"])
    if bool(row["is_superuser"]):
        return PreflightReport(
            warnings=("destructive restore uses the PostgreSQL runtime superuser",)
        )

    errors: list[str] = []
    if not bool(row["owns_database"]):
        errors.append("runtime role must own the active PostgreSQL database")
    if not bool(row["owns_schema"]):
        errors.append("runtime role must own the public application schema")
    return PreflightReport(errors=tuple(errors))


def _normalize_provider(value: Any) -> str:
    provider = str(value)
    return {"c": "libc", "i": "icu", "b": "builtin"}.get(provider, provider)


def _normalize_asyncpg_ssl(value: str) -> str:
    normalized = value.lower()
    aliases = {
        "true": "require",
        "1": "require",
        "false": "disable",
        "0": "disable",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = {
        "disable",
        "allow",
        "prefer",
        "require",
        "verify-ca",
        "verify-full",
    }
    if normalized not in allowed:
        raise InvalidDatabaseUrlError(
            "asyncpg ssl parameter cannot be represented as libpq sslmode"
        )
    return normalized


def _checked_conninfo_value(value: str, label: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise InvalidDatabaseUrlError(f"{label} contains a forbidden control character")
    return value


def _quote_conninfo_value(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _quote_identifier(value: str) -> str:
    validate_postgres_identifier(value)
    return f'"{value}"'


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
