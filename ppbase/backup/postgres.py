"""PostgreSQL primitives for native backup and staging restore.

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


class TargetDatabaseExistsError(PostgresBackupError):
    """Raised before restore when the requested staging DB already exists."""


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
class RestoreResult:
    archive: Path
    target_database: str
    target_owner: str
    client_version: PostgresToolVersion
    toc: str
    stdout: str


@dataclass(frozen=True)
class CreateDatabaseResult:
    database: str
    owner: str
    creator_role: str
    marker_comment: str = field(repr=False)


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


def _validate_target_owner(value: str) -> str:
    """Require an application-dedicated owner, never a predefined pg_* role."""
    owner = validate_postgres_identifier(value, label="target owner")
    if owner.startswith("pg_"):
        raise PostgresContractError(
            "target owner must be a dedicated role and cannot use the reserved pg_ prefix"
        )
    return owner


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


def replace_sqlalchemy_database(
    database_url: str | URL,
    database: str,
) -> URL:
    """Return the same credential/options URL pointed at a new validated DB.

    Returning SQLAlchemy's immutable ``URL`` avoids rendering the password into
    an intermediate string.  ``run_pg_restore`` accepts this object directly.
    """
    target = validate_postgres_identifier(database, label="target database")
    try:
        url = database_url if isinstance(database_url, URL) else make_url(database_url)
    except Exception:
        raise InvalidDatabaseUrlError("invalid PostgreSQL database URL") from None
    if url.drivername != "postgresql+asyncpg":
        raise InvalidDatabaseUrlError(
            "database URL must use the postgresql+asyncpg driver"
        )
    return url.set(database=target)


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


async def _run_with_operation_pgpass(
    runner: CommandRunner,
    argv: Sequence[str],
    env: Mapping[str, str],
    redact_values: Sequence[str],
    passfile: _OperationPgpass,
) -> CommandResult:
    """Rewind an anonymous passfile and inherit it only for the real runner."""
    passfile.rewind()
    if runner is _run_command and passfile.inherited_fds:
        return await _run_command(
            argv,
            env,
            redact_values,
            pass_fds=passfile.inherited_fds,
        )
    return await runner(argv, env, redact_values)


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
    """Write one custom-format dump without owners, ACLs, or tablespaces."""
    connection = sqlalchemy_url_to_libpq(database_url)
    output = Path(destination)
    if not output.parent.is_dir():
        raise PostgresBackupError("pg_dump destination parent does not exist")

    command_runner = runner or _run_command
    version = await detect_postgres_tool_version(pg_dump, runner=command_runner)
    _require_expected_major(version, expected_server_major)
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
    """Ask pg_restore to parse and list an archive before any DB mutation."""
    archive_path = _require_regular_file(archive, "restore archive")
    command_runner = runner or _run_command
    version = await detect_postgres_tool_version(pg_restore, runner=command_runner)
    _require_expected_major(version, expected_server_major)
    result = await command_runner(
        (pg_restore, "--list", str(archive_path)),
        _postgres_environment(None),
        (),
    )
    return RestoreArchiveInspection(
        archive=archive_path,
        client_version=version,
        toc=result.stdout,
    )


async def run_pg_restore(
    restore_database_url: str | URL,
    archive: str | Path,
    *,
    target_owner: str,
    pg_restore: str = "pg_restore",
    expected_server_major: int | None = None,
    passfile_directory: str | Path | None = None,
    runner: CommandRunner | None = None,
) -> RestoreResult:
    """List, then restore an archive into an already-new staging database.

    The command intentionally has no API for ``--clean`` or ``--create``.
    """
    owner = _validate_target_owner(target_owner)
    connection = sqlalchemy_url_to_libpq(restore_database_url)
    restore_role = validate_postgres_identifier(
        connection.username,
        label="restore role",
    )
    if restore_role == owner:
        raise PostgresContractError(
            "restore login role and target owner must be distinct"
        )

    command_runner = runner or _run_command
    inspection = await inspect_pg_restore_archive(
        archive,
        pg_restore=pg_restore,
        expected_server_major=expected_server_major,
        runner=command_runner,
    )
    argv = (
        pg_restore,
        "--single-transaction",
        "--exit-on-error",
        "--no-owner",
        "--no-acl",
        "--no-tablespaces",
        "--no-password",
        f"--role={owner}",
        "--dbname",
        connection.conninfo,
        str(inspection.archive),
    )
    with temporary_pgpass(connection, directory=passfile_directory) as passfile:
        env = _postgres_environment(passfile)
        result = await command_runner(argv, env, _redaction_values(connection))
    return RestoreResult(
        archive=inspection.archive,
        target_database=connection.database,
        target_owner=owner,
        client_version=inspection.client_version,
        toc=inspection.toc,
        stdout=result.stdout,
    )


async def run_pg_restore_from_fd(
    restore_database_url: str | URL,
    archive_fd: int,
    *,
    archive_label: str | Path,
    target_owner: str,
    pg_restore: str = "pg_restore",
    expected_server_major: int | None = None,
    passfile_directory: str | Path | None = None,
    passfile_factory: Callable[[], BinaryIO] | None = None,
) -> RestoreResult:
    """Restore the exact regular-file inode already verified by the caller.

    The descriptor is inherited as standard input by both pg_restore
    processes. No reopenable backup-store path is consumed between signature
    verification and restore.
    """
    if os.name != "posix":
        raise PostgresBackupError(
            "descriptor-pinned pg_restore requires POSIX descriptor support"
        )
    if isinstance(archive_fd, bool) or not isinstance(archive_fd, int) or archive_fd < 0:
        raise PostgresBackupError("pinned restore archive descriptor is invalid")
    try:
        archive_info = os.fstat(archive_fd)
    except OSError as exc:
        raise PostgresBackupError(
            "pinned restore archive descriptor is not open"
        ) from exc
    if not stat.S_ISREG(archive_info.st_mode) or archive_info.st_size <= 0:
        raise PostgresBackupError(
            "pinned restore archive must be a non-empty regular file"
        )

    owner = _validate_target_owner(target_owner)
    connection = sqlalchemy_url_to_libpq(restore_database_url)
    restore_role = validate_postgres_identifier(
        connection.username,
        label="restore role",
    )
    if restore_role == owner:
        raise PostgresContractError(
            "restore login role and target owner must be distinct"
        )

    version = await detect_postgres_tool_version(pg_restore, runner=_run_command)
    _require_expected_major(version, expected_server_major)
    os.lseek(archive_fd, 0, os.SEEK_SET)
    list_result = await _run_command(
        (pg_restore, "--list"),
        _postgres_environment(None),
        (),
        stdin_fd=archive_fd,
    )
    os.lseek(archive_fd, 0, os.SEEK_SET)
    argv = (
        pg_restore,
        "--single-transaction",
        "--exit-on-error",
        "--no-owner",
        "--no-acl",
        "--no-tablespaces",
        "--no-password",
        f"--role={owner}",
        "--dbname",
        connection.conninfo,
    )
    with _operation_pgpass(
        connection,
        directory=passfile_directory,
        file_factory=passfile_factory,
    ) as passfile:
        passfile.rewind()
        result = await _run_command(
            argv,
            _postgres_environment(passfile.path),
            _redaction_values(connection),
            pass_fds=passfile.inherited_fds,
            stdin_fd=archive_fd,
        )
    return RestoreResult(
        archive=Path(archive_label),
        target_database=connection.database,
        target_owner=owner,
        client_version=version,
        toc=list_result.stdout,
        stdout=result.stdout,
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
    require_same_major: bool = True,
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
    """Verify the current source login can dump data without elevated roles."""
    await set_backup_control_search_path(connection)
    errors: list[str] = []
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
        errors.append("dump role has forbidden PostgreSQL cluster privileges")
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
                    WHERE n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
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
                      AND n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                    UNION ALL
                    SELECT 'large_object', large_object.oid::text,
                           pg_has_role(
                               current_role_oid.oid,
                               large_object.lomowner,
                               'USAGE'
                           ) OR EXISTS (
                               SELECT 1
                               FROM aclexplode(COALESCE(
                                   large_object.lomacl,
                                   acldefault('L', large_object.lomowner)
                               )) AS privilege
                               WHERE privilege.privilege_type = 'SELECT'
                                 AND CASE
                                     WHEN privilege.grantee = 0 THEN true
                                     ELSE pg_has_role(
                                         current_role_oid.oid,
                                         privilege.grantee,
                                         'USAGE'
                                     )
                                 END
                           )
                    FROM pg_largeobject_metadata AS large_object
                    CROSS JOIN (
                        SELECT oid FROM pg_roles WHERE rolname = current_user
                    ) AS current_role_oid
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
                    WHERE n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
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
                      AND n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
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
                      AND n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                      AND has_sequence_privilege(
                          current_user, c.oid, required.privilege
                      )
                    UNION ALL
                    SELECT 'large_object', large_object.oid::text, 'UPDATE'
                    FROM pg_largeobject_metadata AS large_object
                    CROSS JOIN (
                        SELECT oid FROM pg_roles WHERE rolname = current_user
                    ) AS current_role_oid
                    WHERE pg_has_role(
                        current_role_oid.oid,
                        large_object.lomowner,
                        'USAGE'
                    ) OR EXISTS (
                        SELECT 1
                        FROM aclexplode(COALESCE(
                            large_object.lomacl,
                            acldefault('L', large_object.lomowner)
                        )) AS granted
                        WHERE granted.privilege_type = 'UPDATE'
                          AND CASE
                              WHEN granted.grantee = 0 THEN true
                              ELSE pg_has_role(
                                  current_role_oid.oid,
                                  granted.grantee,
                                  'USAGE'
                              )
                          END
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
        errors.append(f"dump role has forbidden write privileges: {sample}")

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
                      AND n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                    """
                )
            )
        ).scalar_one()
    )
    if rls_count:
        errors.append(
            "source has row-level-security tables but the dump role may not BYPASSRLS"
        )
    return PreflightReport(errors=tuple(errors))


