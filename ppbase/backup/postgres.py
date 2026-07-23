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

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any, BinaryIO, Iterator
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection


_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_SNAPSHOT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_VERSION_RE = re.compile(r"\b(\d+)(?:\.(\d+))?(?:\.\d+)*\b")
_TRUE_VALUES = frozenset({"1", "on", "t", "true", "yes"})
_SECURE_LOCAL_SEARCH_PATH_SQL = "SET LOCAL search_path = pg_catalog, pg_temp"
_SECURE_SESSION_SEARCH_PATH_SQL = "SET search_path = pg_catalog, pg_temp"
_PUBLIC_SCHEMA_TOC_TYPES = tuple(
    sorted(
        (
            "MATERIALIZED VIEW DATA",
            "PUBLICATION TABLES IN SCHEMA",
            "TEXT SEARCH CONFIGURATION",
            "TEXT SEARCH DICTIONARY",
            "TEXT SEARCH PARSER",
            "TEXT SEARCH TEMPLATE",
            "SEQUENCE OWNED BY",
            "OPERATOR FAMILY",
            "OPERATOR CLASS",
            "MATERIALIZED VIEW",
            "CHECK CONSTRAINT",
            "INDEX ATTACH",
            "TABLE ATTACH",
            "FOREIGN TABLE",
            "FK CONSTRAINT",
            "DEFAULT ACL",
            "TABLE DATA",
            "SEQUENCE SET",
            "ACCESS METHOD",
            "AGGREGATE",
            "COLLATION",
            "CONSTRAINT",
            "CONVERSION",
            "DEFAULT",
            "DOMAIN",
            "FUNCTION",
            "INDEX",
            "OPERATOR",
            "POLICY",
            "PROCEDURE",
            "PUBLICATION TABLE",
            "RULE",
            "SEQUENCE",
            "STATISTICS",
            "TABLE",
            "TRIGGER",
            "TYPE",
            "VIEW",
            "ACL",
        ),
        key=len,
        reverse=True,
    )
)
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


class PostgresVersionMismatchError(PostgresBackupError):
    """Raised when PostgreSQL client and server major versions differ."""


class PostgresContractError(PostgresBackupError):
    """Raised when a source or target violates the supported DB contract."""


class PostgresCommandError(PostgresBackupError):
    """A PostgreSQL client command failed, with credentials redacted."""

    def __init__(
        self,
        executable: str,
        returncode: int,
        stderr: str,
    ) -> None:
        self.executable = executable
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"{Path(executable).name} failed with exit code {returncode}: {stderr}"
        )


