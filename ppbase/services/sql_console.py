"""Raw SQL console for superusers.

Scripts are split into single statements and classified before anything is
sent to PostgreSQL.  Statements that change data or schema are only executed
when the caller explicitly confirms them.  Unconfirmed scripts run inside a
``READ ONLY`` transaction, so PostgreSQL itself rejects writes hidden behind
a statically harmless statement (e.g. ``SELECT my_function()`` that deletes
rows); such a rejection is reported as a confirmation request as well.

Every script runs in a single transaction on a dedicated PostgreSQL session.
Confirmed scripts are committed only when all statements succeed.  The
session is invalidated afterwards so session-level state the script may have
created (``SET``, temp tables, advisory locks, ``LISTEN``...) never leaks into
the shared connection pool.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ppbase.services.write_barrier import mutation_write_barrier_on_connection

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 500
STATEMENT_TIMEOUT_MS = 30_000

# SQLSTATE raised when a write is attempted in a READ ONLY transaction.
_READ_ONLY_SQLSTATE = "25006"

_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_WORD_RE = re.compile(r"[^\W\d][\w$]*")
_NUMBER_RE = re.compile(r"[0-9][0-9A-Za-z_.]*")

_READ_COMMANDS = {"SELECT", "VALUES", "TABLE", "SHOW"}
_MODIFY_COMMANDS = {"INSERT", "UPDATE", "MERGE", "UPSERT"}
_DELETE_COMMANDS = {"DELETE", "TRUNCATE", "DROP"}
_SCHEMA_COMMANDS = {
    "CREATE", "ALTER", "COMMENT", "GRANT", "REVOKE", "SECURITY", "IMPORT",
    "REASSIGN",
}
_MAINTENANCE_COMMANDS = {
    "VACUUM", "ANALYZE", "ANALYSE", "CLUSTER", "REINDEX", "REFRESH",
    "CHECKPOINT",
}
_TRANSACTION_COMMANDS = {
    "BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT",
    "RELEASE",
}
_CREATE_MODIFIERS = {
    "OR", "REPLACE", "UNIQUE", "TEMP", "TEMPORARY", "UNLOGGED", "GLOBAL",
    "LOCAL", "TRUSTED", "PROCEDURAL", "DEFAULT", "CONSTRAINT", "RECURSIVE",
}
_TWO_WORD_OBJECTS = {"MATERIALIZED", "FOREIGN", "EVENT", "TEXT", "USER", "ACCESS", "OPERATOR"}

MUTATING_KINDS = {"modify", "delete", "schema", "maintenance"}


class SqlConsoleError(Exception):
    """User-facing SQL console failure (rendered as HTTP 400)."""

    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data or {}


@dataclass
class SqlStatement:
    """One statement of a script with its static classification."""

    index: int
    sql: str
    words: list[str] = field(repr=False)
    command: str = ""
    kind: str = "other"
    warning: str | None = None
    forbidden_reason: str | None = None

    @property
    def mutating(self) -> bool:
        return self.kind in MUTATING_KINDS

    def describe(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "sql": self.sql,
            "command": self.command,
            "kind": self.kind,
            "mutating": self.mutating,
            "warning": self.warning,
        }


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def _skip_quoted(sql: str, start: int, quote: str, backslash_escapes: bool) -> int:
    """Return the index just past the quoted literal starting at *start*."""
    i = start + 1
    n = len(sql)
    while i < n:
        ch = sql[i]
        if backslash_escapes and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def split_statements(sql: str) -> list[tuple[str, list[str]]]:
    """Split a script on top-level ``;`` into ``(statement, words)`` pairs.

    String literals, quoted identifiers, dollar-quoted bodies and comments are
    skipped, so semicolons inside them never split a statement.  ``words`` are
    the upper-cased bare keywords/identifiers of the statement (plus ``(`` and
    ``)`` tokens) used for classification.  Statements that only contain
    whitespace or comments are dropped.
    """
    statements: list[tuple[str, list[str]]] = []
    words: list[str] = []
    has_content = False
    start = 0
    i = 0
    n = len(sql)

    def flush(end: int) -> None:
        nonlocal words, has_content
        text = sql[start:end].strip()
        if has_content and text:
            statements.append((text, words))
        words = []
        has_content = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if ch == "-" and nxt == "-":
            newline = sql.find("\n", i)
            i = n if newline == -1 else newline + 1
            continue
        if ch == "/" and nxt == "*":
            depth = 1
            i += 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        if ch == "'":
            prev = sql[i - 1] if i > 0 else ""
            before_prev = sql[i - 2] if i > 1 else ""
            escape_string = prev in "eE" and not (before_prev.isalnum() or before_prev == "_")
            i = _skip_quoted(sql, i, "'", escape_string)
            has_content = True
            continue
        if ch == '"':
            i = _skip_quoted(sql, i, '"', False)
            has_content = True
            continue
        if ch == "$":
            match = _DOLLAR_TAG_RE.match(sql, i)
            if match:
                end = sql.find(match.group(0), match.end())
                i = n if end == -1 else end + len(match.group(0))
                has_content = True
                continue
        if ch == ";":
            flush(i)
            i += 1
            start = i
            continue
        if ch.isdigit():
            i = _NUMBER_RE.match(sql, i).end()  # type: ignore[union-attr]
            has_content = True
            continue
        if ch.isalpha() or ch == "_":
            match = _WORD_RE.match(sql, i)
            if match:
                words.append(match.group(0).upper())
                i = match.end()
                has_content = True
                continue
        if ch in "()":
            words.append(ch)
        if not ch.isspace():
            has_content = True
        i += 1

    flush(n)
    return statements


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _top_level_words(words: list[str]) -> list[str]:
    """Words outside of any parentheses."""
    depth = 0
    result: list[str] = []
    for word in words:
        if word == "(":
            depth += 1
        elif word == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            result.append(word)
    return result


def _object_command(verb: str, rest: list[str]) -> str:
    """Build labels like ``CREATE TABLE`` or ``DROP MATERIALIZED VIEW``."""
    j = 0
    while j < len(rest) and rest[j] in _CREATE_MODIFIERS:
        j += 1
    if j >= len(rest) or rest[j] in "()":
        return verb
    label = f"{verb} {rest[j]}"
    if rest[j] in _TWO_WORD_OBJECTS and j + 1 < len(rest) and rest[j + 1] not in "()":
        label += f" {rest[j + 1]}"
    return label


def _classify(stmt: SqlStatement, words: list[str]) -> None:
    """Fill ``command``/``kind``/``warning``/``forbidden_reason`` from *words*."""
    j = 0
    while j < len(words) and words[j] == "(":
        j += 1
    words = words[j:]
    if not words:
        stmt.command, stmt.kind = "UNKNOWN", "other"
        return

    first = words[0]
    rest = words[1:]
    top = _top_level_words(words)

    if first in _TRANSACTION_COMMANDS or (first == "PREPARE" and rest[:1] == ["TRANSACTION"]):
        stmt.command, stmt.kind = first, "forbidden"
        stmt.forbidden_reason = (
            f"{first} is not allowed: the console already runs the whole script "
            "in a single transaction (committed on success, rolled back on error)."
        )
        return
    if first == "SET" and (
        rest[:1] == ["TRANSACTION"]
        or rest[:2] == ["SESSION", "CHARACTERISTICS"]
        or any(w in ("TRANSACTION_READ_ONLY", "DEFAULT_TRANSACTION_READ_ONLY") for w in rest)
    ):
        stmt.command, stmt.kind = "SET TRANSACTION", "forbidden"
        stmt.forbidden_reason = "Changing the transaction mode is not allowed in the SQL console."
        return
    if first == "COPY":
        stmt.command, stmt.kind = "COPY", "forbidden"
        stmt.forbidden_reason = (
            "COPY is not supported in the SQL console. Use INSERT ... SELECT or "
            "SELECT instead."
        )
        return

    if first == "EXPLAIN":
        analyze = False
        k = 0
        if rest[:1] == ["("]:
            while k < len(rest) and rest[k] != ")":
                analyze = analyze or rest[k] in ("ANALYZE", "ANALYSE")
                k += 1
            k += 1
        else:
            while k < len(rest) and rest[k] in ("ANALYZE", "ANALYSE", "VERBOSE"):
                analyze = analyze or rest[k] in ("ANALYZE", "ANALYSE")
                k += 1
        if analyze:
            # EXPLAIN ANALYZE really executes the explained statement.
            inner = SqlStatement(index=stmt.index, sql=stmt.sql, words=rest[k:])
            _classify(inner, rest[k:])
            stmt.command = f"EXPLAIN ANALYZE {inner.command}".strip()
            stmt.kind = "read" if inner.kind == "other" else inner.kind
            stmt.warning = inner.warning
            stmt.forbidden_reason = inner.forbidden_reason
            if inner.kind == "forbidden":
                stmt.kind = "forbidden"
        else:
            stmt.command, stmt.kind = "EXPLAIN", "read"
        return

    if first == "WITH":
        # Data-modifying CTEs: WITH d AS (DELETE ... RETURNING *) SELECT ...
        if "DELETE" in words:
            stmt.command, stmt.kind = "WITH ... DELETE", "delete"
        elif any(w in words for w in ("INSERT", "UPDATE", "MERGE")):
            stmt.command, stmt.kind = "WITH ... MODIFY", "modify"
        else:
            stmt.command, stmt.kind = "SELECT", "read"
        return

    if first in _READ_COMMANDS:
        stmt.command, stmt.kind = first, "read"
        if first == "SELECT" and "INTO" in top:
            stmt.command, stmt.kind = "SELECT INTO", "schema"
            stmt.warning = "SELECT INTO creates a new table."
        return

    if first in _MODIFY_COMMANDS:
        stmt.command, stmt.kind = first, "modify"
        if first == "UPDATE" and "WHERE" not in top:
            stmt.warning = "UPDATE without a WHERE clause modifies every row of the table."
        return

    if first in _DELETE_COMMANDS:
        if first == "DROP":
            stmt.command = _object_command("DROP", rest)
            stmt.warning = f"{stmt.command} permanently removes the object and all its data."
            if "CASCADE" in top:
                stmt.warning += " CASCADE also drops every dependent object."
        else:
            stmt.command = first
            if first == "TRUNCATE":
                stmt.warning = "TRUNCATE removes every row of the table(s)."
            elif "WHERE" not in top:
                stmt.warning = "DELETE without a WHERE clause removes every row of the table."
        stmt.kind = "delete"
        return

    if first in _SCHEMA_COMMANDS:
        stmt.command = _object_command(first, rest) if first in ("CREATE", "ALTER") else first
        stmt.kind = "schema"
        if first == "ALTER" and "DROP" in top:
            stmt.warning = f"This {stmt.command} drops a column, constraint or other part of the object."
        return

    if first in _MAINTENANCE_COMMANDS:
        stmt.command, stmt.kind = first, "maintenance"
        return

    # SET, RESET, DO, CALL, LOCK, NOTIFY, EXECUTE, DECLARE, FETCH...
    # They are executed read-only first; PostgreSQL rejects any write.
    stmt.command, stmt.kind = first, "other"


def analyze_script(sql: str) -> list[SqlStatement]:
    """Split and classify every statement of *sql*."""
    result: list[SqlStatement] = []
    for index, (text, words) in enumerate(split_statements(sql)):
        stmt = SqlStatement(index=index, sql=text, words=words)
        _classify(stmt, words)
        result.append(stmt)
    return result


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Convert asyncpg values into JSON-friendly values for display."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "items") and callable(value.items):  # asyncpg.Record
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _decode_cell(value: Any, type_name: str) -> Any:
    if type_name in ("json", "jsonb") and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return _jsonable(value)


def _pg_error(exc: BaseException) -> BaseException:
    """Return the underlying asyncpg exception of a (wrapped) driver error."""
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        exc = exc.orig
    cause = exc.__cause__
    if cause is not None and getattr(cause, "sqlstate", None):
        return cause
    return exc


def _error_data(exc: BaseException, stmt: SqlStatement) -> dict[str, Any]:
    pg = _pg_error(exc)
    data: dict[str, Any] = {
        "statementIndex": stmt.index,
        "sql": stmt.sql,
        "sqlstate": getattr(pg, "sqlstate", None),
    }
    for attr in ("detail", "hint", "position"):
        value = getattr(pg, attr, None)
        if value:
            data[attr] = value
    return data


def _error_message(exc: BaseException) -> str:
    pg = _pg_error(exc)
    message = getattr(pg, "message", None) or str(pg)
    return str(message).strip() or exc.__class__.__name__


async def _run_statement(apg: Any, stmt: SqlStatement, max_rows: int) -> dict[str, Any]:
    started = time.perf_counter()
    prepared = await apg.prepare(stmt.sql)
    attributes = prepared.get_attributes()
    result = stmt.describe()

    if attributes:
        columns = [{"name": a.name, "type": a.type.name} for a in attributes]
        rows: list[list[Any]] = []
        truncated = False
        async for record in prepared.cursor():
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append([_decode_cell(record[i], c["type"]) for i, c in enumerate(columns)])
        status = f"{stmt.command} {len(rows)}{'+' if truncated else ''}"
        result.update(columns=columns, rows=rows, rowCount=len(rows), truncated=truncated)
    else:
        await prepared.fetch()
        status = prepared.get_statusmsg() or stmt.command
        result.update(columns=[], rows=[], rowCount=_status_row_count(status), truncated=False)

    result["status"] = status
    result["durationMs"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _status_row_count(status: str) -> int | None:
    """Extract the affected-row count from tags like ``UPDATE 3``."""
    parts = status.split()
    if parts and parts[-1].isdigit() and parts[0] in ("INSERT", "UPDATE", "DELETE", "MERGE", "SELECT", "MOVE", "FETCH"):
        return int(parts[-1])
    return None


async def _execute_on_connection(
    conn: AsyncConnection,
    statements: list[SqlStatement],
    *,
    read_only: bool,
    max_rows: int,
) -> list[dict[str, Any]] | SqlStatement:
    """Run *statements* in one asyncpg transaction.

    Returns the per-statement results, or the statement PostgreSQL refused
    because it writes while running in read-only mode.
    """
    raw = await conn.get_raw_connection()
    apg = raw.driver_connection
    transaction = apg.transaction(readonly=read_only)
    await transaction.start()
    results: list[dict[str, Any]] = []
    refused: SqlStatement | None = None
    try:
        await apg.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        if read_only:
            # Taking a snapshot now makes PostgreSQL reject any later attempt
            # to switch this transaction to read-write (e.g. via set_config).
            await apg.execute("SELECT 1")
        for stmt in statements:
            try:
                results.append(await _run_statement(apg, stmt, max_rows))
            except Exception as exc:
                if read_only and getattr(_pg_error(exc), "sqlstate", None) == _READ_ONLY_SQLSTATE:
                    if not stmt.mutating:
                        stmt.kind = "modify"
                        stmt.warning = (
                            "PostgreSQL detected that this statement writes to the "
                            f"database: {_error_message(exc)}"
                        )
                    refused = stmt
                    break
                raise SqlConsoleError(_error_message(exc), _error_data(exc, stmt)) from exc
    except BaseException:
        await transaction.rollback()
        raise

    if read_only or refused is not None:
        await transaction.rollback()
    else:
        await transaction.commit()
    return refused if refused is not None else results


async def execute_script(
    engine: AsyncEngine,
    sql: str,
    *,
    confirm: bool,
    max_rows: int = DEFAULT_MAX_ROWS,
    actor: str = "",
) -> dict[str, Any]:
    """Analyze and (when allowed) execute a SQL script.

    Without *confirm*, scripts containing mutating statements are not
    executed: the response has ``requiresConfirmation: true`` and lists the
    statements so the client can ask the user.  With *confirm*, the script runs
    read-write under the shared write barrier and is committed atomically.
    """
    statements = analyze_script(sql)
    if not statements:
        raise SqlConsoleError("The SQL query is empty.")

    for stmt in statements:
        if stmt.kind == "forbidden":
            raise SqlConsoleError(
                stmt.forbidden_reason or f"{stmt.command} is not allowed.",
                {"statementIndex": stmt.index, "sql": stmt.sql},
            )

    def confirmation_response() -> dict[str, Any]:
        return {
            "requiresConfirmation": True,
            "executed": False,
            "committed": False,
            "statements": [s.describe() for s in statements],
        }

    if not confirm and any(s.mutating for s in statements):
        return confirmation_response()

    started = time.perf_counter()
    async with engine.connect() as conn:
        try:
            if confirm:
                async with mutation_write_barrier_on_connection(conn):
                    # Acquiring the barrier opened a SQLAlchemy transaction;
                    # the advisory lock is session-level and survives commit.
                    await conn.commit()
                    outcome = await _execute_on_connection(
                        conn, statements, read_only=False, max_rows=max_rows
                    )
            else:
                outcome = await _execute_on_connection(
                    conn, statements, read_only=True, max_rows=max_rows
                )
        finally:
            await conn.invalidate()

    if isinstance(outcome, SqlStatement):
        return confirmation_response()

    if confirm:
        logger.warning(
            "SQL console script committed by %s (%d statement(s)): %s",
            actor or "unknown admin",
            len(statements),
            "; ".join(f"{s.command}" for s in statements),
        )

    return {
        "requiresConfirmation": False,
        "executed": True,
        "committed": confirm,
        "statements": outcome,
        "durationMs": round((time.perf_counter() - started) * 1000, 2),
    }