async def preflight_database_contract(
    connection: AsyncConnection,
    source: DatabaseContract,
    *,
    creator_role: str,
    restore_role: str,
    target_owner: str,
    allowed_extensions: Mapping[str, str] | None = None,
) -> PreflightReport:
    """Check source portability and exact target role/extension prerequisites."""
    await set_backup_control_search_path(connection)
    creator = validate_postgres_identifier(creator_role, label="creator role")
    restore = validate_postgres_identifier(restore_role, label="restore role")
    owner = _validate_target_owner(target_owner)
    _require_distinct_roles(creator, restore, owner)
    allowlist = dict(allowed_extensions or {})
    errors: list[str] = []
    warnings: list[str] = []

    target_version_num = int(
        (
            await connection.execute(
                text("SELECT current_setting('server_version_num')::integer")
            )
        ).scalar_one()
    )
    target_major = target_version_num // 10000
    if source.server_major != target_major:
        errors.append(
            f"source PostgreSQL major {source.server_major} differs from target "
            f"major {target_major}"
        )
    if source.encoding.upper().replace("-", "") != "UTF8":
        errors.append(f"source encoding {source.encoding!r} is not UTF-8")
    if source.locale_provider not in {"libc", "icu"}:
        errors.append(
            f"source locale provider {source.locale_provider!r} is unsupported"
        )
    if (
        source.collation_version
        and source.actual_collation_version
        and source.collation_version != source.actual_collation_version
    ):
        errors.append(
            "source database collation recorded version differs from its actual version"
        )
    for collation in source.collations:
        if (
            collation.version
            and collation.actual_version
            and collation.version != collation.actual_version
        ):
            errors.append(
                "source collation "
                f"{collation.schema}.{collation.name} recorded version differs "
                "from its actual version"
            )

    role_report = await _preflight_target_roles(
        connection,
        creator=creator,
        restore=restore,
        owner=owner,
    )
    errors.extend(role_report.errors)
    warnings.extend(role_report.warnings)

    available_rows = (
        await connection.execute(
            text(
                """
                SELECT v.name, v.version,
                       COALESCE((to_jsonb(v)->>'superuser')::boolean, true)
                           AS superuser,
                       COALESCE((to_jsonb(v)->>'trusted')::boolean, false)
                           AS trusted
                FROM pg_available_extension_versions AS v
                """
            )
        )
    ).mappings().all()
    available = {
        (str(row["name"]), str(row["version"])): (
            bool(row["superuser"]),
            bool(row["trusted"]),
        )
        for row in available_rows
    }
    for extension in source.extensions:
        key = (extension.name, extension.version)
        metadata = available.get(key)
        if extension.name != "plpgsql":
            if allowlist.get(extension.name) != extension.version:
                errors.append(
                    f"extension {extension.name}={extension.version} is not allowlisted"
                )
            if metadata is not None and metadata[0] and not metadata[1]:
                errors.append(
                    f"extension {extension.name}={extension.version} requires superuser"
                )
        if metadata is None:
            errors.append(
                f"extension {extension.name}={extension.version} is unavailable on target"
            )

    locale_report = await _preflight_locale(connection, source)
    errors.extend(locale_report.errors)
    warnings.extend(locale_report.warnings)

    distinct_owners = sorted(
        {
            object_owner
            for summary in source.object_security
            for object_owner in summary.owners
        }
    )
    acl_count = sum(item.acl_object_count for item in source.object_security)
    if source.database_acl or acl_count:
        warnings.append(
            "source database/object ACLs will be intentionally omitted during restore"
        )
    if distinct_owners and distinct_owners != [owner]:
        warnings.append(
            "source object owners will be remapped to the target owner "
            f"{owner!r}"
        )
    return PreflightReport(errors=tuple(errors), warnings=tuple(warnings))