@dataclass(frozen=True)
class CommandResult:
    """Captured, decoded output from an async subprocess."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[
    [Sequence[str], Mapping[str, str], Sequence[str]],
    Awaitable[CommandResult],
]


@dataclass(frozen=True)
class LibpqConnectionInfo:
    """Credential-separated representation of a PostgreSQL application URL."""

    conninfo: str
    host: str
    port: str
    database: str
    username: str
    password: str | None = field(default=None, repr=False)

    def pgpass_line(self) -> str | None:
        """Return one escaped libpq passfile line, or ``None`` if passwordless."""
        if self.password is None:
            return None
        values = (
            self.host or "localhost",
            self.port or "5432",
            self.database,
            self.username,
            self.password,
        )
        return ":".join(_escape_pgpass_field(value) for value in values) + "\n"


@dataclass(frozen=True)
class PostgresToolVersion:
    """Parsed version of a PostgreSQL command-line client."""

    executable: str
    version: str
    major: int


@dataclass(frozen=True)
class PostgresVersionSet:
    """Server and client versions used for a dump/restore plan."""

    server_version_num: int
    server_major: int
    pg_dump: PostgresToolVersion
    pg_restore: PostgresToolVersion


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


@dataclass(frozen=True)
class DumpResult:
    destination: Path
    client_version: PostgresToolVersion
    stdout: str


@dataclass(frozen=True)
class RestoreArchiveInspection:
    archive: Path
    client_version: PostgresToolVersion
    toc: str


@dataclass(frozen=True)
class DestructiveRestoreResult:
    """Result of replacing the active application schema in one transaction."""

    archive: Path
    target_database: str
    runtime_role: str
    restore_id: str
    client_version: PostgresToolVersion
    toc: str


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



@contextmanager
def temporary_pgpass(
    connection: LibpqConnectionInfo,
    *,
    directory: str | Path | None = None,
) -> Iterator[Path]:
    """Create and securely remove an operation-scoped libpq passfile."""
    line = connection.pgpass_line()
    parent = Path(directory) if directory is not None else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    path: Path | None = None
    fd: int | None = None
    try:
        for _ in range(32):
            candidate = parent / f".ppbase-pgpass-{secrets.token_hex(16)}"
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
            except FileExistsError:
                continue
            path = candidate
            break
        if fd is None or path is None:
            raise PostgresBackupError("could not allocate a temporary passfile")

        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = None
            # An explicit empty passfile also prevents passwordless URLs from
            # silently consuming credentials from the process owner's ~/.pgpass.
            handle.write(line or "")
            handle.flush()
            os.fsync(handle.fileno())
        if path.stat().st_mode & 0o777 != 0o600:
            raise PostgresBackupError("temporary passfile permissions are not 0600")
        yield path
    finally:
        if fd is not None:
            os.close(fd)
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class _OperationPgpass:
    """One operation-scoped passfile and any descriptor a child must inherit."""

    path: Path
    inherited_fds: tuple[int, ...] = ()

    def rewind(self) -> None:
        """Reset an anonymous passfile before each independent tool process."""
        for descriptor in self.inherited_fds:
            os.lseek(descriptor, 0, os.SEEK_SET)


def _inherited_fd_path(descriptor: int) -> Path:
    """Return a child-reopenable path for one explicitly inherited POSIX FD."""
    if os.name != "posix":
        raise PostgresBackupError(
            "descriptor-backed PostgreSQL passfiles require POSIX support"
        )
    if Path("/proc/self/fd").is_dir():
        return Path("/proc/self/fd") / str(descriptor)
    if Path("/dev/fd").is_dir():
        return Path("/dev/fd") / str(descriptor)
    raise PostgresBackupError(
        "this platform cannot expose an inherited PostgreSQL passfile descriptor"
    )


@contextmanager
def _operation_pgpass(
    connection: LibpqConnectionInfo,
    *,
    directory: str | Path | None = None,
    file_factory: Callable[[], BinaryIO] | None = None,
) -> Iterator[_OperationPgpass]:
    """Create either a regular temporary passfile or an anonymous anchored one."""
    if directory is not None and file_factory is not None:
        raise ValueError("passfile directory and file factory are mutually exclusive")
    if file_factory is None:
        with temporary_pgpass(connection, directory=directory) as path:
            yield _OperationPgpass(path=path)
        return

    handle: BinaryIO | None = None
    try:
        handle = file_factory()
        descriptor = handle.fileno()
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PostgresBackupError(
                "anonymous PostgreSQL passfile must be a regular file"
            )
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = (connection.pgpass_line() or "").encode("utf-8")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise PostgresBackupError(
                    "short write while creating PostgreSQL passfile"
                )
            remaining = remaining[written:]
        os.fsync(descriptor)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise PostgresBackupError(
                "temporary passfile permissions are not 0600"
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield _OperationPgpass(
            path=_inherited_fd_path(descriptor),
            inherited_fds=(descriptor,),
        )
    finally:
        if handle is not None:
            handle.close()


async def run_pg_dump(
    database_url: str | URL,
    destination: str | Path,
    *,
    pg_dump: str = "pg_dump",
    expected_server_major: int | None = None,
    lock_wait_timeout_seconds: int = 30,
    snapshot_id: str | None = None,
    passfile_directory: str | Path | None = None,
    runner: CommandRunner | None = None,
) -> DumpResult:
    """Write a public-only custom dump without owners, ACLs, or tablespaces."""
    connection = sqlalchemy_url_to_libpq(database_url)
    output = Path(destination)
    if not output.parent.is_dir():
        raise PostgresBackupError("pg_dump destination parent does not exist")

    command_runner = runner or _run_command
    version = await detect_postgres_tool_version(pg_dump, runner=command_runner)
    _require_client_supports_server(version, expected_server_major)
    if not isinstance(lock_wait_timeout_seconds, int) or not (
        1 <= lock_wait_timeout_seconds <= 3600
    ):
        raise ValueError("lock_wait_timeout_seconds must be an integer from 1 to 3600")
    if snapshot_id is not None and (
        not isinstance(snapshot_id, str)
        or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id)
    ):
        raise ValueError("snapshot_id is not a valid PostgreSQL exported snapshot ID")
    reserved_identity = _reserve_output_file(output)

    argv_parts = [
        pg_dump,
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--no-tablespaces",
        "--schema=public",
        f"--lock-wait-timeout={lock_wait_timeout_seconds}s",
    ]
    if snapshot_id is not None:
        argv_parts.append(f"--snapshot={snapshot_id}")
    argv_parts.extend(
        (
            "--no-password",
            "--file",
            str(output),
            "--dbname",
            connection.conninfo,
        )
    )
    argv = tuple(argv_parts)
    try:
        with temporary_pgpass(connection, directory=passfile_directory) as passfile:
            env = _postgres_environment(passfile)
            result = await command_runner(argv, env, _redaction_values(connection))
        stat_result = output.lstat()
        if output.is_symlink() or (
            stat_result.st_dev,
            stat_result.st_ino,
        ) != reserved_identity:
            raise PostgresBackupError("pg_dump destination identity changed")
        if stat_result.st_size <= 0:
            raise PostgresBackupError("pg_dump produced an empty archive")
    except BaseException:
        _unlink_reserved_output(output, reserved_identity)
        raise
    return DumpResult(
        destination=output,
        client_version=version,
        stdout=result.stdout,
    )


async def inspect_pg_restore_archive(
    archive: str | Path,
    *,
    pg_restore: str = "pg_restore",
    expected_server_major: int | None = None,
    runner: CommandRunner | None = None,
) -> RestoreArchiveInspection:
    """Parse an archive and enforce the public-only policy before mutation."""
    archive_path = _require_regular_file(archive, "restore archive")
    command_runner = runner or _run_command
    version = await detect_postgres_tool_version(pg_restore, runner=command_runner)
    _require_client_supports_server(version, expected_server_major)
    result = await command_runner(
        (pg_restore, "--list", str(archive_path)),
        _postgres_environment(None),
        (),
    )
    _require_public_only_archive_toc(result.stdout)
    return RestoreArchiveInspection(
        archive=archive_path,
        client_version=version,
        toc=result.stdout,
    )


# ``SET transaction_timeout`` first appears in PostgreSQL 17 dump headers. The
# bundled PostgreSQL 17 client can dump and restore into a 16 server, but
# replaying that header line into a 16 server aborts under ON_ERROR_STOP because
# the parameter does not exist there. It is only ever emitted in the leading
# header (before any DDL or COPY data). The destructive prelude already disables
# transaction_timeout for the session in a 16/17-compatible way, so removing the
# incompatible header line loses no protection while keeping the restore
# portable across server majors.
_UNKNOWN_HEADER_SET_RE = re.compile(rb"^SET transaction_timeout\b")
_RESTORE_HEADER_LINE_RE = re.compile(
    rb"^(?:--|SET |SELECT pg_catalog\.set_config|\\|\s*$)"
)


async def _stream_restore_sql_to_psql(source: Any, destination: Any) -> None:
    """Copy pg_restore SQL into psql, dropping unknown header GUC lines.

    Filtering is confined to the leading header (comments, ``SET``,
    ``SELECT pg_catalog.set_config`` and ``\\`` meta-command lines such as the
    PostgreSQL 17 ``\\restrict`` guard): once the first DDL or data line is seen
    the remainder streams verbatim so COPY payloads are never inspected.
    """
    header_done = False
    pending = b""
    while True:
        chunk = await source.read(1024 * 1024)
        if not chunk:
            break
        if header_done:
            if pending:
                destination.write(pending)
                pending = b""
            destination.write(chunk)
            await destination.drain()
            continue
        pending += chunk
        while True:
            newline = pending.find(b"\n")
            if newline == -1:
                break
            line = pending[: newline + 1]
            pending = pending[newline + 1 :]
            if header_done:
                destination.write(line)
            elif _RESTORE_HEADER_LINE_RE.match(line):
                if not _UNKNOWN_HEADER_SET_RE.match(line):
                    destination.write(line)
            else:
                header_done = True
                destination.write(line)
        await destination.drain()
    if pending:
        destination.write(pending)
        await destination.drain()


async def run_destructive_pg_restore_from_fd(
    database_url: str | URL,
    archive_fd: int,
    *,
    archive_label: str | Path,
    restore_id: str,
    pg_restore: str = "pg_restore",
    psql: str = "psql",
    expected_server_major: int | None = None,
    passfile_factory: Callable[[], BinaryIO] | None = None,
) -> DestructiveRestoreResult:
    """Replace ``public`` atomically by streaming pg_restore SQL through psql.

    ``psql`` is retained because it is the smallest reliable way to place the
    schema reset, the complete pg_restore script, and the durable commit marker
    in one PostgreSQL transaction.  If either child fails or the process is
    interrupted before COMMIT, PostgreSQL rolls the whole replacement back.
    """
    if os.name != "posix":
        raise PostgresBackupError(
            "descriptor-pinned destructive restore requires POSIX support"
        )
    if not re.fullmatch(r"[a-f0-9]{32}", restore_id):
        raise PostgresBackupError("destructive restore ID must be 32 lowercase hex characters")
    if isinstance(archive_fd, bool) or not isinstance(archive_fd, int) or archive_fd < 0:
        raise PostgresBackupError("pinned restore archive descriptor is invalid")
    try:
        archive_info = os.fstat(archive_fd)
    except OSError as exc:
        raise PostgresBackupError("pinned restore archive descriptor is not open") from exc
    if not stat.S_ISREG(archive_info.st_mode) or archive_info.st_size <= 0:
        raise PostgresBackupError("pinned restore archive must be a non-empty regular file")

    connection = sqlalchemy_url_to_libpq(database_url)
    runtime_role = connection.username
    quoted_runtime_role = _quote_catalog_identifier(
        runtime_role,
        label="runtime role",
    )
    version = await detect_postgres_tool_version(pg_restore, runner=_run_command)
    _require_client_supports_server(version, expected_server_major)

    os.lseek(archive_fd, 0, os.SEEK_SET)
    list_result = await _run_command(
        (pg_restore, "--list"),
        _postgres_environment(None),
        (),
        stdin_fd=archive_fd,
    )
    _require_public_only_archive_toc(list_result.stdout)
    os.lseek(archive_fd, 0, os.SEEK_SET)

    # ``public`` must always be recreated after the drop.  Since PostgreSQL 15
    # the schema is a built-in owned by ``pg_database_owner`` and
    # ``pg_restore --schema=public`` never emits ``CREATE SCHEMA public`` in its
    # ``--file=-`` output, so relying on the archive to recreate it would leave
    # the target without a ``public`` schema and fail on the first table.
    prelude = (
        "\\set ON_ERROR_STOP on\n"
        # PostgreSQL 17 added transaction_timeout; a non-zero server-wide value
        # would abort a long destructive restore. Disable it for this session in
        # a way that is a no-op on 16 (no matching pg_settings row) and must run
        # before BEGIN because a session GUC cannot be changed mid-transaction
        # here otherwise.
        "SELECT pg_catalog.set_config(name, '0', false)\n"
        "  FROM pg_catalog.pg_settings WHERE name = 'transaction_timeout';\n"
        "BEGIN;\n"
        "SET LOCAL statement_timeout = 0;\n"
        "SET LOCAL lock_timeout = '30s';\n"
        "DROP SCHEMA IF EXISTS public CASCADE;\n"
        'DROP SCHEMA IF EXISTS "_ppbase_restore_control" CASCADE;\n'
        f"CREATE SCHEMA public AUTHORIZATION {quoted_runtime_role};\n"
    ).encode("utf-8")
    postlude = (
        '\nCREATE SCHEMA "_ppbase_restore_control" AUTHORIZATION '
        f"{quoted_runtime_role};\n"
        'CREATE TABLE "_ppbase_restore_control".commits ('
        "restore_id text PRIMARY KEY, "
        "committed_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()"
        ");\n"
        'INSERT INTO "_ppbase_restore_control".commits (restore_id) '
        f"VALUES ('{restore_id}');\n"
        "COMMIT;\n"
    ).encode("utf-8")

    restore_argv = (
        pg_restore,
        "--file=-",
        "--no-owner",
        "--no-acl",
        "--no-tablespaces",
        "--schema=public",
    )
    psql_argv = (
        psql,
        "--no-psqlrc",
        "--quiet",
        "--no-password",
        "--set=ON_ERROR_STOP=1",
        "--dbname",
        connection.conninfo,
    )
    redactions = _redaction_values(connection)

    with _operation_pgpass(connection, file_factory=passfile_factory) as passfile:
        passfile.rewind()
        environment = _postgres_environment(passfile.path)
        process_options: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.PIPE,
            "env": environment,
        }
        if passfile.inherited_fds:
            process_options["pass_fds"] = passfile.inherited_fds
        try:
            psql_process = await asyncio.create_subprocess_exec(
                *psql_argv,
                **process_options,
            )
            restore_process = await asyncio.create_subprocess_exec(
                *restore_argv,
                stdin=archive_fd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_postgres_environment(None),
                pass_fds=(archive_fd,),
            )
        except OSError as exc:
            if "psql_process" in locals():
                await _terminate_process(psql_process)
            raise PostgresCommandError(
                psql if "psql_process" not in locals() else pg_restore,
                -1,
                _redact(str(exc), redactions),
            ) from None

        psql_stderr_task = asyncio.create_task(_read_limited_stream(psql_process.stderr))
        restore_stderr_task = asyncio.create_task(_read_limited_stream(restore_process.stderr))
        try:
            if psql_process.stdin is None or restore_process.stdout is None:
                raise PostgresBackupError("PostgreSQL restore subprocess pipes are unavailable")
            psql_process.stdin.write(prelude)
            await psql_process.stdin.drain()
            await _stream_restore_sql_to_psql(
                restore_process.stdout, psql_process.stdin
            )
            restore_returncode = await restore_process.wait()
            restore_stderr = _redact(
                (await restore_stderr_task).decode("utf-8", errors="replace").strip(),
                redactions,
            )
            if restore_returncode != 0:
                psql_process.stdin.write(b"\nROLLBACK;\n")
                await psql_process.stdin.drain()
                psql_process.stdin.close()
                await psql_process.wait()
                await psql_stderr_task
                raise PostgresCommandError(pg_restore, restore_returncode, restore_stderr)

            psql_process.stdin.write(postlude)
            await psql_process.stdin.drain()
            psql_process.stdin.close()
            psql_returncode = await psql_process.wait()
            psql_stderr = _redact(
                (await psql_stderr_task).decode("utf-8", errors="replace").strip(),
                redactions,
            )
            if psql_returncode != 0:
                raise PostgresCommandError(psql, psql_returncode, psql_stderr)
        except BaseException:
            await _terminate_process(restore_process)
            await _terminate_process(psql_process)
            if not restore_stderr_task.done():
                restore_stderr_task.cancel()
            if not psql_stderr_task.done():
                psql_stderr_task.cancel()
            raise

    return DestructiveRestoreResult(
        archive=Path(archive_label),
        target_database=connection.database,
        runtime_role=runtime_role,
        restore_id=restore_id,
        client_version=version,
        toc=list_result.stdout,
    )


async def detect_postgres_tool_version(
    executable: str,
    *,
    runner: CommandRunner | None = None,
) -> PostgresToolVersion:
    """Run ``<tool> --version`` and parse its PostgreSQL major version."""
    command_runner = runner or _run_command
    result = await command_runner(
        (executable, "--version"),
        _postgres_environment(None),
        (),
    )
    output = (result.stdout or result.stderr).strip()
    match = _VERSION_RE.search(output)
    if match is None:
        raise PostgresBackupError(
            f"could not parse {Path(executable).name} version output"
        )
    version = match.group(0)
    return PostgresToolVersion(
        executable=executable,
        version=version,
        major=int(match.group(1)),
    )


async def detect_postgres_versions(
    connection: AsyncConnection,
    *,
    pg_dump: str = "pg_dump",
    pg_restore: str = "pg_restore",
    runner: CommandRunner | None = None,
    require_same_major: bool = False,
) -> PostgresVersionSet:
    """Detect server, pg_dump, and pg_restore versions on an existing lease."""
    await set_backup_control_search_path(connection)
    command_runner = runner or _run_command
    server_version_num = int(
        (
            await connection.execute(
                text("SELECT current_setting('server_version_num')::integer")
            )
        ).scalar_one()
    )
    server_major = server_version_num // 10000
    dump_version, restore_version = await asyncio.gather(
        detect_postgres_tool_version(pg_dump, runner=command_runner),
        detect_postgres_tool_version(pg_restore, runner=command_runner),
    )
    versions = PostgresVersionSet(
        server_version_num=server_version_num,
        server_major=server_major,
        pg_dump=dump_version,
        pg_restore=restore_version,
    )
    if require_same_major:
        mismatches = [
            tool
            for tool in (dump_version, restore_version)
            if tool.major != server_major
        ]
        if mismatches:
            details = ", ".join(
                f"{Path(tool.executable).name}={tool.major}" for tool in mismatches
            )
            raise PostgresVersionMismatchError(
                f"PostgreSQL server major is {server_major}, but {details}"
            )
    elif dump_version.major != restore_version.major:
        raise PostgresVersionMismatchError(
            "bundled pg_dump and pg_restore must use the same PostgreSQL major"
        )
    else:
        _require_client_supports_server(dump_version, server_major)
    return versions


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


async def preflight_dump_role(connection: AsyncConnection) -> PreflightReport:
    """Verify the source login can read every dumped application object.

    Elevated privileges are reported as non-blocking warnings.  This accepts
    both the superuser created by ``ppbase db start`` and a least-privilege
    read-capable login without imposing a dedicated-role topology.
    """
    await set_backup_control_search_path(connection)
    errors: list[str] = []
    warnings: list[str] = []
    role = (
        await connection.execute(
            text(
                """
                SELECT r.rolname, r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                       r.rolreplication, r.rolbypassrls,
                       current_setting('server_version_num')::integer
                           AS server_version_num,
                       has_database_privilege(
                           r.rolname, current_database(), 'CONNECT'
                       ) AS can_connect,
                       pg_has_role(r.rolname, 'pg_read_server_files', 'MEMBER')
                           AS read_server_files,
                       pg_has_role(r.rolname, 'pg_write_server_files', 'MEMBER')
                           AS write_server_files,
                       pg_has_role(r.rolname, 'pg_execute_server_program', 'MEMBER')
                           AS execute_server_program
                FROM pg_roles AS r
                WHERE r.rolname = current_user
                """
            )
        )
    ).mappings().one()
    forbidden_attributes = (
        "rolsuper",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
        "read_server_files",
        "write_server_files",
        "execute_server_program",
    )
    if any(bool(role[name]) for name in forbidden_attributes):
        warnings.append("backup uses a PostgreSQL role with elevated privileges")
    if not bool(role["can_connect"]):
        errors.append("dump role lacks CONNECT on the source database")

    permission_rows = (
        await connection.execute(
            text(
                """
                SELECT object_kind, qualified_name
                FROM (
                    SELECT 'schema'::text AS object_kind,
                           quote_ident(n.nspname) AS qualified_name,
                           has_schema_privilege(current_user, n.oid, 'USAGE') AS ok
                    FROM pg_namespace AS n
                    WHERE n.nspname = 'public'
                    UNION ALL
                    SELECT CASE c.relkind WHEN 'S' THEN 'sequence' ELSE 'table' END,
                           quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                           CASE c.relkind
                               WHEN 'S' THEN has_sequence_privilege(
                                   current_user, c.oid, 'SELECT'
                               )
                               ELSE has_table_privilege(current_user, c.oid, 'SELECT')
                           END
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r', 'p', 'S', 'm', 'f')
                      AND n.nspname = 'public'
                ) AS required_permissions
                WHERE NOT ok
                ORDER BY object_kind, qualified_name
                LIMIT 50
                """
            )
        )
    ).mappings().all()
    if permission_rows:
        sample = ", ".join(
            f"{row['object_kind']} {row['qualified_name']}"
            for row in permission_rows[:5]
        )
        errors.append(f"dump role lacks required read privileges: {sample}")

    forbidden_permission_rows = (
        await connection.execute(
            text(
                """
                SELECT object_kind, qualified_name, privilege
                FROM (
                    SELECT 'database'::text AS object_kind,
                           quote_ident(current_database()) AS qualified_name,
                           required.privilege
                    FROM unnest(
                        ARRAY['CREATE', 'TEMPORARY']::text[]
                    ) AS required(privilege)
                    WHERE has_database_privilege(
                        current_user, current_database(), required.privilege
                    )
                    UNION ALL
                    SELECT 'schema', quote_ident(n.nspname), 'CREATE'
                    FROM pg_namespace AS n
                    WHERE n.nspname = 'public'
                      AND has_schema_privilege(current_user, n.oid, 'CREATE')
                    UNION ALL
                    SELECT 'table',
                           quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                           required.privilege
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN unnest(CAST(
                        :forbidden_table_privileges AS text[]
                    )) AS required(privilege)
                    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
                      AND n.nspname = 'public'
                      AND has_table_privilege(
                          current_user, c.oid, required.privilege
                      )
                    UNION ALL
                    SELECT 'sequence',
                           quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                           required.privilege
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN unnest(
                        ARRAY['USAGE', 'UPDATE']::text[]
                    ) AS required(privilege)
                    WHERE c.relkind = 'S'
                      AND n.nspname = 'public'
                      AND has_sequence_privilege(
                          current_user, c.oid, required.privilege
                      )
                ) AS forbidden_permissions
                ORDER BY object_kind, qualified_name, privilege
                LIMIT 50
                """
            ),
            {
                "forbidden_table_privileges": list(
                    _forbidden_table_write_privileges(
                        int(role["server_version_num"])
                    )
                )
            },
        )
    ).mappings().all()
    if forbidden_permission_rows:
        sample = ", ".join(
            f"{row['object_kind']} {row['qualified_name']} {row['privilege']}"
            for row in forbidden_permission_rows[:5]
        )
        warnings.append(f"backup role also has write privileges: {sample}")

    rls_count = int(
        (
            await connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r', 'p')
                      AND c.relrowsecurity
                      AND n.nspname = 'public'
                    """
                )
            )
        ).scalar_one()
    )
    if rls_count and not bool(role["rolsuper"]) and not bool(role["rolbypassrls"]):
        errors.append(
            "source has row-level-security tables but the dump role may not BYPASSRLS"
        )
    return PreflightReport(errors=tuple(errors), warnings=tuple(warnings))


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


