"""Migration runner for PPBase.

Provides the ``MigrationApp`` helper class (passed to migration up()/down()
functions) and runner functions that discover, apply, and revert migration
files stored on disk.

Migration files are plain Python modules with ``up(app)`` and ``down(app)``
async functions.  The runner dynamically imports them via ``importlib``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ppbase.core.id_generator import generate_id
from ppbase.core.storage_safety import validate_collection_id
from ppbase.db.schema_manager import (
    create_collection_table,
    delete_collection_table,
    update_collection_table,
)
from ppbase.db.system_tables import CollectionRecord, MigrationRecord

logger = logging.getLogger(__name__)


class MigrationLockError(RuntimeError):
    """Raised when the PostgreSQL migration lock cannot be acquired safely."""


class MigrationCommitOutcomeError(RuntimeError):
    """Raised when PostgreSQL commit success cannot be determined safely."""


_MIGRATION_LOCK_SEED = 0x505042415345  # "PPBASE"
_MIGRATION_LOCK_POLL_SECONDS = 0.1


def _validate_lock_timeout(timeout_seconds: float) -> None:
    """Validate a caller-provided migration lock timeout."""
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("Migration lock timeout must be a finite non-negative value.")


async def _invalidate_migration_lock_connection(
    connection: AsyncConnection,
) -> None:
    """Terminate a suspect lock session despite repeated task cancellation."""
    invalidation = asyncio.create_task(connection.invalidate())
    while True:
        try:
            await asyncio.shield(invalidation)
            break
        except asyncio.CancelledError:
            if invalidation.done():
                break
            continue
        except Exception:
            break

    try:
        invalidation.result()
    except Exception:
        logger.exception("Failed to invalidate migration-lock connection")


async def migration_lock_key(executor: Any) -> int:
    """Return the database-scoped advisory-lock key used by every actor.

    PostgreSQL advisory locks are cluster-wide, so the key hashes the current
    database name plus the stable ``ppbase:migrations`` namespace.  Backup
    coordination also uses this public helper when it already owns a dedicated
    connection; duplicating the derivation would risk two actors silently
    locking different keys.
    """
    result = await executor.execute(
        text(
            "SELECT pg_catalog.hashtextextended("
            "pg_catalog.concat("
            "pg_catalog.current_database(), ':ppbase:migrations'"
            "), :seed)"
        ),
        {"seed": _MIGRATION_LOCK_SEED},
    )
    return int(result.scalar_one())


async def acquire_migration_transaction_lock(
    session: AsyncSession,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Acquire the runner lock for the lifetime of ``session``'s transaction.

    Dashboard/API migration producers use a transaction-level advisory lock on
    their existing request connection.  Session- and transaction-level
    PostgreSQL advisory locks share the same conflict space, so this excludes
    the session-level runner lock without requiring a second pool connection.
    """
    _validate_lock_timeout(timeout_seconds)
    lock_key = await migration_lock_key(session)
    deadline = time.monotonic() + timeout_seconds

    while True:
        result = await session.execute(
            text("SELECT pg_catalog.pg_try_advisory_xact_lock(:key)"),
            {"key": lock_key},
        )
        if bool(result.scalar_one()):
            return
        if time.monotonic() >= deadline:
            raise MigrationLockError(
                "Timed out waiting for the PPBase migration lock "
                f"after {timeout_seconds:g} seconds."
            )
        await asyncio.sleep(_MIGRATION_LOCK_POLL_SECONDS)


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` without mutating either."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge_auth_options(
    defaults: dict[str, Any],
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge auth options while preserving omitted runtime credentials.

    Token settings are nested mappings and are covered by the regular deep
    merge. OAuth providers are a list, so sanitized snapshot entries must be
    matched by provider name to retain an existing ``clientId`` and
    ``clientSecret`` that were intentionally not written to disk.
    """
    merged = _deep_merge_dicts(defaults, existing)
    incoming_copy = dict(incoming)
    incoming_oauth = incoming_copy.get("oauth2")

    if isinstance(incoming_oauth, dict) and isinstance(
        incoming_oauth.get("providers"), list
    ):
        current_oauth = merged.get("oauth2")
        current_providers = (
            current_oauth.get("providers", [])
            if isinstance(current_oauth, dict)
            else []
        )
        current_by_name = {
            provider.get("name"): provider
            for provider in current_providers
            if isinstance(provider, dict) and provider.get("name")
        }
        providers: list[Any] = []
        for provider in incoming_oauth["providers"]:
            if not isinstance(provider, dict):
                providers.append(provider)
                continue
            provider_name = provider.get("name")
            current = current_by_name.get(provider_name, {})
            providers.append(_deep_merge_dicts(current, provider))

        oauth_without_providers = dict(incoming_oauth)
        oauth_without_providers.pop("providers", None)
        incoming_copy["oauth2"] = oauth_without_providers
        merged = _deep_merge_dicts(merged, incoming_copy)
        merged_oauth = merged.get("oauth2")
        if not isinstance(merged_oauth, dict):
            merged_oauth = {}
        merged_oauth = dict(merged_oauth)
        merged_oauth["providers"] = providers
        merged["oauth2"] = merged_oauth
        return merged

    return _deep_merge_dicts(merged, incoming_copy)


# ---------------------------------------------------------------------------
# MigrationApp — helper passed to each migration's up() / down()
# ---------------------------------------------------------------------------


_ROOT_TRANSACTION_COMMANDS = {
    "ABORT",
    "BEGIN",
    "COMMIT",
    "END",
    "ROLLBACK",
}


def _split_sql_statements(sql: str) -> list[str]:
    """Split SQL at top-level semicolons for transaction-control checks.

    This is deliberately a small lexical scanner rather than a SQL parser. It
    only needs to avoid treating semicolons inside PostgreSQL strings, quoted
    identifiers, comments, or dollar-quoted bodies as statement boundaries.
    PostgreSQL will remain responsible for validating the SQL itself.
    """
    statements: list[str] = []
    start = 0
    index = 0
    length = len(sql)

    while index < length:
        current = sql[index]
        following = sql[index + 1] if index + 1 < length else ""

        if current == "-" and following == "-":
            newline = sql.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue

        if current == "/" and following == "*":
            depth = 1
            index += 2
            while index < length and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue

        if current in {"'", '"'}:
            quote = current
            escape_string = (
                quote == "'"
                and index > 0
                and sql[index - 1] in {"E", "e"}
                and (
                    index < 2
                    or not (
                        sql[index - 2].isalnum()
                        or sql[index - 2] in {"_", "$"}
                    )
                )
            )
            index += 1
            while index < length:
                if escape_string and sql[index] == "\\":
                    index += 2
                    continue
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue

        if current == "$":
            delimiter_match = re.match(
                r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$",
                sql[index:],
            )
            if delimiter_match is not None:
                delimiter = delimiter_match.group(0)
                body_start = index + len(delimiter)
                body_end = sql.find(delimiter, body_start)
                index = length if body_end < 0 else body_end + len(delimiter)
                continue

        if current == ";":
            statements.append(sql[start:index])
            start = index + 1

        index += 1

    statements.append(sql[start:])
    return statements


def _leading_sql_words(statement: str, *, limit: int = 2) -> list[str]:
    """Return the first unquoted SQL words, ignoring whitespace/comments."""
    words: list[str] = []
    index = 0
    length = len(statement)

    while index < length and len(words) < limit:
        while index < length:
            if statement[index].isspace():
                index += 1
                continue
            if statement.startswith("--", index):
                newline = statement.find("\n", index + 2)
                index = length if newline < 0 else newline + 1
                continue
            if statement.startswith("/*", index):
                depth = 1
                index += 2
                while index < length and depth:
                    if statement.startswith("/*", index):
                        depth += 1
                        index += 2
                    elif statement.startswith("*/", index):
                        depth -= 1
                        index += 2
                    else:
                        index += 1
                continue
            break

        match = re.match(r"[A-Za-z_][A-Za-z0-9_$]*", statement[index:])
        if match is None:
            break
        words.append(match.group(0).upper())
        index += len(match.group(0))

    return words


def _transaction_control_command(statement: Any) -> str | None:
    """Return a forbidden root-transaction command, if present."""
    if isinstance(statement, str):
        sql = statement
    else:
        raw_text = getattr(statement, "text", None)
        if isinstance(raw_text, str):
            sql = raw_text
        else:
            try:
                sql = str(statement)
            except Exception:
                return None

    leading_words = _leading_sql_words(sql, limit=4)
    routine_prefixes = {
        ("CREATE", "FUNCTION"),
        ("CREATE", "PROCEDURE"),
        ("CREATE", "OR", "REPLACE", "FUNCTION"),
        ("CREATE", "OR", "REPLACE", "PROCEDURE"),
    }
    has_routine_prefix = any(
        tuple(leading_words[: len(prefix)]) == prefix
        for prefix in routine_prefixes
    )
    if (
        has_routine_prefix
        and re.search(r"\bBEGIN\s+ATOMIC\b", sql, flags=re.IGNORECASE)
    ):
        # PostgreSQL's SQL-standard routine body is intentionally not quoted
        # and may contain semicolons followed by END. It is still a single
        # CREATE statement, not an END/COMMIT of the migration transaction.
        return None

    for sql_statement in _split_sql_statements(sql):
        words = _leading_sql_words(sql_statement)
        if not words:
            continue
        if words[0] in _ROOT_TRANSACTION_COMMANDS:
            return words[0]
        if len(words) > 1 and tuple(words[:2]) in {
            ("PREPARE", "TRANSACTION"),
            ("START", "TRANSACTION"),
        }:
            return " ".join(words[:2])
    return None


def _reject_transaction_control(statement: Any) -> None:
    """Prevent raw SQL from taking ownership of the runner transaction."""
    command = _transaction_control_command(statement)
    if command is not None:
        raise RuntimeError(
            f"Migration SQL cannot execute transaction-control command {command!r}; "
            "the migration runner owns the root transaction."
        )


class _MigrationConnectionFacade:
    """Connection-like object that cannot own the migration root transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def execute(self, statement: Any, params: dict | None = None) -> Any:
        _reject_transaction_control(statement)
        return await self._session.execute(statement, params or {})

    async def scalar(self, statement: Any, params: dict | None = None) -> Any:
        _reject_transaction_control(statement)
        return await self._session.scalar(statement, params or {})

    @asynccontextmanager
    async def _nested(self) -> AsyncIterator[_MigrationConnectionFacade]:
        async with self._session.begin_nested():
            yield self

    def begin(self) -> Any:
        """Return a savepoint context, never an independent transaction."""
        return self._nested()

    def begin_nested(self) -> Any:
        return self._nested()

    async def commit(self) -> None:
        raise RuntimeError(
            "Migration connections cannot commit the runner-owned root transaction."
        )

    async def rollback(self) -> None:
        raise RuntimeError(
            "Migration connections cannot roll back the runner-owned root transaction."
        )

    async def close(self) -> None:
        raise RuntimeError(
            "Migration connections cannot close the runner-owned connection."
        )

    def in_transaction(self) -> bool:
        return bool(self._session.in_transaction())


class _MigrationEngineFacade:
    """Engine-compatible facade pinned to the current migration transaction."""

    def __init__(
        self,
        session: AsyncSession,
        engine: AsyncEngine,
        connection: _MigrationConnectionFacade,
    ) -> None:
        self._session = session
        self._engine = engine
        self._connection = connection

    @property
    def url(self) -> Any:
        return self._engine.url

    @property
    def dialect(self) -> Any:
        return self._engine.dialect

    @asynccontextmanager
    async def _begin(self) -> AsyncIterator[_MigrationConnectionFacade]:
        async with self._session.begin_nested():
            yield self._connection

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[_MigrationConnectionFacade]:
        # The context deliberately does not close or commit the shared
        # connection. Explicit transaction ownership belongs to the runner.
        yield self._connection

    def begin(self) -> Any:
        return self._begin()

    def connect(self) -> Any:
        return self._connect()

    async def dispose(self) -> None:
        raise RuntimeError("Migration code cannot dispose the application engine.")


class _MigrationSessionFacade:
    """Useful AsyncSession subset with root transaction ownership removed."""

    def __init__(
        self,
        session: AsyncSession,
        connection: _MigrationConnectionFacade,
    ) -> None:
        self._session = session
        self._connection = connection

    async def execute(self, statement: Any, params: dict | None = None) -> Any:
        _reject_transaction_control(statement)
        return await self._session.execute(statement, params or {})

    async def scalar(self, statement: Any, params: dict | None = None) -> Any:
        _reject_transaction_control(statement)
        return await self._session.scalar(statement, params or {})

    async def scalars(self, statement: Any, params: dict | None = None) -> Any:
        _reject_transaction_control(statement)
        return await self._session.scalars(statement, params or {})

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return await self._session.get(*args, **kwargs)

    def add(self, instance: Any) -> None:
        self._session.add(instance)

    def add_all(self, instances: Any) -> None:
        self._session.add_all(instances)

    async def delete(self, instance: Any) -> None:
        await self._session.delete(instance)

    async def flush(self, *args: Any, **kwargs: Any) -> None:
        await self._session.flush(*args, **kwargs)

    async def connection(self) -> _MigrationConnectionFacade:
        return self._connection

    def begin(self) -> Any:
        return self._connection.begin()

    def begin_nested(self) -> Any:
        return self._connection.begin_nested()

    def in_transaction(self) -> bool:
        return bool(self._session.in_transaction())

    async def commit(self) -> None:
        raise RuntimeError(
            "Migration sessions cannot commit the runner-owned root transaction."
        )

    async def rollback(self) -> None:
        raise RuntimeError(
            "Migration sessions cannot roll back the runner-owned root transaction."
        )

    async def close(self) -> None:
        raise RuntimeError("Migration sessions cannot close the runner-owned session.")


class MigrationApp:
    """Context object passed to migration ``up()`` and ``down()`` functions.

    Wraps a database session and transaction-bound engine facade so migration
    code can create, update, and delete collections (both ORM records and
    physical tables) without opening an independently committed connection.

    All operations share the same session / transaction.
    """

    def __init__(self, session: AsyncSession, engine: AsyncEngine) -> None:
        self._session = session
        bound_connection = _MigrationConnectionFacade(session)
        self.session = _MigrationSessionFacade(session, bound_connection)
        self.engine = _MigrationEngineFacade(session, engine, bound_connection)

    # -- collection helpers -------------------------------------------------

    async def create_collection(self, definition: dict) -> CollectionRecord:
        """Create a new collection record and its physical table.

        ``definition`` should contain at least ``name`` and optionally
        ``type``, ``schema``, ``options``, rule fields, etc.
        """
        now = datetime.now(timezone.utc)
        collection_type = definition.get("type", "base")
        options = definition.get("options", {})
        if not isinstance(options, dict):
            options = {}
        if collection_type == "auth":
            # Snapshots intentionally omit secrets.  Generate fresh runtime
            # secrets and layer the public migration options over them.
            from ppbase.services.auth_service import generate_default_auth_options

            defaults = generate_default_auth_options(
                is_superusers=definition.get("name") == "_superusers"
            )
            options = _merge_auth_options(defaults, {}, options)

        raw_collection_id = definition.get("id")
        collection_id = (
            generate_id()
            if raw_collection_id is None or raw_collection_id == ""
            else validate_collection_id(raw_collection_id)
        )
        record = CollectionRecord(
            id=collection_id,
            name=definition["name"],
            type=collection_type,
            system=definition.get("system", False),
            schema=definition.get("schema", []),
            indexes=definition.get("indexes", []),
            list_rule=definition.get("listRule"),
            view_rule=definition.get("viewRule"),
            create_rule=definition.get("createRule"),
            update_rule=definition.get("updateRule"),
            delete_rule=definition.get("deleteRule"),
            options=options,
            created=now,
            updated=now,
        )
        self._session.add(record)
        await self._session.flush()
        await create_collection_table(self._session, record)
        return record

    async def update_collection(
        self, id_or_name: str, changes: dict
    ) -> CollectionRecord:
        """Update an existing collection and alter the physical table."""
        record = await self.find_collection(id_or_name)

        # Snapshot old state for schema diff
        class _Snapshot:
            def __init__(self, rec: CollectionRecord) -> None:
                self.name = rec.name
                self.type = rec.type
                self.schema = list(rec.schema) if isinstance(rec.schema, list) else []
                self.indexes = (
                    list(rec.indexes) if isinstance(rec.indexes, list) else []
                )
                self.options = dict(rec.options) if isinstance(rec.options, dict) else {}

        old_snapshot = _Snapshot(record)

        # Apply changes
        if "name" in changes:
            record.name = changes["name"]
        if "type" in changes:
            record.type = changes["type"]
        if "system" in changes:
            record.system = changes["system"]
        if "schema" in changes:
            import copy
            record.schema = copy.deepcopy(changes["schema"])
            flag_modified(record, "schema")
        if "indexes" in changes:
            import copy
            record.indexes = copy.deepcopy(changes["indexes"])
            flag_modified(record, "indexes")
        if "listRule" in changes:
            record.list_rule = changes["listRule"]
        if "viewRule" in changes:
            record.view_rule = changes["viewRule"]
        if "createRule" in changes:
            record.create_rule = changes["createRule"]
        if "updateRule" in changes:
            record.update_rule = changes["updateRule"]
        if "deleteRule" in changes:
            record.delete_rule = changes["deleteRule"]
        if "options" in changes:
            import copy

            incoming_options = changes["options"]
            if not isinstance(incoming_options, dict):
                incoming_options = {}
            if record.type == "auth":
                # Updating an auth collection from a sanitized snapshot must
                # retain its existing runtime secrets.
                from ppbase.services.auth_service import generate_default_auth_options

                defaults = generate_default_auth_options(
                    is_superusers=record.name == "_superusers"
                )
                existing = record.options if isinstance(record.options, dict) else {}
                record.options = _merge_auth_options(
                    defaults,
                    existing,
                    incoming_options,
                )
            else:
                record.options = copy.deepcopy(incoming_options)
            flag_modified(record, "options")
        elif record.type == "auth" and old_snapshot.type != "auth":
            from ppbase.services.auth_service import generate_default_auth_options

            existing = record.options if isinstance(record.options, dict) else {}
            record.options = _merge_auth_options(
                generate_default_auth_options(
                    is_superusers=record.name == "_superusers"
                ),
                existing,
                {},
            )
            flag_modified(record, "options")

        record.updated = datetime.now(timezone.utc)
        await self._session.flush()

        # Alter the physical relation when its defining attributes changed.
        # This includes view queries and custom indexes, not only columns.
        physical_schema_changed = (
            old_snapshot.name != record.name
            or old_snapshot.type != record.type
            or old_snapshot.schema
            != (record.schema if isinstance(record.schema, list) else [])
            or old_snapshot.indexes
            != (record.indexes if isinstance(record.indexes, list) else [])
            or (
                (old_snapshot.type == "view" or record.type == "view")
                and old_snapshot.options
                != (record.options if isinstance(record.options, dict) else {})
            )
        )
        if physical_schema_changed:
            await update_collection_table(self._session, old_snapshot, record)

        return record

    async def delete_collection(self, id_or_name: str) -> None:
        """Delete a collection record and drop the physical table."""
        record = await self.find_collection(id_or_name)
        table_name = record.name
        col_type = record.type or "base"

        await self._session.delete(record)
        await self._session.flush()
        await delete_collection_table(self._session, table_name, col_type)

    async def find_collection(self, id_or_name: str) -> CollectionRecord:
        """Find a collection by ID or name (case-insensitive)."""
        stmt = select(CollectionRecord).where(
            (CollectionRecord.id == id_or_name)
            | (func.lower(CollectionRecord.name) == id_or_name.lower())
        )
        result = await self._session.execute(stmt)
        record = result.scalars().first()
        if record is None:
            raise LookupError(f"Collection '{id_or_name}' not found.")
        return record

    async def save_collection(self, definition: dict) -> CollectionRecord:
        """Create or update a collection (upsert).

        If a collection with the given ``id`` or ``name`` already exists it is
        updated; otherwise a new one is created.
        """
        id_or_name = definition.get("id") or definition.get("name")
        if not id_or_name:
            raise ValueError("Collection definition must include 'id' or 'name'.")

        try:
            record = await self.find_collection(id_or_name)
        except LookupError:
            return await self.create_collection(definition)

        # Build changes dict from definition (exclude 'id')
        changes = {k: v for k, v in definition.items() if k != "id"}
        record = await self.update_collection(record.id, changes)

        # Legacy PPBase versions could leave view metadata behind after a
        # cascading parent replacement. Extend-mode snapshots repair such an
        # already-missing physical view instead of trusting metadata alone.
        if record.type == "view":
            result = await self._session.execute(
                text(
                    "SELECT EXISTS("
                    "SELECT 1 FROM pg_catalog.pg_class AS relation "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'public' "
                    "AND relation.relname = :name "
                    "AND relation.relkind IN ('v', 'm'))"
                ),
                {"name": record.name},
            )
            if not bool(result.scalar_one()):
                await create_collection_table(self._session, record)

        return record

    async def execute_sql(self, sql: str, params: dict | None = None) -> Any:
        """Execute raw SQL within the migration transaction."""
        _reject_transaction_control(sql)
        result = await self._session.execute(text(sql), params or {})
        return result


# ---------------------------------------------------------------------------
# Migration file discovery helpers
# ---------------------------------------------------------------------------

_MIGRATION_FILE_PATTERN = ".py"


def _list_migration_files(migrations_dir: str | Path) -> list[str]:
    """Return sorted list of migration filenames from the directory.

    Migration files are ``*.py`` files whose names start with a digit
    (timestamp prefix). They are sorted by the numeric prefix and then by name,
    so chronological order remains correct even if prefix width changes.
    """
    dirpath = Path(migrations_dir)
    if not dirpath.is_dir():
        return []

    candidates = [
        path
        for path in dirpath.iterdir()
        if path.is_file()
        and path.suffix == _MIGRATION_FILE_PATTERN
        and path.name[0].isdigit()
        and path.name != "__init__.py"
    ]

    def _migration_sort_key(path: Path) -> tuple[int, str]:
        match = re.match(r"^(\d+)", path.name)
        timestamp = int(match.group(1)) if match else 0
        return timestamp, path.name

    return [path.name for path in sorted(candidates, key=_migration_sort_key)]


def _load_migration_module(migration_file: str, migrations_dir: str | Path) -> Any:
    """Dynamically import a migration Python file and return the module."""
    filepath = Path(migrations_dir) / migration_file
    if not filepath.exists():
        raise FileNotFoundError(f"Migration file not found: {filepath}")

    module_name = f"ppbase_migration_{filepath.stem}"
    spec = importlib.util.spec_from_file_location(module_name, str(filepath))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load migration module: {filepath}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# Runner functions
# ---------------------------------------------------------------------------


async def get_applied_migrations(session: AsyncSession) -> list[MigrationRecord]:
    """Return all applied migration records ordered by filename."""
    stmt = select(MigrationRecord).order_by(MigrationRecord.file)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_pending_migrations(
    session: AsyncSession, migrations_dir: str | Path
) -> list[str]:
    """Return filenames of migrations not yet applied, sorted by timestamp."""
    all_files = _list_migration_files(migrations_dir)
    if not all_files:
        return []

    applied = await get_applied_migrations(session)
    applied_names = {m.file for m in applied}

    return [f for f in all_files if f not in applied_names]


async def apply_migration(
    session: AsyncSession,
    engine: AsyncEngine,
    migration_file: str,
    migrations_dir: str | Path,
) -> None:
    """Apply a single migration file.

    Loads the module, calls ``up(app)``, and records the migration in the
    ``_migrations`` table.  The caller owns the transaction boundary.
    """
    module = _load_migration_module(migration_file, migrations_dir)

    up_fn = getattr(module, "up", None)
    if up_fn is None:
        raise AttributeError(
            f"Migration {migration_file} does not define an 'up' function."
        )

    logger.info("Applying migration: %s", migration_file)

    app = MigrationApp(session, engine)
    await up_fn(app)

    # The history row is flushed in the very same transaction as metadata
    # and PostgreSQL DDL.  PostgreSQL transactional DDL then guarantees that
    # either all three persist or none of them do.
    record = MigrationRecord(file=migration_file)
    session.add(record)
    await session.flush()

    logger.info("Applied migration: %s", migration_file)


async def revert_migration(
    session: AsyncSession,
    engine: AsyncEngine,
    migration_file: str,
    migrations_dir: str | Path,
) -> None:
    """Revert a single migration file.

    Loads the module, calls ``down(app)``, and removes the migration record
    from the ``_migrations`` table.  The caller owns the transaction boundary.
    """
    module = _load_migration_module(migration_file, migrations_dir)

    down_fn = getattr(module, "down", None)
    if down_fn is None:
        raise AttributeError(
            f"Migration {migration_file} does not define a 'down' function."
        )

    logger.info("Reverting migration: %s", migration_file)

    app = MigrationApp(session, engine)
    await down_fn(app)

    # Remove the history row in the same transaction as the down migration.
    stmt = delete(MigrationRecord).where(MigrationRecord.file == migration_file)
    await session.execute(stmt)
    await session.flush()

    logger.info("Reverted migration: %s", migration_file)


async def apply_all_pending(
    session: AsyncSession,
    engine: AsyncEngine,
    migrations_dir: str | Path,
) -> list[str]:
    """Apply pending migrations with a lock and one commit per file.

    ``session`` is retained in the signature for backwards compatibility.
    It must be idle because migration work deliberately uses a dedicated
    connection so that each file can own an independent root transaction.
    """
    if session.in_transaction():
        raise RuntimeError(
            "apply_all_pending() requires an idle session. Commit or roll back "
            "the caller transaction first, or call run_migrations_up(engine, dir)."
        )
    return await run_migrations_up(engine, migrations_dir)


async def commit_migration_transaction(
    session: AsyncSession,
    connection: AsyncConnection,
    migration_file: str,
    *,
    expected_applied: bool,
) -> None:
    """Commit one migration and reconcile a lost PostgreSQL acknowledgement.

    The history row is in the same PostgreSQL transaction as migration DDL, so
    its presence is an exact commit witness for ``up`` and its absence is an
    exact witness for ``down``. If the connection itself was invalidated, the
    outcome can still be reported precisely but the advisory-lock lease is no
    longer trusted and the batch stops with an explicit recovery error.
    """
    try:
        await session.commit()
        return
    except BaseException as commit_error:
        lock_connection_was_invalidated = connection.invalidated
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "Rollback failed after COMMIT error for migration %s",
                migration_file,
            )

        try:
            result = await session.execute(
                select(MigrationRecord.file).where(
                    MigrationRecord.file == migration_file
                )
            )
            is_applied = result.scalar_one_or_none() is not None
            await session.rollback()
        except BaseException as verify_error:
            raise MigrationCommitOutcomeError(
                "PostgreSQL commit outcome is unknown for migration "
                f"{migration_file!r}. Inspect migration status before retrying; "
                "for a down operation do not blindly repeat a relative "
                "`down 1`."
            ) from verify_error

        if is_applied != expected_applied:
            raise commit_error

        direction = "applied" if expected_applied else "reverted"
        if lock_connection_was_invalidated or connection.invalidated:
            raise MigrationCommitOutcomeError(
                f"Migration {migration_file!r} was confirmed {direction}, but "
                "the database connection and its advisory-lock lease were lost. "
                "The batch stopped; inspect status before continuing. Do not "
                "repeat a relative down command for this already reverted file."
            ) from commit_error

        logger.warning(
            "Migration COMMIT acknowledgement was lost, but history confirms "
            "%s was %s",
            migration_file,
            direction,
        )


@asynccontextmanager
async def migration_lock_on_connection(
    connection: AsyncConnection,
    *,
    timeout_seconds: float = 30.0,
) -> AsyncIterator[AsyncConnection]:
    """Hold the migration lock on an already reserved PostgreSQL session.

    The caller owns and must eventually close ``connection``.  It must be a
    dedicated, transaction-free connection with backend-session affinity.
    This variant lets coordinators acquire another advisory lock first and
    then the migration lock without checking out a second pool connection.
    """
    _validate_lock_timeout(timeout_seconds)
    if connection.closed or connection.invalidated:
        raise MigrationLockError(
            "The dedicated PostgreSQL migration-lock connection is not open."
        )
    if connection.in_transaction():
        raise MigrationLockError(
            "The dedicated PostgreSQL migration-lock connection must not have "
            "an active transaction."
        )

    started_at = time.monotonic()
    acquired = False
    body_error: BaseException | None = None
    unlock_error: BaseException | None = None

    try:
        lock_key = await migration_lock_key(connection)
        # SELECT starts an implicit transaction.  Session-level locks survive
        # commits, and callers receive a clean connection after acquisition.
        await connection.commit()

        deadline = started_at + timeout_seconds
        while True:
            result = await connection.execute(
                text("SELECT pg_catalog.pg_try_advisory_lock(:key)"),
                {"key": lock_key},
            )
            acquired = bool(result.scalar_one())
            await connection.commit()
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise MigrationLockError(
                    "Timed out waiting for the PPBase migration lock "
                    f"after {timeout_seconds:g} seconds."
                )
            await asyncio.sleep(_MIGRATION_LOCK_POLL_SECONDS)

        try:
            yield connection
        except BaseException as exc:
            body_error = exc
            raise
    finally:
        if acquired and not connection.closed:
            try:
                if connection.in_transaction():
                    await connection.rollback()
                result = await connection.execute(
                    text("SELECT pg_catalog.pg_advisory_unlock(:key)"),
                    {"key": lock_key},
                )
                unlocked = bool(result.scalar_one())
                await connection.commit()
                if not unlocked:
                    raise MigrationLockError(
                        "PostgreSQL reported that the PPBase migration lock "
                        "was no longer owned by its reserved connection."
                    )
            except BaseException as exc:
                unlock_error = exc
                logger.exception("Failed to release the PPBase migration lock")
                await _invalidate_migration_lock_connection(connection)
        elif not connection.closed and not connection.invalidated:
            # pg_try_advisory_lock may have succeeded server-side even when its
            # result never reached this task. Returning that session to a pool
            # would retain the session-level migration lock indefinitely.
            await _invalidate_migration_lock_connection(connection)

        if body_error is None and unlock_error is not None:
            if isinstance(unlock_error, MigrationLockError):
                raise unlock_error
            raise MigrationLockError(
                "The PostgreSQL session holding the PPBase migration lock was "
                "lost while releasing it."
            ) from unlock_error


@asynccontextmanager
async def migration_lock(
    engine: AsyncEngine,
    *,
    timeout_seconds: float = 30.0,
) -> AsyncIterator[AsyncConnection]:
    """Reserve a PostgreSQL connection and hold the PPBase advisory lock.

    The lock is session-level, which lets us commit individual migration
    transactions while still excluding other PPBase instances.  The key is
    derived from the current database name so databases in the same cluster
    do not block one another. It requires backend-session affinity: PgBouncer
    transaction and statement pooling are intentionally unsupported.
    """
    _validate_lock_timeout(timeout_seconds)

    started_at = time.monotonic()
    try:
        connection = await asyncio.wait_for(
            engine.connect(),
            timeout=max(timeout_seconds, 0.001),
        )
    except TimeoutError as exc:
        raise MigrationLockError(
            "Timed out waiting for a database connection for the PPBase "
            f"migration lock after {timeout_seconds:g} seconds."
        ) from exc
    body_error: BaseException | None = None

    try:
        try:
            remaining = max(
                0.0,
                timeout_seconds - (time.monotonic() - started_at),
            )
            async with migration_lock_on_connection(
                connection,
                timeout_seconds=remaining,
            ) as locked_connection:
                yield locked_connection
        except BaseException as exc:
            body_error = exc
            raise
    finally:
        close_error: BaseException | None = None
        try:
            await connection.close()
        except BaseException as exc:
            close_error = exc
            if body_error is not None:
                logger.exception(
                    "Failed to close migration connection while handling "
                    "another migration error"
                )

        if body_error is None and close_error is not None:
            raise close_error


async def apply_pending_on_connection(
    connection: AsyncConnection,
    migrations_dir: str | Path,
) -> list[str]:
    """Apply pending files on an already locked dedicated connection."""
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        async with session.begin():
            pending = await get_pending_migrations(session, migrations_dir)

    applied: list[str] = []
    for migration_file in pending:
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            await session.begin()
            try:
                await apply_migration(
                    session,
                    connection.engine,
                    migration_file,
                    migrations_dir,
                )
            except BaseException:
                await session.rollback()
                raise
            await commit_migration_transaction(
                session,
                connection,
                migration_file,
                expected_applied=True,
            )
        applied.append(migration_file)

    return applied


async def run_migrations_up(
    engine: AsyncEngine,
    migrations_dir: str | Path,
    *,
    lock_timeout_seconds: float = 30.0,
) -> list[str]:
    """Apply pending migrations under the PostgreSQL migration lock."""
    async with migration_lock(
        engine,
        timeout_seconds=lock_timeout_seconds,
    ) as connection:
        return await apply_pending_on_connection(connection, migrations_dir)


async def revert_on_connection(
    connection: AsyncConnection,
    migrations_dir: str | Path,
    *,
    count: int = 1,
) -> list[str]:
    """Revert the latest migrations on an already locked connection."""
    if count < 1:
        return []

    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        async with session.begin():
            result = await session.execute(
                select(MigrationRecord)
                .order_by(MigrationRecord.applied.desc(), MigrationRecord.file.desc())
                .limit(count)
            )
            migration_files = [record.file for record in result.scalars().all()]

    reverted: list[str] = []
    for migration_file in migration_files:
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            await session.begin()
            try:
                await revert_migration(
                    session,
                    connection.engine,
                    migration_file,
                    migrations_dir,
                )
            except BaseException:
                await session.rollback()
                raise
            await commit_migration_transaction(
                session,
                connection,
                migration_file,
                expected_applied=False,
            )
        reverted.append(migration_file)

    return reverted


async def run_migrations_down(
    engine: AsyncEngine,
    migrations_dir: str | Path,
    *,
    count: int = 1,
    lock_timeout_seconds: float = 30.0,
) -> list[str]:
    """Revert migrations with a lock and one transaction per file."""
    async with migration_lock(
        engine,
        timeout_seconds=lock_timeout_seconds,
    ) as connection:
        return await revert_on_connection(
            connection,
            migrations_dir,
            count=count,
        )


async def get_migration_status(
    executor: AsyncSession | AsyncConnection,
    migrations_dir: str | Path,
) -> dict[str, Any]:
    """Return a summary dict with migration status information.

    Keys:
        initialized: whether the ``_migrations`` table already exists
        applied: list of applied migration filenames
        pending: list of pending migration filenames
        orphaned: applied history rows whose files are no longer present
        total: total number of migration files
        tracked_total: union of files and history rows
        applied_count: number of applied migrations
        pending_count: number of pending migrations
        orphaned_count: number of history rows without files
    """
    all_files = _list_migration_files(migrations_dir)
    initialized_result = await executor.execute(
        text("SELECT to_regclass('public._migrations') IS NOT NULL")
    )
    initialized = bool(initialized_result.scalar_one())

    if initialized:
        applied_result = await executor.execute(
            select(MigrationRecord.file).order_by(MigrationRecord.file)
        )
        applied_names = list(applied_result.scalars().all())
    else:
        applied_names = []

    applied_set = set(applied_names)
    file_set = set(all_files)
    pending = [filename for filename in all_files if filename not in applied_set]
    orphaned = [filename for filename in applied_names if filename not in file_set]

    return {
        "initialized": initialized,
        "applied": applied_names,
        "pending": pending,
        "orphaned": orphaned,
        "total": len(all_files),
        "tracked_total": len(file_set | applied_set),
        "applied_count": len(applied_names),
        "pending_count": len(pending),
        "orphaned_count": len(orphaned),
    }