async def create_target_database(
    creator_database_url: str | URL,
    target_database: str,
    *,
    target_owner: str,
    restore_role: str,
    contract: DatabaseContract,
    expected_server_identity: Mapping[str, Any],
    psql: str = "psql",
    passfile_directory: str | Path | None = None,
    passfile_factory: Callable[[], BinaryIO] | None = None,
    runner: CommandRunner | None = None,
) -> CreateDatabaseResult:
    """Create a brand-new staging database from ``template0`` only."""
    target = validate_postgres_identifier(target_database, label="target database")
    owner = _validate_target_owner(target_owner)
    restore = validate_postgres_identifier(restore_role, label="restore role")
    connection = sqlalchemy_url_to_libpq(creator_database_url)
    creator = validate_postgres_identifier(
        connection.username,
        label="creator role",
    )
    _require_distinct_roles(creator, restore, owner)
    if target == connection.database:
        raise PostgresContractError(
            "target database must differ from the creator maintenance database"
        )
    if contract.encoding.upper().replace("-", "") != "UTF8":
        raise PostgresContractError("target database contract requires UTF-8")
    if contract.locale_provider not in {"libc", "icu"}:
        raise PostgresContractError("target locale provider must be libc or icu")
    create_precondition_sql = _create_target_precondition_sql(
        target=target,
        creator=creator,
        restore=restore,
        owner=owner,
        maintenance_database=connection.database,
        expected_server_identity=expected_server_identity,
    )

    command_runner = runner or _run_command
    with _operation_pgpass(
        connection,
        directory=passfile_directory,
        file_factory=passfile_factory,
    ) as passfile:
        env = _postgres_environment(passfile.path)
        redactions = _redaction_values(connection)
        check_result = await _run_with_operation_pgpass(
            command_runner,
            _psql_command(
                psql,
                connection.conninfo,
                _role_and_target_check_sql(
                    target=target,
                    creator=creator,
                    restore=restore,
                    owner=owner,
                ),
            ),
            env,
            redactions,
            passfile,
        )
        output_lines = [
            line.strip()
            for line in check_result.stdout.splitlines()
            if line.strip()
        ]
        values = output_lines[-1].split("|") if output_lines else []
        if len(values) != 9:
            raise PostgresContractError(
                "could not verify creator/restore/owner role contract"
            )
        (
            effective_creator,
            target_exists,
            creator_ok,
            restore_ok,
            owner_ok,
            creator_member,
            restore_member,
            forbidden_membership,
            unexpected_membership,
        ) = values
        if effective_creator != creator:
            raise PostgresContractError(
                "creator DSN authenticated as an unexpected effective role"
            )
        if _parse_psql_bool(target_exists):
            raise TargetDatabaseExistsError(
                f"target database {target!r} already exists"
            )
        if not _parse_psql_bool(creator_ok):
            raise PostgresContractError(
                "creator role must be LOGIN CREATEDB NOINHERIT without other "
                "cluster attributes"
            )
        if not _parse_psql_bool(restore_ok):
            raise PostgresContractError(
                "restore role must be LOGIN NOINHERIT without CREATEDB or "
                "elevated attributes"
            )
        if not _parse_psql_bool(owner_ok):
            raise PostgresContractError(
                "target owner must be NOLOGIN without elevated cluster attributes"
            )
        if not _parse_psql_bool(creator_member) or not _parse_psql_bool(
            restore_member
        ):
            raise PostgresContractError(
                "creator and restore roles must have direct SET-only, "
                "non-admin, non-inherited membership in the target owner"
            )
        if _parse_psql_bool(forbidden_membership):
            raise PostgresContractError(
                "creator or restore role has a forbidden server-file/program role"
            )
        if _parse_psql_bool(unexpected_membership):
            raise PostgresContractError(
                "creator/restore/owner membership graph extends beyond the "
                "target owner"
            )

        marker_comment = f"ppbase-restore-marker:{secrets.token_hex(32)}"
        await _run_with_operation_pgpass(
            command_runner,
            _psql_command(
                psql,
                connection.conninfo,
                create_precondition_sql,
                _create_database_sql(target, owner, contract),
                _normalize_database_acl_sql(
                    target,
                    owner,
                    restore,
                    marker_comment,
                ),
            ),
            env,
            (*redactions, marker_comment),
            passfile,
        )
    return CreateDatabaseResult(
        database=target,
        owner=owner,
        creator_role=creator,
        marker_comment=marker_comment,
    )