async def _run_command(
    argv: Sequence[str],
    env: Mapping[str, str],
    redact_values: Sequence[str],
    *,
    pass_fds: Sequence[int] = (),
    stdin_fd: int | None = None,
) -> CommandResult:
    if not argv:
        raise ValueError("argv cannot be empty")
    try:
        process_options: dict[str, Any] = {
            "stdin": (
                stdin_fd
                if stdin_fd is not None
                else asyncio.subprocess.DEVNULL
            ),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": dict(env),
        }
        if pass_fds:
            process_options["pass_fds"] = tuple(pass_fds)
        process = await asyncio.create_subprocess_exec(*argv, **process_options)
    except OSError as exc:
        safe_error = _redact(str(exc), redact_values)
        raise PostgresCommandError(str(argv[0]), -1, safe_error) from None
    try:
        stdout_bytes, stderr_bytes = await process.communicate()
    except BaseException:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        raise
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = _redact(
        stderr_bytes.decode("utf-8", errors="replace").strip(),
        redact_values,
    )
    result = CommandResult(tuple(str(value) for value in argv), process.returncode, stdout, stderr)
    if process.returncode != 0:
        raise PostgresCommandError(str(argv[0]), process.returncode, stderr)
    return result


async def _read_limited_stream(
    stream: asyncio.StreamReader | None,
    *,
    limit: int = 1024 * 1024,
) -> bytes:
    """Drain a child stream without allowing diagnostics to grow unbounded."""
    if stream is None:
        return b""
    chunks: list[bytes] = []
    retained = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        if retained < limit:
            selected = chunk[: limit - retained]
            chunks.append(selected)
            retained += len(selected)
    return b"".join(chunks)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Stop and reap a child process during cancellation or pipe failure."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