async def _preflight_target_roles(
    connection: AsyncConnection,
    *,
    creator: str,
    restore: str,
    owner: str,
) -> PreflightReport:
    row = (
        await connection.execute(
            text(
                _role_and_target_check_sql(
                    target="__ppbase_preflight_nonexistent__",
                    creator=creator,
                    restore=restore,
                    owner=owner,
                )
            )
        )
    ).mappings().one()
    errors: list[str] = []
    if str(row["effective_creator"]) != creator:
        errors.append("preflight connection is not authenticated as creator role")
    if not bool(row["creator_ok"]):
        errors.append("creator role attributes do not match the native restore contract")
    if not bool(row["restore_ok"]):
        errors.append("restore role attributes do not match the native restore contract")
    if not bool(row["owner_ok"]):
        errors.append("target owner attributes do not match the native restore contract")
    if not bool(row["creator_member"]):
        errors.append(
            "creator role lacks direct SET-only, non-admin, non-inherited "
            "membership in target owner"
        )
    if not bool(row["restore_member"]):
        errors.append(
            "restore role lacks direct SET-only, non-admin, non-inherited "
            "membership in target owner"
        )
    if bool(row["forbidden_membership"]):
        errors.append("creator/restore role has server-file or program privileges")
    if bool(row["unexpected_membership"]):
        errors.append(
            "creator/restore/owner membership graph extends beyond the target owner"
        )
    return PreflightReport(errors=tuple(errors))