def _postgres_environment(passfile: Path | None) -> dict[str, str]:
    # A caller-controlled PGOPTIONS/PGSERVICE/PGPASSWORD could silently change
    # the effective role, target, TLS policy, or credentials.  The explicit
    # conninfo is authoritative, so no ambient libpq setting is inherited.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    if passfile is not None:
        env["PGPASSFILE"] = str(passfile)
    return env


def _reserve_output_file(path: Path) -> tuple[int, int]:
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError:
        raise PostgresBackupError(
            "pg_dump destination already exists; overwrite is forbidden"
        ) from None
    except OSError as exc:
        raise PostgresBackupError(f"could not reserve pg_dump destination: {exc}") from None
    else:
        os.fchmod(fd, 0o600)
        stat_result = os.fstat(fd)
        os.close(fd)
        return stat_result.st_dev, stat_result.st_ino


def _unlink_reserved_output(path: Path, identity: tuple[int, int]) -> None:
    """Remove only the inode this function reserved, never a replacement."""
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or (stat_result.st_dev, stat_result.st_ino) != identity:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _require_regular_file(path: str | Path, label: str) -> Path:
    result = Path(path)
    if not result.is_file() or result.is_symlink():
        raise PostgresBackupError(f"{label} must be a regular, non-symlink file")
    return result