async def _preflight_locale(
    connection: AsyncConnection,
    source: DatabaseContract,
) -> PreflightReport:
    errors: list[str] = []
    rows = (
        await connection.execute(
            text(
                """
                SELECT c.collprovider::text AS provider,
                       n.nspname AS schema,
                       c.collname AS name,
                       to_jsonb(c)->>'collcollate' AS collate,
                       to_jsonb(c)->>'collctype' AS ctype,
                       COALESCE(
                           to_jsonb(c)->>'colllocale',
                           to_jsonb(c)->>'colliculocale'
                       ) AS locale,
                       to_jsonb(c)->>'collicurules' AS rules,
                       c.collversion AS version,
                       pg_collation_actual_version(c.oid) AS actual_version
                FROM pg_collation AS c
                JOIN pg_namespace AS n ON n.oid = c.collnamespace
                """
            )
        )
    ).mappings().all()
    normalized_rows = [
        {
            "provider": _normalize_provider(row["provider"]),
            "schema": str(row["schema"]),
            "name": str(row["name"]),
            "collate": _optional_str(row["collate"]),
            "ctype": _optional_str(row["ctype"]),
            "locale": _optional_str(row["locale"]),
            "rules": _optional_str(row["rules"]),
            "version": _optional_str(row["version"]),
            "actual_version": _optional_str(row["actual_version"]),
        }
        for row in rows
    ]
    # Some minimal libc images can create a locale without importing a named
    # pg_collation row for it.  An existing DB on the target cluster is still
    # authoritative proof that PostgreSQL can use that exact provider/locale.
    database_locale_rows = (
        await connection.execute(
            text(
                """
                SELECT d.datlocprovider::text AS provider,
                       d.datcollate AS collate,
                       d.datctype AS ctype,
                       COALESCE(
                           to_jsonb(d)->>'datlocale',
                           to_jsonb(d)->>'daticulocale'
                       ) AS locale,
                       to_jsonb(d)->>'daticurules' AS rules,
                       to_jsonb(d)->>'datcollversion' AS version,
                       pg_database_collation_actual_version(d.oid)
                           AS actual_version
                FROM pg_database AS d
                """
            )
        )
    ).mappings().all()
    normalized_rows.extend(
        {
            "provider": _normalize_provider(row["provider"]),
            "schema": None,
            "name": None,
            "collate": _optional_str(row["collate"]),
            "ctype": _optional_str(row["ctype"]),
            "locale": _optional_str(row["locale"]),
            "rules": _optional_str(row["rules"]),
            "version": _optional_str(row["version"]),
            "actual_version": _optional_str(row["actual_version"]),
        }
        for row in database_locale_rows
    )

    if source.locale_provider == "libc":
        candidates = [
            row
            for row in normalized_rows
            if row["provider"] == "libc"
            and row["collate"] == source.collate
            and row["ctype"] == source.ctype
        ]
    elif source.locale_provider == "icu":
        candidates = [
            row
            for row in normalized_rows
            if row["provider"] == "icu"
            and row["locale"] == source.icu_locale
            and row["rules"] == source.icu_rules
        ]
        # ICU's version is library-wide.  A custom valid locale may not have a
        # catalog row yet, so any ICU collation with the exact version proves
        # compatible target ICU data when locale-name matching is unavailable.
        if (
            not candidates
            and source.icu_rules is None
            and source.actual_collation_version
        ):
            candidates = [
                row
                for row in normalized_rows
                if row["provider"] == "icu"
                and row["actual_version"] == source.actual_collation_version
            ]
    else:
        return PreflightReport(
            errors=(f"unsupported locale provider {source.locale_provider!r}",)
        )

    if not candidates:
        errors.append("source database locale is unavailable on target")
    elif not any(
        row["version"] == source.collation_version
        and row["actual_version"] == source.actual_collation_version
        for row in candidates
    ):
        errors.append("source and target collation versions differ")

    target_collations = {
        (
            row["provider"],
            row["schema"],
            row["name"],
            row["collate"],
            row["ctype"],
            row["locale"],
            row["rules"],
            row["version"],
            row["actual_version"],
        )
        for row in normalized_rows
    }
    for collation in source.collations:
        if collation.schema != "pg_catalog":
            # Extension-owned collations are supplied by the separately
            # allowlisted extension and do not exist before restore.
            continue
        signature = (
            collation.provider,
            collation.schema,
            collation.name,
            collation.collate,
            collation.ctype,
            collation.locale,
            collation.rules,
            collation.version,
            collation.actual_version,
        )
        if signature not in target_collations:
            errors.append(
                f"required collation pg_catalog.{collation.name} is incompatible"
            )
    return PreflightReport(errors=tuple(errors))


def _role_and_target_check_sql(
    *,
    target: str,
    creator: str,
    restore: str,
    owner: str,
) -> str:
    target_literal = _sql_literal(target)
    creator_literal = _sql_literal(creator)
    restore_literal = _sql_literal(restore)
    owner_literal = _sql_literal(owner)
    return f"""
        SELECT
            current_user AS effective_creator,
            EXISTS (
                SELECT 1
                FROM pg_catalog.pg_database
                WHERE datname = {target_literal}
            ) AS target_exists,
            EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = {creator_literal}
                  AND rolcanlogin AND rolcreatedb AND NOT rolinherit
                  AND NOT rolsuper AND NOT rolcreaterole
                  AND NOT rolreplication AND NOT rolbypassrls
            ) AS creator_ok,
            EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = {restore_literal}
                  AND rolcanlogin AND NOT rolcreatedb AND NOT rolinherit
                  AND NOT rolsuper AND NOT rolcreaterole
                  AND NOT rolreplication AND NOT rolbypassrls
            ) AS restore_ok,
            EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = {owner_literal}
                  AND NOT rolcanlogin AND NOT rolcreatedb
                  AND NOT rolsuper AND NOT rolcreaterole
                  AND NOT rolreplication AND NOT rolbypassrls
            ) AS owner_ok,
            EXISTS (
                SELECT 1
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS member_role
                  ON member_role.oid = membership.member
                JOIN pg_catalog.pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                WHERE member_role.rolname = {creator_literal}
                  AND granted_role.rolname = {owner_literal}
                  AND NOT membership.admin_option
                  AND COALESCE(
                      (pg_catalog.to_jsonb(membership)->>'set_option')::boolean,
                      true
                  )
                  AND NOT COALESCE(
                      (pg_catalog.to_jsonb(membership)->>'inherit_option')::boolean,
                      member_role.rolinherit
                  )
            ) AND NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS member_role
                  ON member_role.oid = membership.member
                JOIN pg_catalog.pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                WHERE member_role.rolname = {creator_literal}
                  AND granted_role.rolname = {owner_literal}
                  AND (
                      membership.admin_option
                      OR NOT COALESCE(
                          (pg_catalog.to_jsonb(membership)->>'set_option')::boolean,
                          true
                      )
                      OR COALESCE(
                          (pg_catalog.to_jsonb(membership)->>'inherit_option')::boolean,
                          member_role.rolinherit
                      )
                  )
            ) AS creator_member,
            EXISTS (
                SELECT 1
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS member_role
                  ON member_role.oid = membership.member
                JOIN pg_catalog.pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                WHERE member_role.rolname = {restore_literal}
                  AND granted_role.rolname = {owner_literal}
                  AND NOT membership.admin_option
                  AND COALESCE(
                      (pg_catalog.to_jsonb(membership)->>'set_option')::boolean,
                      true
                  )
                  AND NOT COALESCE(
                      (pg_catalog.to_jsonb(membership)->>'inherit_option')::boolean,
                      member_role.rolinherit
                  )
            ) AND NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS member_role
                  ON member_role.oid = membership.member
                JOIN pg_catalog.pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                WHERE member_role.rolname = {restore_literal}
                  AND granted_role.rolname = {owner_literal}
                  AND (
                      membership.admin_option
                      OR NOT COALESCE(
                          (pg_catalog.to_jsonb(membership)->>'set_option')::boolean,
                          true
                      )
                      OR COALESCE(
                          (pg_catalog.to_jsonb(membership)->>'inherit_option')::boolean,
                          member_role.rolinherit
                      )
                  )
            ) AS restore_member,
            EXISTS (
                SELECT 1
                FROM (VALUES ({creator_literal}), ({restore_literal})) AS r(role_name)
                CROSS JOIN (
                    VALUES ('pg_read_server_files'),
                           ('pg_write_server_files'),
                           ('pg_execute_server_program')
                ) AS f(role_name)
                WHERE COALESCE(pg_catalog.pg_has_role(
                    (
                        SELECT oid FROM pg_catalog.pg_roles
                        WHERE rolname = r.role_name
                    ),
                    (
                        SELECT oid FROM pg_catalog.pg_roles
                        WHERE rolname = f.role_name
                    ),
                    'MEMBER'
                ), false)
            ) AS forbidden_membership,
            EXISTS (
                SELECT 1
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS member_role
                  ON member_role.oid = membership.member
                JOIN pg_catalog.pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                WHERE (
                    member_role.rolname IN (
                        {creator_literal}, {restore_literal}, {owner_literal}
                    )
                    OR granted_role.rolname IN (
                        {creator_literal}, {restore_literal}, {owner_literal}
                    )
                ) AND NOT (
                    member_role.rolname IN ({creator_literal}, {restore_literal})
                    AND granted_role.rolname = {owner_literal}
                    AND NOT membership.admin_option
                    AND COALESCE(
                        (pg_catalog.to_jsonb(membership)->>'set_option')::boolean,
                        true
                    )
                    AND NOT COALESCE(
                        (pg_catalog.to_jsonb(membership)->>'inherit_option')::boolean,
                        member_role.rolinherit
                    )
                )
            ) AS unexpected_membership
    """.strip()