def _require_expected_major(
    version: PostgresToolVersion,
    expected_server_major: int | None,
) -> None:
    if expected_server_major is not None and version.major != expected_server_major:
        raise PostgresVersionMismatchError(
            f"expected PostgreSQL major {expected_server_major}, but "
            f"{Path(version.executable).name} is major {version.major}"
        )


def _require_client_supports_server(
    version: PostgresToolVersion,
    expected_server_major: int | None,
) -> None:
    """Allow one newer client wheel to support each declared server major."""
    if expected_server_major is not None and version.major < expected_server_major:
        raise PostgresVersionMismatchError(
            f"PostgreSQL server major is {expected_server_major}, but "
            f"{Path(version.executable).name} is older at major {version.major}"
        )


def _require_public_only_archive_toc(toc: str) -> None:
    """Reject archives which can restore an application schema other than public."""
    unexpected: set[str] = set()
    saw_public = False
    for raw_line in toc.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        matched = re.match(r"^\d+;\s+\d+\s+\d+\s+(.+)$", line)
        if matched is None:
            raise PostgresBackupError(
                "pg_restore archive TOC contains an unrecognized entry"
            )
        descriptor = matched.group(1)
        schema_entry = re.match(r"^SCHEMA\s+-\s+(\S+)(?:\s|$)", descriptor)
        if schema_entry is not None:
            schema = schema_entry.group(1)
            if schema == "public":
                saw_public = True
            else:
                unexpected.add(schema)
            continue
        schema_metadata = re.match(
            r"^(?:COMMENT|SECURITY LABEL|ACL)\s+-\s+SCHEMA\s+(\S+)(?:\s|$)",
            descriptor,
        )
        if schema_metadata is not None:
            schema = schema_metadata.group(1)
            if schema == "public":
                saw_public = True
            else:
                unexpected.add(schema)
            continue
        for object_type in _PUBLIC_SCHEMA_TOC_TYPES:
            prefix = f"{object_type} "
            if not descriptor.startswith(prefix):
                continue
            namespace = descriptor[len(prefix):].split(None, 1)[0]
            if namespace == "public":
                saw_public = True
            elif namespace != "-":
                unexpected.add(namespace)
            break
    if unexpected:
        raise PostgresBackupError(
            "destructive restore supports only the public application schema; "
            "archive also contains: " + ", ".join(sorted(unexpected))
        )
    if not saw_public:
        raise PostgresBackupError(
            "destructive restore archive does not contain the public application schema"
        )


def _forbidden_table_write_privileges(
    server_version_num: int,
) -> tuple[str, ...]:
    privileges = (
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    )
    if server_version_num >= 170000:
        return (*privileges, "MAINTAIN")
    return privileges


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


def _escape_pgpass_field(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _quote_identifier(value: str) -> str:
    validate_postgres_identifier(value)
    return f'"{value}"'


def _quote_catalog_identifier(value: str, *, label: str = "identifier") -> str:
    """Quote an identifier read from PostgreSQL without narrowing its syntax."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PostgresContractError(f"{label} returned by PostgreSQL is invalid")
    return '"' + value.replace('"', '""') + '"'


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _redaction_values(connection: LibpqConnectionInfo) -> tuple[str, ...]:
    if not connection.password:
        return ()
    return (
        connection.password,
        quote(connection.password, safe=""),
    )


def _redact(value: str, redactions: Sequence[str]) -> str:
    result = value
    for secret_value in redactions:
        if secret_value:
            result = result.replace(secret_value, "[REDACTED]")
    return result