def _create_target_precondition_sql(
    *,
    target: str,
    creator: str,
    restore: str,
    owner: str,
    maintenance_database: str,
    expected_server_identity: Mapping[str, Any],
) -> str:
    """Assert the sealed destination on the psql session that will mutate it."""
    try:
        expected_role = str(expected_server_identity["role"])
        expected_database = str(expected_server_identity["database"])
        expected_address = str(expected_server_identity["server_address"])
        expected_port_raw = expected_server_identity["server_port"]
        expected_started_at = str(
            expected_server_identity["postmaster_started_at"]
        )
        expected_version = str(expected_server_identity["server_version_num"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PostgresContractError(
            "expected PostgreSQL server identity is incomplete"
        ) from exc
    if isinstance(expected_port_raw, bool):
        raise PostgresContractError("expected PostgreSQL server port is invalid")
    try:
        expected_port = int(expected_port_raw)
    except (TypeError, ValueError) as exc:
        raise PostgresContractError(
            "expected PostgreSQL server port is invalid"
        ) from exc
    if not 0 <= expected_port <= 65535:
        raise PostgresContractError("expected PostgreSQL server port is invalid")
    if expected_role != creator or expected_database != maintenance_database:
        raise PostgresContractError(
            "expected PostgreSQL identity does not match the creator DSN"
        )
    if not expected_started_at or not expected_version.isdigit():
        raise PostgresContractError("expected PostgreSQL server identity is invalid")

    role_check_sql = _role_and_target_check_sql(
        target=target,
        creator=creator,
        restore=restore,
        owner=owner,
    )
    creator_literal = _sql_literal(creator)
    database_literal = _sql_literal(maintenance_database)
    address_literal = _sql_literal(expected_address)
    started_literal = _sql_literal(expected_started_at)
    version_literal = _sql_literal(expected_version)
    return f"""
        DO $ppbase_create_guard$
        DECLARE
            role_check record;
        BEGIN
            IF current_user <> {creator_literal}
               OR pg_catalog.current_database() <> {database_literal}
            THEN
                RAISE EXCEPTION
                    'PPBase restore destination role/database changed before create';
            END IF;
            IF COALESCE(pg_catalog.inet_server_addr()::text, '')
                    <> {address_literal}
            THEN
                RAISE EXCEPTION
                    'PPBase restore destination address changed before create';
            END IF;
            IF COALESCE(pg_catalog.inet_server_port(), 0) <> {expected_port}
            THEN
                RAISE EXCEPTION
                    'PPBase restore destination port changed before create';
            END IF;
            IF EXTRACT(
                    EPOCH FROM pg_catalog.pg_postmaster_start_time()
               )::text
                    <> {started_literal}
               OR pg_catalog.current_setting('server_version_num')
                    <> {version_literal}
            THEN
                RAISE EXCEPTION
                    'PPBase restore server instance changed before create';
            END IF;

            SELECT * INTO role_check FROM ({role_check_sql}) AS checked_roles;
            IF role_check.effective_creator <> {creator_literal}
               OR role_check.target_exists
               OR NOT role_check.creator_ok
               OR NOT role_check.restore_ok
               OR NOT role_check.owner_ok
               OR NOT role_check.creator_member
               OR NOT role_check.restore_member
               OR role_check.forbidden_membership
               OR role_check.unexpected_membership
            THEN
                RAISE EXCEPTION
                    'PPBase restore role or target contract changed before create';
            END IF;
        END
        $ppbase_create_guard$
    """.strip()


def _create_database_sql(
    target: str,
    owner: str,
    contract: DatabaseContract,
) -> str:
    clauses = [
        f"CREATE DATABASE {_quote_identifier(target)}",
        "WITH TEMPLATE template0",
        f"OWNER {_quote_identifier(owner)}",
        "ENCODING 'UTF8'",
        "ALLOW_CONNECTIONS false",
        f"LOCALE_PROVIDER {contract.locale_provider}",
        f"LC_COLLATE {_sql_literal(contract.collate)}",
        f"LC_CTYPE {_sql_literal(contract.ctype)}",
    ]
    if contract.locale_provider == "icu":
        if not contract.icu_locale:
            raise PostgresContractError("ICU database contract lacks ICU locale")
        clauses.append(f"ICU_LOCALE {_sql_literal(contract.icu_locale)}")
        if contract.icu_rules:
            clauses.append(f"ICU_RULES {_sql_literal(contract.icu_rules)}")
    return " ".join(clauses)


def _normalize_database_acl_sql(
    target: str,
    owner: str,
    restore: str,
    marker_comment: str,
) -> str:
    """Bind the new target before admitting the restore login."""
    target_identifier = _quote_identifier(target)
    owner_identifier = _quote_identifier(owner)
    restore_identifier = _quote_identifier(restore)
    marker_literal = _sql_literal(marker_comment)
    return " ".join(
        (
            "BEGIN;",
            f"SET LOCAL ROLE {owner_identifier};",
            f"REVOKE ALL ON DATABASE {target_identifier} FROM PUBLIC;",
            f"GRANT CONNECT, TEMPORARY ON DATABASE {target_identifier} "
            f"TO {restore_identifier};",
            f"COMMENT ON DATABASE {target_identifier} IS {marker_literal};",
            f"ALTER DATABASE {target_identifier} ALLOW_CONNECTIONS true;",
            "COMMIT",
        )
    )


def _psql_command(
    psql: str,
    conninfo: str,
    *commands: str,
) -> tuple[str, ...]:
    if not commands or any(not command for command in commands):
        raise ValueError("psql requires at least one non-empty command")
    argv = [
        psql,
        "--no-password",
        "--no-psqlrc",
        "--set=ON_ERROR_STOP=1",
        "--tuples-only",
        "--no-align",
        "--field-separator=|",
        "--dbname",
        conninfo,
        "--command",
        _SECURE_SESSION_SEARCH_PATH_SQL,
    ]
    for command in commands:
        argv.extend(("--command", command))
    return tuple(argv)


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


def _require_distinct_roles(creator: str, restore: str, owner: str) -> None:
    if len({creator, restore, owner}) != 3:
        raise PostgresContractError(
            "creator login, restore login, and target owner must be distinct roles"
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


def _sql_literal(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise PostgresContractError("SQL literal contains a forbidden control character")
    return "'" + value.replace("'", "''") + "'"


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _parse_psql_bool(value: str) -> bool:
    return value.strip().lower() in _TRUE_VALUES


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
