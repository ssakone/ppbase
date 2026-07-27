"""Declarative ``schema.json`` model and the strict PPBase schema contract.

``schema.json`` is the materialised-schema half of the native backup (the
logical equivalent of PocketBase's ``data.db`` structure).  It is a strictly
validated, closed-grammar description of every ``public`` object PPBase itself
produces: system tables, collection base/auth tables, collection views, and
managed/custom indexes, plus the applied-migration registry and the
``data.copy`` segment index.

The contract is designed to **fail closed**: anything PPBase cannot reproduce
with certainty aborts the backup instead of being silently dropped or replayed
as opaque SQL.  Concretely:

* Column types come from a **closed allowlist** of the physical PostgreSQL
  types PPBase emits, and every column captures its **exact default
  expression** so the column is byte-for-byte reconstructable.
* Index keys are represented **structurally** (plain columns with optional
  ASC/DESC and NULLS ordering).  Expression keys, non-default opclasses,
  collations, ``nulls_not_distinct`` and exclusion indexes are **rejected**,
  never silently normalised.  ``INCLUDE`` columns and partial predicates are
  captured faithfully.
* View definitions and index predicates are validated by a conservative,
  single-statement SQL guard at **both** backup capture and ``schema.json``
  load — not deferred to restore.
* Tables and views are **reconciled against a caller-supplied inventory**
  (system tables + collections known to PPBase).  An object that is not in the
  inventory aborts the backup; there is no ``unknown -> base/system`` fallback.

:func:`introspect_public_schema` builds this model from a live database and
:func:`assert_no_unmanaged_objects` blocks triggers, functions, sequences, RLS
policies, foreign keys, CHECK/exclusion constraints, enum/domain/composite
types, partitions, materialised views, foreign tables and non-baseline
extensions.  A silent, lossy backup is never produced.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Any

from ppbase.backup.copy_format import CopySegment, validate_segment_layout
from ppbase.backup.models import (
    BackupManifestError,
    canonical_json_bytes,
    parse_canonical_json,
)


SCHEMA_CONTRACT_VERSION = 1

# Version of the PPBase internal system schema (the fixed ORM tables such as
# ``_collections``/``_superusers``/``_migrations``).  The native backup is the
# first official baseline, so this starts at 1.  Restore accepts only an exact
# match: a mismatch is refused before any destructive action.  A deliberate
# migration policy can be introduced later if a new system schema ships and real
# archives of the previous version exist.
SYSTEM_SCHEMA_VERSION = 1

# Extensions that may exist without making the backup lossy.  ``plpgsql`` ships
# in every stock database and is not required by any PPBase-managed object.
_BASELINE_EXTENSIONS = frozenset({"plpgsql"})

_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_IDENTIFIER_MAX_BYTES = 63

# Closed allowlist of physical column types, as PostgreSQL's ``format_type``
# renders them.  ``character varying(N)`` and array forms are matched by
# pattern below; the rest are exact.
_SCALAR_TYPES = frozenset(
    {
        "text",
        "integer",
        "bigint",
        "smallint",
        "double precision",
        "real",
        "boolean",
        "timestamp with time zone",
        "jsonb",
        "json",
    }
)
_VARCHAR_PATTERN = re.compile(r"^character varying\((\d+)\)$")
_ARRAY_PATTERN = re.compile(r"^(text|character varying)(?:\((\d+)\))?\[\]$")

# ``gist`` is intentionally excluded: every builtin gist opclass covers a type
# (box/circle/inet/point/polygon/tsquery/tsvector/range) that is *not* a
# PPBase-supported column type, so a reachable gist index would require an
# extension (btree_gist / pg_trgm) which the unmanaged-object guard already
# refuses.  ``spgist`` stays because ``text_ops`` is builtin and covers the
# PPBase ``text`` type.
_INDEX_METHODS = frozenset({"btree", "hash", "gin", "brin", "spgist"})
_TABLE_KINDS = frozenset({"system", "base", "auth"})

# The exact COPY session contract that produced ``data.copy``.  Recorded in
# schema.json and validated on load so a tampered/foreign settings block is
# rejected before any byte is imported.
_COPY_SETTINGS = {
    "format": "text",
    "encoding": "UTF8",
    "date_style": "ISO",
    "time_zone": "UTC",
    "interval_style": "postgres",
}

# ---------------------------------------------------------------------------
# Read-only SQL allowlist (view bodies and index partial predicates)
# ---------------------------------------------------------------------------
#
# The two places PPBase cannot fully structure are collection view SELECT
# bodies (``options.query``) and custom-index partial predicates, both of which
# are *arbitrary user SQL*.  A regex blacklist over the raw text is unsound:
# quoted identifiers such as ``"pg_sleep"(10)`` or ``"nextval"('s'::regclass)``
# slip past a "forbidden word" scan because the quotes hide the callable's name.
#
# Instead we tokenise the SQL with a real PostgreSQL-aware lexer that classifies
# every token (bare word, quoted identifier, string/dollar/bit literal, number,
# operator, punctuation) and then enforce a strict, closed **allowlist**:
#
#   * exactly one read-only statement (no interior ``;``, no comments);
#   * every *callable* (a word or quoted identifier immediately followed by
#     ``(``) must be on ``_ALLOWED_FUNCTIONS`` — anything else (``pg_sleep``,
#     ``nextval``, ``pg_read_file`` … regardless of quoting or qualification)
#     fails closed;
#   * no DML / utility keyword, no ``pg_*``/``information_schema`` qualifier, no
#     ``reg*`` catalog cast, no bind parameter.
#
# An expression that cannot be proven to be a read-only projection over
# allowlisted operators/functions aborts the backup rather than being replayed.
_SQL_MAX_LEN = 100_000

# Structural keywords that may legally precede ``(`` without being a user
# function call (``in (...)``, ``over (...)``, ``values (...)`` …).  ``cast`` and
# the other special-form functions are handled via ``_ALLOWED_FUNCTIONS``.
_SQL_STRUCTURAL_KEYWORDS = frozenset(
    {
        "select", "from", "where", "and", "or", "not", "in", "exists", "any",
        "all", "some", "over", "partition", "values", "array", "case", "when",
        "then", "else", "end", "on", "using", "group", "order", "by", "having",
        "distinct", "union", "intersect", "except", "as", "between", "like",
        "ilike", "similar", "is", "null", "true", "false", "asc", "desc",
        "nulls", "first", "last", "limit", "offset", "fetch", "row", "rows",
        "with", "recursive", "lateral", "join", "inner", "outer", "left",
        "right", "full", "cross", "natural", "filter", "within", "grouping",
        "collate", "at", "time", "zone", "interval", "cast", "coalesce",
        "nullif", "greatest", "least", "extract", "substring", "trim",
        "position", "overlay",
    }
)

# DML / utility / transaction keywords that must never appear as a bare keyword.
_SQL_FORBIDDEN_KEYWORDS = frozenset(
    {
        "insert", "update", "delete", "drop", "alter", "create", "grant",
        "revoke", "truncate", "copy", "call", "do", "merge", "vacuum",
        "analyze", "reindex", "cluster", "comment", "attach", "detach",
        "execute", "prepare", "deallocate", "set", "reset", "savepoint",
        "listen", "notify", "unlisten", "load", "refresh", "import", "into",
        "returning", "begin", "commit", "rollback", "start", "lock",
        "declare", "open", "close", "move", "discard", "checkpoint",
        "security", "definer", "function", "procedure", "trigger", "rule",
        "policy",
    }
)

# ``reg*`` object-identifier casts force catalog resolution and are a known
# object-reference vector (``'s'::regclass`` -> nextval target); never legal in
# a reproducible PPBase view/predicate.
_SQL_FORBIDDEN_TYPES = frozenset(
    {
        "regclass", "regproc", "regprocedure", "regoper", "regoperator",
        "regtype", "regrole", "regnamespace", "regconfig", "regdictionary",
        "regcollation",
    }
)

# Only these functions may be *called* from a view body or index predicate.
# Every entry is immutable/stable and side-effect free.  Volatile I/O, sequence,
# session, large-object, dblink and backend-control functions are absent, so
# they fail closed.
_ALLOWED_FUNCTIONS = frozenset(
    {
        # conditional / null handling
        "coalesce", "nullif", "greatest", "least",
        # string
        "lower", "upper", "initcap", "length", "char_length",
        "character_length", "octet_length", "bit_length", "trim", "btrim",
        "ltrim", "rtrim", "lpad", "rpad", "substr", "substring", "left",
        "right", "reverse", "replace", "translate", "concat", "concat_ws",
        "format", "split_part", "strpos", "position", "starts_with",
        "to_char", "to_number", "md5", "ascii", "chr", "repeat",
        "regexp_replace", "regexp_match", "regexp_matches",
        "regexp_split_to_array",
        # math
        "abs", "ceil", "ceiling", "floor", "round", "trunc", "sign", "mod",
        "power", "sqrt", "cbrt", "exp", "ln", "log", "log10", "div",
        "width_bucket", "gcd", "lcm",
        # date / time
        "date_part", "date_trunc", "extract", "age", "to_timestamp",
        "to_date", "make_date", "make_time", "make_timestamp",
        "make_timestamptz", "justify_days", "justify_hours",
        "justify_interval", "now",
        # type helpers
        "cast",
        # json / jsonb
        "jsonb_array_length", "json_array_length", "jsonb_extract_path",
        "jsonb_extract_path_text", "json_extract_path",
        "json_extract_path_text", "jsonb_typeof", "json_typeof",
        "jsonb_object_keys", "jsonb_build_object", "jsonb_build_array",
        "to_jsonb", "to_json", "jsonb_strip_nulls", "jsonb_pretty",
        # array
        "array_length", "array_upper", "array_lower", "array_ndims",
        "array_position", "array_positions", "array_to_string",
        "string_to_array", "cardinality", "array_append", "array_prepend",
        "array_cat", "array_remove", "array_replace", "unnest",
        # aggregates
        "count", "sum", "avg", "min", "max", "array_agg", "string_agg",
        "jsonb_agg", "json_agg", "bool_and", "bool_or", "every", "variance",
        "var_samp", "var_pop", "stddev", "stddev_samp", "stddev_pop",
        # window
        "row_number", "rank", "dense_rank", "percent_rank", "cume_dist",
        "ntile", "lag", "lead", "first_value", "last_value", "nth_value",
    }
)

# Closed structural allowlist of the *exact* column default expressions PPBase
# emits (as ``pg_get_expr`` renders them, verified empirically).  A default that
# is not on this list — e.g. ``nextval('rogue_seq'::regclass)`` — aborts the
# backup.  PPBase never creates identity, generated or sequence columns.
_ALLOWED_DEFAULTS = frozenset(
    {
        "''::text",
        "''::character varying",
        "0",
        "'0'::double precision",
        "false",
        "now()",
        "'{}'::text[]",
        "'{}'::character varying[]",
        "'null'::jsonb",
        "'[]'::jsonb",
        "'{}'::jsonb",
        '\'{"lat": 0, "lon": 0}\'::jsonb',
        "'base'::character varying",
    }
)


class SchemaContractError(BackupManifestError):
    """Raised when schema.json is malformed or violates the closed grammar."""


class UnmanagedObjectError(BackupManifestError):
    """Raised when the live schema contains an object PPBase cannot reproduce.

    Backups fail closed on this error: an object outside the contract would be
    silently lost on restore, so the backup is refused instead.
    """


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise SchemaContractError(f"{label} is not a valid unquoted identifier")
    if len(value.encode("utf-8")) > _IDENTIFIER_MAX_BYTES:
        raise SchemaContractError(f"{label} exceeds {_IDENTIFIER_MAX_BYTES} bytes")
    return value


def normalize_column_type(value: Any) -> str:
    """Return ``value`` if it is an allowed physical column type, else raise."""
    if not isinstance(value, str):
        raise SchemaContractError("column type must be a string")
    if value in _SCALAR_TYPES:
        return value
    if _VARCHAR_PATTERN.fullmatch(value):
        return value
    if _ARRAY_PATTERN.fullmatch(value):
        return value
    raise SchemaContractError(f"column type is outside the allowlist: {value!r}")


def _bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaContractError(f"{label} must be a boolean")
    return value


def _tokenize_sql(value: str, *, label: str) -> list[tuple[str, str]]:
    """Lex ``value`` into ``(kind, text)`` tokens with a PostgreSQL-aware lexer.

    ``kind`` is one of ``word`` (bare identifier/keyword, lower-cased),
    ``qident`` (double-quoted identifier, unquoted), ``literal`` (any string /
    dollar-quoted / bit / hex constant), ``number``, or ``punct`` (a single
    operator/punctuation character).  Comments, bind parameters and unterminated
    constants raise :class:`SchemaContractError` — none of them are allowed in a
    reproducible read-only fragment, and refusing them keeps the lexer total.
    """
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch in " \t\r\n\f\v":
            i += 1
            continue
        if ch == "-" and i + 1 < n and value[i + 1] == "-":
            raise SchemaContractError(f"{label} must not contain SQL comments")
        if ch == "/" and i + 1 < n and value[i + 1] == "*":
            raise SchemaContractError(f"{label} must not contain SQL comments")
        if ch == "$":
            # Dollar-quoted string ($tag$...$tag$) or a bind parameter ($1).
            tag_match = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", value[i:])
            if tag_match:
                tag = tag_match.group(0)
                end = value.find(tag, i + len(tag))
                if end == -1:
                    raise SchemaContractError(
                        f"{label} has an unterminated dollar-quoted string"
                    )
                tokens.append(("literal", value[i : end + len(tag)]))
                i = end + len(tag)
                continue
            raise SchemaContractError(
                f"{label} must not contain bind parameters"
            )
        if ch == '"':
            j = i + 1
            while j < n:
                if value[j] == '"':
                    if j + 1 < n and value[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            else:
                raise SchemaContractError(
                    f"{label} has an unterminated quoted identifier"
                )
            tokens.append(("qident", value[i + 1 : j].replace('""', '"')))
            i = j + 1
            continue
        # String constants, incl. E'' (backslash) and b''/x'' variants.
        prefix = ""
        if ch in "eEbBxX" and i + 1 < n and value[i + 1] == "'":
            prefix = ch.lower()
            i += 1
            ch = "'"
        if ch == "'":
            escaped = prefix == "e"
            j = i + 1
            while j < n:
                if value[j] == "\\" and escaped:
                    j += 2
                    continue
                if value[j] == "'":
                    if j + 1 < n and value[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            else:
                raise SchemaContractError(
                    f"{label} has an unterminated string literal"
                )
            tokens.append(("literal", value[i : j + 1]))
            i = j + 1
            continue
        if ch.isdigit() or (
            ch == "." and i + 1 < n and value[i + 1].isdigit()
        ):
            j = i
            while j < n and (value[j].isalnum() or value[j] in "._+-" ):
                # Stop a trailing sign that is not part of an exponent.
                if value[j] in "+-" and value[j - 1] not in "eE":
                    break
                j += 1
            tokens.append(("number", value[i:j]))
            i = j
            continue
        if ch.isalpha() or ch == "_" or ord(ch) >= 0x80:
            j = i
            while j < n and (value[j].isalnum() or value[j] in "_$" or ord(value[j]) >= 0x80):
                j += 1
            tokens.append(("word", value[i:j].lower()))
            i = j
            continue
        if ch == "\x00":
            raise SchemaContractError(f"{label} contains a NUL byte")
        tokens.append(("punct", ch))
        i += 1
    return tokens


def _next_significant(tokens: list[tuple[str, str]], start: int) -> tuple[str, str] | None:
    for idx in range(start, len(tokens)):
        return tokens[idx]
    return None


def _validate_readonly_sql(value: Any, *, label: str, require_select: bool) -> str:
    """Validate one read-only SQL fragment against the closed allowlist.

    Enforces: a single statement, no comments/parameters, only allowlisted
    callables, no DML/utility keyword, no ``pg_*``/``information_schema``
    qualifier and no ``reg*`` catalog cast.  When ``require_select`` the fragment
    must be a bare ``SELECT``/``WITH``/parenthesised query (view body); otherwise
    it is a boolean predicate expression.
    """
    if not isinstance(value, str) or not value.strip():
        raise SchemaContractError(f"{label} must be a non-empty string")
    if len(value) > _SQL_MAX_LEN:
        raise SchemaContractError(f"{label} exceeds {_SQL_MAX_LEN} characters")
    text = value.rstrip()
    if text.endswith(";"):  # a single trailing terminator is tolerated
        text = text[:-1].rstrip()
    tokens = _tokenize_sql(text, label=label)
    if not tokens:
        raise SchemaContractError(f"{label} must be a non-empty statement")

    for idx, (kind, tok) in enumerate(tokens):
        if kind == "punct" and tok == ";":
            raise SchemaContractError(f"{label} must be a single statement")
        if kind == "word":
            if tok in _SQL_FORBIDDEN_KEYWORDS:
                raise SchemaContractError(
                    f"{label} contains forbidden keyword {tok!r}"
                )
            if tok in _SQL_FORBIDDEN_TYPES:
                raise SchemaContractError(
                    f"{label} uses a forbidden catalog cast {tok!r}"
                )
        # Narrow, exact-match guard for the two *reserved* schema qualifiers
        # that can never name a PPBase collection.  This is not a prefix rule
        # (``pg_posts`` / ``pg_status`` collections are fine) — it only rejects
        # ``pg_catalog.x`` / ``information_schema.x`` qualification.  Bare
        # catalog relations and every other cross-schema reference are caught
        # by the OID-based pg_depend gate during introspection.
        if kind in ("word", "qident"):
            following = _next_significant(tokens, idx + 1)
            if following == ("punct", ".") and tok.lower() in (
                "pg_catalog",
                "information_schema",
            ):
                raise SchemaContractError(
                    f"{label} qualifies a reserved schema {tok!r}"
                )
        # Callable detection: a word/quoted identifier immediately followed by
        # '(' is a function call and must be allowlisted.  Security is otherwise
        # based on namespace + OID + inventory (verified from pg_depend during
        # introspection), never on an identifier's name prefix — so a
        # legitimately named collection such as ``pg_posts`` is accepted while a
        # real ``pg_class`` reference is caught by the dependency gate.
        if kind in ("word", "qident"):
            following = _next_significant(tokens, idx + 1)
            if following == ("punct", "("):
                # A schema-qualified callable (``evil.lower(...)``,
                # ``pg_catalog.pg_read_file(...)``) resolves outside the pinned
                # search_path and could reach an attacker's function even when
                # the bare name is allowlisted; only unqualified builtin calls
                # are permitted.
                if idx > 0 and tokens[idx - 1] == ("punct", "."):
                    raise SchemaContractError(
                        f"{label} calls a schema-qualified function {tok!r}"
                    )
                name = tok.lower()
                if kind == "word" and name in _SQL_STRUCTURAL_KEYWORDS:
                    continue
                if name not in _ALLOWED_FUNCTIONS:
                    raise SchemaContractError(
                        f"{label} calls non-allowlisted function {tok!r}"
                    )

    if require_select:
        first_kind, first_tok = tokens[0]
        if not (
            (first_kind == "word" and first_tok in {"select", "with"})
            or (first_kind == "punct" and first_tok == "(")
        ):
            raise SchemaContractError("view definition must be a single SELECT")
    return text


def _validate_index_predicate(value: Any) -> str:
    return _validate_readonly_sql(
        value, label="index predicate", require_select=False
    )


def _validate_view_definition(value: Any) -> str:
    return _validate_readonly_sql(
        value, label="view definition", require_select=True
    )


def _classify_default(value: Any) -> str | None:
    """Return a validated column default or ``None`` for no default.

    The default must be one of the exact expressions PPBase itself emits (a
    closed structural allowlist).  An unknown or crafted default — including any
    function call such as ``nextval(...)`` — aborts the backup.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaContractError("column default must be a string or null")
    if value not in _ALLOWED_DEFAULTS:
        raise SchemaContractError(
            f"column default is not a reproducible PPBase default: {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Structural specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """A physical column: allowed type, nullability and exact default expr."""

    name: str
    type: str
    nullable: bool
    default: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, label="column name"))
        object.__setattr__(self, "type", normalize_column_type(self.type))
        object.__setattr__(self, "nullable", _bool(self.nullable, label="nullable"))
        object.__setattr__(self, "default", _classify_default(self.default))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "default": self.default,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ColumnSpec":
        if not isinstance(value, Mapping) or set(value) != {
            "name",
            "type",
            "nullable",
            "default",
        }:
            raise SchemaContractError("column has unknown or missing fields")
        return cls(
            name=value["name"],
            type=value["type"],
            nullable=value["nullable"],
            default=value["default"],
        )


def _column_tuple(values: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise SchemaContractError(f"{label} must be an array")
    result = tuple(_identifier(v, label=f"{label} entry") for v in values)
    if len(result) != len(set(result)):
        raise SchemaContractError(f"{label} has duplicate columns")
    return result


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    kind: str  # "system" | "base" | "auth"
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...]
    unique_constraints: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, label="table name"))
        if self.kind not in _TABLE_KINDS:
            raise SchemaContractError(f"unknown table kind: {self.kind!r}")
        columns = tuple(self.columns)
        if not columns:
            raise SchemaContractError(f"table {self.name!r} has no columns")
        names = [c.name for c in columns]
        if len(names) != len(set(names)):
            raise SchemaContractError(f"table {self.name!r} has duplicate columns")
        object.__setattr__(self, "columns", columns)
        known = set(names)
        pk = _column_tuple(self.primary_key, label="primary_key")
        for col in pk:
            if col not in known:
                raise SchemaContractError(f"primary key references unknown column {col!r}")
        object.__setattr__(self, "primary_key", pk)
        uniques: list[tuple[str, ...]] = []
        for entry in self.unique_constraints:
            cols = _column_tuple(entry, label="unique constraint")
            for col in cols:
                if col not in known:
                    raise SchemaContractError(
                        f"unique constraint references unknown column {col!r}"
                    )
            uniques.append(cols)
        object.__setattr__(self, "unique_constraints", tuple(uniques))

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "columns": [c.to_dict() for c in self.columns],
            "primary_key": list(self.primary_key),
            "unique_constraints": [list(u) for u in self.unique_constraints],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "TableSpec":
        expected = {"name", "kind", "columns", "primary_key", "unique_constraints"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise SchemaContractError("table has unknown or missing fields")
        columns = value["columns"]
        if not isinstance(columns, list):
            raise SchemaContractError("table columns must be an array")
        return cls(
            name=value["name"],
            kind=value["kind"],
            columns=tuple(ColumnSpec.from_dict(c) for c in columns),
            primary_key=value["primary_key"],
            unique_constraints=value["unique_constraints"],
        )


@dataclass(frozen=True, slots=True)
class IndexKeySpec:
    """One key of an index: a plain column with optional ordering.

    Expression keys, opclasses and collations are *not* representable here — the
    introspector rejects them rather than lose them silently.
    """

    column: str
    descending: bool
    nulls_first: bool | None  # None = PostgreSQL default for the direction

    def __post_init__(self) -> None:
        object.__setattr__(self, "column", _identifier(self.column, label="index column"))
        object.__setattr__(self, "descending", _bool(self.descending, label="descending"))
        if self.nulls_first is not None and not isinstance(self.nulls_first, bool):
            raise SchemaContractError("index key nulls_first must be a boolean or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "descending": self.descending,
            "nulls_first": self.nulls_first,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "IndexKeySpec":
        if not isinstance(value, Mapping) or set(value) != {
            "column",
            "descending",
            "nulls_first",
        }:
            raise SchemaContractError("index key has unknown or missing fields")
        return cls(
            column=value["column"],
            descending=value["descending"],
            nulls_first=value["nulls_first"],
        )


@dataclass(frozen=True, slots=True)
class IndexSpec:
    name: str
    table: str
    method: str
    unique: bool
    nulls_not_distinct: bool
    keys: tuple[IndexKeySpec, ...]
    included: tuple[str, ...]
    predicate: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, label="index name"))
        object.__setattr__(self, "table", _identifier(self.table, label="index table"))
        if self.method not in _INDEX_METHODS:
            raise SchemaContractError(f"unsupported index method: {self.method!r}")
        object.__setattr__(self, "unique", _bool(self.unique, label="unique"))
        object.__setattr__(
            self,
            "nulls_not_distinct",
            _bool(self.nulls_not_distinct, label="nulls_not_distinct"),
        )
        keys = tuple(self.keys)
        if not keys:
            raise SchemaContractError(f"index {self.name!r} has no keys")
        object.__setattr__(self, "keys", keys)
        object.__setattr__(
            self, "included", _column_tuple(self.included, label="index include")
            if self.included
            else ()
        )
        if self.predicate is not None:
            object.__setattr__(
                self, "predicate", _validate_index_predicate(self.predicate)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table": self.table,
            "method": self.method,
            "unique": self.unique,
            "nulls_not_distinct": self.nulls_not_distinct,
            "keys": [k.to_dict() for k in self.keys],
            "included": list(self.included),
            "predicate": self.predicate,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "IndexSpec":
        expected = {
            "name",
            "table",
            "method",
            "unique",
            "nulls_not_distinct",
            "keys",
            "included",
            "predicate",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise SchemaContractError("index has unknown or missing fields")
        keys = value["keys"]
        if not isinstance(keys, list):
            raise SchemaContractError("index keys must be an array")
        return cls(
            name=value["name"],
            table=value["table"],
            method=value["method"],
            unique=value["unique"],
            nulls_not_distinct=value["nulls_not_distinct"],
            keys=tuple(IndexKeySpec.from_dict(k) for k in keys),
            included=tuple(value["included"]),
            predicate=value["predicate"],
        )


@dataclass(frozen=True, slots=True)
class ViewSpec:
    """A collection view: its validated single-SELECT definition and deps.

    ``definition`` is validated as one read-only statement at both capture and
    load; it is never concatenated blindly.  The restore path re-validates it
    against the live catalog before use.
    """

    name: str
    definition: str
    depends_on: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, label="view name"))
        object.__setattr__(self, "definition", _validate_view_definition(self.definition))
        object.__setattr__(
            self, "depends_on", _column_tuple(self.depends_on, label="view depends_on")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "definition": self.definition,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ViewSpec":
        if not isinstance(value, Mapping) or set(value) != {
            "name",
            "definition",
            "depends_on",
        }:
            raise SchemaContractError("view has unknown or missing fields")
        return cls(
            name=value["name"],
            definition=value["definition"],
            depends_on=value["depends_on"],
        )


@dataclass(frozen=True, slots=True)
class MigrationSpec:
    """One applied migration: filename plus the SHA-256 of its content."""

    file: str
    sha256: str | None
    applied: str

    def __post_init__(self) -> None:
        if not isinstance(self.file, str) or not self.file:
            raise SchemaContractError("migration file must be a non-empty string")
        if self.sha256 is not None and (
            not isinstance(self.sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
        ):
            raise SchemaContractError("migration sha256 must be 64 hex chars or null")
        if not isinstance(self.applied, str) or not self.applied:
            raise SchemaContractError("migration applied timestamp must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "sha256": self.sha256, "applied": self.applied}

    @classmethod
    def from_dict(cls, value: Any) -> "MigrationSpec":
        if not isinstance(value, Mapping) or set(value) != {"file", "sha256", "applied"}:
            raise SchemaContractError("migration has unknown or missing fields")
        return cls(
            file=value["file"], sha256=value["sha256"], applied=value["applied"]
        )


@dataclass(frozen=True, slots=True)
class DatabaseSchema:
    """The full declarative snapshot serialised as ``schema.json``."""

    tables: tuple[TableSpec, ...]
    views: tuple[ViewSpec, ...]
    indexes: tuple[IndexSpec, ...]
    migrations: tuple[MigrationSpec, ...]
    segments: tuple[CopySegment, ...]
    data_copy_size: int
    source_postgres_version: int | None = None
    contract_version: int = SCHEMA_CONTRACT_VERSION
    system_schema_version: int = SYSTEM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != SCHEMA_CONTRACT_VERSION:
            raise SchemaContractError(
                f"unsupported schema contract version: {self.contract_version!r}"
            )
        if self.system_schema_version != SYSTEM_SCHEMA_VERSION:
            raise SchemaContractError(
                "unsupported system schema version: "
                f"{self.system_schema_version!r}"
            )
        table_names = [t.name for t in self.tables]
        if len(table_names) != len(set(table_names)):
            raise SchemaContractError("duplicate table names in schema")
        known_tables = set(table_names)
        table_by_name = {t.name: t for t in self.tables}
        view_names = [v.name for v in self.views]
        if len(view_names) != len(set(view_names)):
            raise SchemaContractError("duplicate view names in schema")
        relation_names = known_tables | set(view_names)
        for view in self.views:
            for dep in view.depends_on:
                if dep not in relation_names:
                    raise SchemaContractError(
                        f"view {view.name!r} depends on unknown relation {dep!r}"
                    )
        index_names = [i.name for i in self.indexes]
        if len(index_names) != len(set(index_names)):
            raise SchemaContractError("duplicate index names in schema")
        for index in self.indexes:
            if index.table not in known_tables:
                raise SchemaContractError(
                    f"index {index.name!r} targets unknown table {index.table!r}"
                )
            table = table_by_name[index.table]
            for key in index.keys:
                if key.column not in set(table.column_names):
                    raise SchemaContractError(
                        f"index {index.name!r} references unknown column {key.column!r}"
                    )
            for column in index.included:
                if column not in set(table.column_names):
                    raise SchemaContractError(
                        f"index {index.name!r} includes unknown column {column!r}"
                    )
        migration_files = [m.file for m in self.migrations]
        if len(migration_files) != len(set(migration_files)):
            raise SchemaContractError("duplicate migration files in schema")
        if isinstance(self.data_copy_size, bool) or not isinstance(
            self.data_copy_size, int
        ) or self.data_copy_size < 0:
            raise SchemaContractError("data_copy_size must be a non-negative integer")
        # Prove the segment index tiles data.copy exactly, and every present
        # segment maps to a real table with the *exact* column order recorded in
        # that table's TableSpec (no reordering or column drift on restore).
        validate_segment_layout(self.segments, total_size=self.data_copy_size)
        seen_segment_tables: set[str] = set()
        for segment in self.segments:
            if segment.table not in known_tables:
                raise SchemaContractError(
                    f"segment targets unknown table {segment.table!r}"
                )
            if segment.table in seen_segment_tables:
                raise SchemaContractError(
                    f"multiple segments target table {segment.table!r}"
                )
            seen_segment_tables.add(segment.table)
            expected = table_by_name[segment.table].column_names
            if tuple(segment.columns) != expected:
                raise SchemaContractError(
                    f"segment for {segment.table!r} does not match the table's "
                    "column order"
                )
        if self.source_postgres_version is not None and (
            isinstance(self.source_postgres_version, bool)
            or not isinstance(self.source_postgres_version, int)
        ):
            raise SchemaContractError("source_postgres_version must be an integer or null")

    def assert_every_table_exported(self) -> None:
        """Require exactly one COPY segment per table (including empty tables).

        ``__post_init__`` already guarantees no table has *more than one*
        segment, that each segment targets a known table, that segment column
        order matches the table, and that the segments tile ``data.copy``
        exactly with no gaps or extra bytes.  This adds the final completeness
        rule: every table (including empty ones) must be covered, so no table is
        silently skipped on restore.

        A schema-only snapshot may carry no segments; a *restorable archive*
        must call this (see :meth:`from_archive_dict`).
        """
        segment_tables = [s.table for s in self.segments]
        table_names = {t.name for t in self.tables}
        missing = table_names - set(segment_tables)
        if missing:
            raise SchemaContractError(
                "restorable archive is incomplete: tables without a COPY "
                f"segment: {', '.join(sorted(missing))}"
            )
        # __post_init__ already rejects duplicate/foreign segments, so equal
        # sets here imply the exact one-segment-per-table cover.
        if set(segment_tables) != table_names:
            raise SchemaContractError(
                "every table must have exactly one COPY segment"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "copy_settings": dict(_COPY_SETTINGS),
            "data_copy_size": self.data_copy_size,
            "indexes": [i.to_dict() for i in self.indexes],
            "migrations": [m.to_dict() for m in self.migrations],
            "segments": [s.to_dict() for s in self.segments],
            "source_postgres_version": self.source_postgres_version,
            "system_schema_version": self.system_schema_version,
            "tables": [t.to_dict() for t in self.tables],
            "views": [v.to_dict() for v in self.views],
        }

    def to_canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "DatabaseSchema":
        expected = {
            "contract_version",
            "copy_settings",
            "data_copy_size",
            "indexes",
            "migrations",
            "segments",
            "source_postgres_version",
            "system_schema_version",
            "tables",
            "views",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise SchemaContractError("schema.json has unknown or missing fields")
        if value["copy_settings"] != _COPY_SETTINGS:
            raise SchemaContractError("schema.json copy_settings are not the PPBase contract")
        for key in ("tables", "views", "indexes", "migrations", "segments"):
            if not isinstance(value[key], list):
                raise SchemaContractError(f"schema.json {key} must be an array")
        return cls(
            tables=tuple(TableSpec.from_dict(t) for t in value["tables"]),
            views=tuple(ViewSpec.from_dict(v) for v in value["views"]),
            indexes=tuple(IndexSpec.from_dict(i) for i in value["indexes"]),
            migrations=tuple(MigrationSpec.from_dict(m) for m in value["migrations"]),
            segments=tuple(CopySegment.from_dict(s) for s in value["segments"]),
            data_copy_size=value["data_copy_size"],
            source_postgres_version=value["source_postgres_version"],
            contract_version=value["contract_version"],
            system_schema_version=value["system_schema_version"],
        )

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> "DatabaseSchema":
        return cls.from_dict(parse_canonical_json(payload))

    @classmethod
    def from_archive_dict(cls, value: Any) -> "DatabaseSchema":
        """Load a schema that will be **restored**, enforcing completeness.

        Unlike :meth:`from_dict` (which also accepts a schema-only snapshot),
        this path additionally requires every table to have exactly one COPY
        segment.  Any restore code MUST use this entry point so a future caller
        cannot forget the completeness check and restore a partial archive.
        """
        schema = cls.from_dict(value)
        schema.assert_every_table_exported()
        return schema

    @classmethod
    def from_archive_bytes(cls, payload: bytes) -> "DatabaseSchema":
        """Load and completeness-check a restorable ``schema.json`` payload."""
        return cls.from_archive_dict(parse_canonical_json(payload))


# ---------------------------------------------------------------------------
# Live introspection + fail-closed unmanaged-object guard
# ---------------------------------------------------------------------------


async def assert_no_unmanaged_objects(pg_conn: Any) -> None:
    """Raise :class:`UnmanagedObjectError` if ``public`` has anything PPBase
    cannot reproduce.  The backup is refused rather than silently lossy."""
    offenders: list[str] = []

    async def _collect(sql: str, label: str) -> None:
        rows = await pg_conn.fetch(sql)
        for row in rows:
            offenders.append(f"{label}: {row[0]}")

    await _collect(
        "SELECT c.relname || '.' || t.tgname "
        "FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT t.tgisinternal",
        "trigger",
    )
    await _collect(
        "SELECT p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'",
        "function",
    )
    await _collect(
        "SELECT c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' "
        "AND (c.relkind IN ('S', 'm', 'f', 'p') OR c.relispartition)",
        "relation",
    )
    await _collect(
        "SELECT con.conname FROM pg_constraint con "
        "JOIN pg_namespace n ON n.oid = con.connamespace "
        "WHERE n.nspname = 'public' AND con.contype IN ('f', 'c', 'x')",
        "constraint",
    )
    # Deferrable PK/UNIQUE constraints carry timing semantics PPBase never emits
    # and does not reproduce; refuse rather than restore a non-equivalent table.
    await _collect(
        "SELECT con.conname FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND con.contype IN ('p', 'u') "
        "AND con.condeferrable",
        "deferrable_constraint",
    )
    # Custom rewrite rules (anything other than a view's own ``_RETURN`` SELECT
    # rule) rewrite DML in ways that are not part of the contract.
    await _collect(
        "SELECT c.relname || '.' || r.rulename FROM pg_rewrite r "
        "JOIN pg_class c ON c.oid = r.ev_class "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' "
        "AND NOT (r.rulename = '_RETURN' AND r.ev_type = '1' "
        "AND c.relkind = 'v')",
        "rule",
    )
    await _collect(
        "SELECT t.typname FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "LEFT JOIN pg_class c ON c.reltype = t.oid "
        "WHERE n.nspname = 'public' AND ("
        "t.typtype IN ('e', 'd') OR (t.typtype = 'c' AND c.relkind = 'c'))",
        "type",
    )
    await _collect(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relrowsecurity",
        "row_security",
    )
    await _collect(
        "SELECT pol.polname FROM pg_policy pol "
        "JOIN pg_class c ON c.oid = pol.polrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public'",
        "policy",
    )
    await _collect(
        "SELECT child.relname FROM pg_inherits i "
        "JOIN pg_class child ON child.oid = i.inhrelid "
        "JOIN pg_namespace n ON n.oid = child.relnamespace "
        "WHERE n.nspname = 'public'",
        "inheritance",
    )
    extension_rows = await pg_conn.fetch("SELECT extname FROM pg_extension")
    for row in extension_rows:
        if row[0] not in _BASELINE_EXTENSIONS:
            offenders.append(f"extension: {row[0]}")

    if offenders:
        raise UnmanagedObjectError(
            "backup refused: the public schema contains objects PPBase cannot "
            "reproduce: " + "; ".join(sorted(offenders))
        )


# pg index option bit flags (see PostgreSQL access/htup.h / catalog/pg_index).
_INDOPTION_DESC = 0x0001
_INDOPTION_NULLS_FIRST = 0x0002


def _char_flag(value: Any) -> str:
    """Normalise a PostgreSQL ``"char"`` catalog value to a set flag or ``""``.

    ``attgenerated`` / ``attidentity`` are ``"char"`` columns whose unset value
    is a NUL byte.  asyncpg surfaces them as ``bytes`` (``b'\\x00'``) or ``str``;
    return the meaningful non-NUL character, or ``""`` when unset.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        text = value.decode("latin-1")
    else:
        text = str(value)
    return text.strip("\x00")


def _reconcile_table_against_canonical(
    introspected: TableSpec, canonical: TableSpec
) -> None:
    """Reject a live table whose shape diverges from the PPBase canonical model.

    A table with the right *name* but different columns / primary key / unique
    constraints must never be auto-accepted: restoring it would silently produce
    a schema PPBase did not author.  Any drift aborts the backup.

    Columns are reconciled **by name**, not by physical position: PPBase emits a
    collection's initial columns in ``_collections.schema`` field order, but a
    later ``ALTER TABLE ... ADD COLUMN`` appends physically at the end, so an
    evolved table PPBase authored can legitimately carry a physical order that
    differs from the canonical (schema-JSON) order.  Physical order is never
    used for data integrity — the COPY export streams and ``schema.json`` records
    the live physical order, and restore replays that exact order — so only the
    *set* of columns and each column's type/nullability/default must match.
    """
    introspected_by_name = {c.name: c for c in introspected.columns}
    canonical_by_name = {c.name: c for c in canonical.columns}
    if set(introspected_by_name) != set(canonical_by_name):
        raise UnmanagedObjectError(
            f"backup refused: table {introspected.name!r} columns diverge from "
            "the PPBase canonical model"
        )
    for name, introspected_column in introspected_by_name.items():
        if introspected_column != canonical_by_name[name]:
            raise UnmanagedObjectError(
                f"backup refused: table {introspected.name!r} columns diverge "
                "from the PPBase canonical model"
            )
    if introspected.primary_key != canonical.primary_key:
        raise UnmanagedObjectError(
            f"backup refused: table {introspected.name!r} primary key diverges "
            "from the PPBase canonical model"
        )
    if set(introspected.unique_constraints) != set(canonical.unique_constraints):
        raise UnmanagedObjectError(
            f"backup refused: table {introspected.name!r} unique constraints "
            "diverge from the PPBase canonical model"
        )


async def _introspect_tables(
    pg_conn: Any,
    table_inventory: Mapping[str, str],
    canonical_tables: Mapping[str, TableSpec] | None = None,
) -> tuple[TableSpec, ...]:
    table_rows = await pg_conn.fetch(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
        "ORDER BY c.relname"
    )
    # Bidirectional reconciliation: every live table must be in the inventory
    # (checked per-row below) *and* every inventory table must exist live.  A
    # missing expected table blocks the backup rather than yielding a partial
    # snapshot.
    live_names = {r[0] for r in table_rows}
    missing = set(table_inventory) - live_names
    if missing:
        raise UnmanagedObjectError(
            "backup refused: expected PPBase tables are missing from the "
            f"database: {', '.join(sorted(missing))}"
        )
    tables: list[TableSpec] = []
    for table_row in table_rows:
        name = table_row[0]
        if name not in table_inventory:
            raise UnmanagedObjectError(
                f"backup refused: public table {name!r} is not a known PPBase "
                "system table or collection"
            )
        kind = table_inventory[name]
        columns_rows = await pg_conn.fetch(
            "SELECT a.attname, "
            "pg_catalog.format_type(a.atttypid, a.atttypmod) AS coltype, "
            "a.attnotnull, "
            "pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS default_expr, "
            "a.attgenerated, a.attidentity, "
            "a.attcollation::oid AS attcollation, "
            "t.typcollation::oid AS typcollation "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_type t ON t.oid = a.atttypid "
            "LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid "
            "AND ad.adnum = a.attnum "
            "WHERE n.nspname = 'public' AND c.relname = $1 "
            "AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum",
            name,
        )
        for r in columns_rows:
            # Generated / identity columns carry semantics (an expression or a
            # sequence) PPBase never emits and cannot reproduce structurally.
            # ``attgenerated`` / ``attidentity`` are ``"char"`` columns whose
            # unset value is the NUL byte (asyncpg returns them as bytes), so
            # normalise to a flag byte before testing.
            if _char_flag(r["attgenerated"]):
                raise SchemaContractError(
                    f"generated column {r['attname']!r} on table {name!r} is "
                    "not reproducible"
                )
            if _char_flag(r["attidentity"]):
                raise SchemaContractError(
                    f"identity column {r['attname']!r} on table {name!r} is "
                    "not reproducible"
                )
            # A per-column collation other than the type's default would change
            # ordering/uniqueness semantics and is not captured structurally.
            attcoll = int(r["attcollation"])
            typcoll = int(r["typcollation"])
            if attcoll != 0 and attcoll != typcoll:
                raise SchemaContractError(
                    f"non-standard collation on column {r['attname']!r} of "
                    f"table {name!r} is not reproducible"
                )
        columns = tuple(
            ColumnSpec(
                name=r["attname"],
                type=r["coltype"],
                nullable=not r["attnotnull"],
                default=r["default_expr"],
            )
            for r in columns_rows
        )
        pk_rows = await pg_conn.fetch(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) "
            "WHERE n.nspname = 'public' AND c.relname = $1 AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)",
            name,
        )
        unique_rows = await pg_conn.fetch(
            "SELECT con.conname, "
            "(SELECT array_agg(att.attname ORDER BY k.ord) "
            " FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
            " JOIN pg_attribute att ON att.attrelid = con.conrelid "
            " AND att.attnum = k.attnum) AS cols "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = $1 AND con.contype = 'u' "
            "ORDER BY con.conname",
            name,
        )
        spec = TableSpec(
            name=name,
            kind=kind,
            columns=columns,
            primary_key=tuple(r[0] for r in pk_rows),
            unique_constraints=tuple(tuple(r[1]) for r in unique_rows),
        )
        if canonical_tables is not None and name in canonical_tables:
            _reconcile_table_against_canonical(spec, canonical_tables[name])
        tables.append(spec)
    return tuple(tables)


async def _introspect_indexes(
    pg_conn: Any, allowed_relations: Set[str] = frozenset()
) -> tuple[IndexSpec, ...]:
    """Introspect secondary indexes, excluding PK / constraint-backed indexes.

    Reads the ``pg_index`` catalog columns directly (``indkey``, ``indoption``,
    ``indclass``, ``indcollation``, ``indexprs``) instead of parsing the lossy
    per-key ``pg_get_indexdef`` rendering.  DESC / NULLS ordering is preserved
    exactly, and expressions, non-default opclasses, explicit collations and
    exclusion indexes are refused rather than silently dropped.
    """
    # Map every non-default opclass so we can reject anything that is not the
    # type's default (e.g. text_pattern_ops).
    opclass_default: dict[int, bool] = {
        int(row["oid"]): bool(row["opcdefault"])
        for row in await pg_conn.fetch("SELECT oid, opcdefault FROM pg_opclass")
    }
    rows = await pg_conn.fetch(
        "SELECT ic.relname AS index_name, tc.relname AS table_name, "
        "i.indexrelid AS index_oid, "
        "i.indrelid, am.amname AS method, i.indisunique, i.indisexclusion, "
        "i.indnullsnotdistinct, i.indnkeyatts, i.indnatts, "
        "(i.indexprs IS NOT NULL) AS has_expr, "
        "ic.reloptions AS reloptions, (ic.reltablespace <> 0) AS has_tablespace, "
        "pg_get_expr(i.indpred, i.indrelid, true) AS predicate, "
        "i.indkey::int2[] AS indkey, i.indoption::int2[] AS indoption, "
        "i.indclass::oid[] AS indclass, i.indcollation::oid[] AS indcollation "
        "FROM pg_index i "
        "JOIN pg_class ic ON ic.oid = i.indexrelid "
        "JOIN pg_class tc ON tc.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = tc.relnamespace "
        "JOIN pg_am am ON am.oid = ic.relam "
        "WHERE n.nspname = 'public' "
        "AND NOT i.indisprimary "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM pg_constraint con "
        "  WHERE con.conindid = i.indexrelid AND con.contype IN ('u', 'p', 'x')) "
        "ORDER BY ic.relname"
    )
    attr_cache: dict[int, dict[int, tuple[str, int]]] = {}
    indexes: list[IndexSpec] = []
    for row in rows:
        index_name = row["index_name"]
        if row["indisexclusion"]:
            raise UnmanagedObjectError(
                f"backup refused: exclusion index {index_name!r} is not "
                "reproducible"
            )
        if row["has_expr"]:
            raise SchemaContractError(
                f"expression index keys are not supported: {index_name!r}"
            )
        # Storage parameters (WITH (...)) and a non-default tablespace are not
        # represented in the schema contract, so restoring the captured
        # IndexSpec would silently drop them. Fail closed rather than lose them.
        if row["reloptions"] is not None:
            raise SchemaContractError(
                f"index storage parameters are not supported: {index_name!r}"
            )
        if row["has_tablespace"]:
            raise SchemaContractError(
                f"index tablespace is not supported: {index_name!r}"
            )
        relid = int(row["indrelid"])
        if relid not in attr_cache:
            attr_rows = await pg_conn.fetch(
                "SELECT attnum, attname, attcollation FROM pg_attribute "
                "WHERE attrelid = $1 AND attnum > 0 AND NOT attisdropped",
                relid,
            )
            attr_cache[relid] = {
                int(a["attnum"]): (a["attname"], int(a["attcollation"]))
                for a in attr_rows
            }
        attrs = attr_cache[relid]
        indkey = [int(v) for v in row["indkey"]]
        indoption = [int(v) for v in row["indoption"]]
        indclass = [int(v) for v in row["indclass"]]
        indcollation = [int(v) for v in row["indcollation"]]
        nkey = int(row["indnkeyatts"])
        natts = int(row["indnatts"])

        keys: list[IndexKeySpec] = []
        for pos in range(nkey):
            attnum = indkey[pos]
            if attnum == 0:
                raise SchemaContractError(
                    f"expression index keys are not supported: {index_name!r}"
                )
            column, attcollation = attrs[attnum]
            if not opclass_default.get(indclass[pos], False):
                raise SchemaContractError(
                    f"non-default index opclass is not supported: {index_name!r} "
                    f"(column {column!r})"
                )
            key_collation = indcollation[pos]
            if key_collation != 0 and key_collation != attcollation:
                raise SchemaContractError(
                    f"explicit index collation is not supported: {index_name!r} "
                    f"(column {column!r})"
                )
            option = indoption[pos]
            # Only btree (amcanorder) carries meaningful ASC/DESC/NULLS bits.
            # For hash/gin/brin/spgist the bits are always zero and the
            # ordering clause is not even valid syntax, so record no ordering.
            if row["method"] == "btree":
                descending = bool(option & _INDOPTION_DESC)
                nulls_first: bool | None = bool(option & _INDOPTION_NULLS_FIRST)
            else:
                descending = False
                nulls_first = None
            keys.append(
                IndexKeySpec(
                    column=column,
                    descending=descending,
                    nulls_first=nulls_first,
                )
            )

        included: list[str] = []
        for pos in range(nkey, natts):
            attnum = indkey[pos]
            if attnum == 0:
                raise SchemaContractError(
                    f"expression INCLUDE columns are not supported: {index_name!r}"
                )
            included.append(attrs[attnum][0])

        if row["predicate"] is not None:
            # A partial-index predicate can reach a function/operator/type/
            # collation outside pg_catalog; gate those real dependencies by OID.
            await _verify_index_dependencies(
                pg_conn, int(row["index_oid"]), index_name, allowed_relations
            )
        indexes.append(
            IndexSpec(
                name=index_name,
                table=row["table_name"],
                method=row["method"],
                unique=row["indisunique"],
                nulls_not_distinct=bool(row["indnullsnotdistinct"]),
                keys=tuple(keys),
                included=tuple(included),
                predicate=row["predicate"],
            )
        )
    return tuple(indexes)


# Every ``pg_depend`` reference is resolved through exactly these catalogs; a
# reference in any *other* catalog (``pg_language``, ``pg_ts_config``,
# ``pg_extension`` …) is an unrecognised dependency category and fails closed.
# Builtin ``pg_catalog`` objects are *pinned* and therefore never produce a
# ``pg_depend`` row, so anything we actually see here is user/extension defined.
_DEPENDENCY_RESOLUTION_SQL = (
    "d.refclassid::regclass::text AS catalog, "
    "COALESCE(cn.nspname, pn.nspname, tn.nspname, opn.nspname, coln.nspname) "
    "AS nspname, "
    "COALESCE(c.relname, p.proname, t.typname, op.oprname, col.collname) "
    "AS objname "
    "FROM pg_depend d "
    "LEFT JOIN pg_class c ON d.refclassid = 'pg_class'::regclass "
    "AND c.oid = d.refobjid "
    "LEFT JOIN pg_namespace cn ON cn.oid = c.relnamespace "
    "LEFT JOIN pg_proc p ON d.refclassid = 'pg_proc'::regclass "
    "AND p.oid = d.refobjid "
    "LEFT JOIN pg_namespace pn ON pn.oid = p.pronamespace "
    "LEFT JOIN pg_type t ON d.refclassid = 'pg_type'::regclass "
    "AND t.oid = d.refobjid "
    "LEFT JOIN pg_namespace tn ON tn.oid = t.typnamespace "
    "LEFT JOIN pg_operator op ON d.refclassid = 'pg_operator'::regclass "
    "AND op.oid = d.refobjid "
    "LEFT JOIN pg_namespace opn ON opn.oid = op.oprnamespace "
    "LEFT JOIN pg_collation col ON d.refclassid = 'pg_collation'::regclass "
    "AND col.oid = d.refobjid "
    "LEFT JOIN pg_namespace coln ON coln.oid = col.collnamespace "
)


def _assert_dependency_in_contract(
    *, catalog: Any, nspname: Any, objname: Any, subject: str,
    allowed_relations: Set[str],
) -> None:
    """Gate one resolved ``pg_depend`` reference against the closed contract.

    ``catalog`` is the referenced object's catalog (``pg_class``/``pg_proc``/
    ``pg_type``/``pg_operator``/``pg_collation``).  Relations must be a known
    ``public`` PPBase table/view; functions, operators and collations must be
    ``pg_catalog`` builtins; types must be a builtin or the row/array type of a
    known relation.  Every other catalog — and every reference that fails to
    resolve to a namespace — is an unrecognised category and is refused.
    """
    if catalog == "pg_class":
        if nspname == "public" and objname in allowed_relations:
            return
    elif catalog in ("pg_proc", "pg_operator", "pg_collation"):
        if nspname == "pg_catalog":
            return
    elif catalog == "pg_type":
        if nspname == "pg_catalog":
            return
        if nspname == "public" and objname:
            # A leading ``_`` marks the implicit array type of a relation.
            base = objname[1:] if objname.startswith("_") else objname
            if base in allowed_relations:
                return
    raise UnmanagedObjectError(
        f"backup refused: {subject} depends on {catalog} "
        f"{nspname}.{objname} which is outside the PPBase contract"
    )


async def _verify_view_dependencies(
    pg_conn: Any, view_name: str, allowed_relations: Set[str]
) -> None:
    """Refuse a view whose *real* dependencies escape the closed contract.

    The view body was already lexed against the read-only allowlist, but a
    text scan cannot tell a legitimate ``table.column`` reference from a
    cross-schema ``otherschema.table`` one, nor see an external function or
    collation reached through the rewrite rule.  Here we resolve the view's
    actual catalog dependencies through ``pg_depend``/``pg_rewrite`` and gate
    every one of them by OID.
    """
    dep_rows = await pg_conn.fetch(
        "SELECT " + _DEPENDENCY_RESOLUTION_SQL
        + "JOIN pg_rewrite rw ON rw.oid = d.objid "
        "JOIN pg_class v ON v.oid = rw.ev_class "
        "JOIN pg_namespace vn ON vn.oid = v.relnamespace "
        "WHERE vn.nspname = 'public' AND v.relname = $1 "
        "AND d.classid = 'pg_rewrite'::regclass "
        "AND d.refobjid <> v.oid",
        view_name,
    )
    for row in dep_rows:
        _assert_dependency_in_contract(
            catalog=row["catalog"],
            nspname=row["nspname"],
            objname=row["objname"],
            subject=f"view {view_name!r}",
            allowed_relations=allowed_relations,
        )


async def _verify_index_dependencies(
    pg_conn: Any, index_oid: int, index_name: str, allowed_relations: Set[str]
) -> None:
    """Refuse a (partial) index whose predicate reaches outside the contract.

    Key columns, opclasses and collations are already gated elsewhere; the
    remaining vector is a partial-index ``WHERE`` predicate, whose function,
    operator, type or collation references are recorded as *normal* (``'n'``)
    ``pg_depend`` rows on the index relation.  Builtins are pinned and never
    appear, so any row seen here must resolve to a contract object or the
    backup is refused (a mis-resolved ``pg_get_expr`` rendering on restore
    could otherwise change the predicate's meaning).
    """
    dep_rows = await pg_conn.fetch(
        "SELECT " + _DEPENDENCY_RESOLUTION_SQL
        + "WHERE d.classid = 'pg_class'::regclass AND d.objid = $1 "
        "AND d.deptype = 'n'",
        index_oid,
    )
    for row in dep_rows:
        _assert_dependency_in_contract(
            catalog=row["catalog"],
            nspname=row["nspname"],
            objname=row["objname"],
            subject=f"index {index_name!r}",
            allowed_relations=allowed_relations,
        )


async def _introspect_views(
    pg_conn: Any, view_inventory: Set[str], table_names: Set[str]
) -> tuple[ViewSpec, ...]:
    allowed_relations = set(view_inventory) | set(table_names)
    rows = await pg_conn.fetch(
        "SELECT c.relname, pg_get_viewdef(c.oid, true) AS definition, "
        "c.reloptions AS reloptions "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'v' "
        "ORDER BY c.relname"
    )
    # Bidirectional reconciliation: every expected collection view must exist.
    live_views = {row["relname"] for row in rows}
    missing = set(view_inventory) - live_views
    if missing:
        raise UnmanagedObjectError(
            "backup refused: expected PPBase collection views are missing from "
            f"the database: {', '.join(sorted(missing))}"
        )
    views: list[ViewSpec] = []
    for row in rows:
        name = row["relname"]
        if name not in view_inventory:
            raise UnmanagedObjectError(
                f"backup refused: public view {name!r} is not a known PPBase "
                "collection view"
            )
        # View options (security_barrier / security_invoker / check_option) are
        # not represented in the schema contract; restoring the captured
        # ViewSpec would silently drop them. Fail closed rather than lose them.
        if row["reloptions"] is not None:
            raise SchemaContractError(
                f"view options are not supported: {name!r}"
            )
        dep_rows = await pg_conn.fetch(
            "SELECT DISTINCT src.relname FROM pg_depend d "
            "JOIN pg_rewrite r ON r.oid = d.objid "
            "JOIN pg_class v ON v.oid = r.ev_class "
            "JOIN pg_class src ON src.oid = d.refobjid "
            "JOIN pg_namespace sn ON sn.oid = src.relnamespace "
            "WHERE v.relname = $1 AND sn.nspname = 'public' "
            "AND src.relname <> $1 AND src.relkind IN ('r', 'v') "
            "ORDER BY src.relname",
            name,
        )
        # Resolve and gate the view's *actual* catalog dependencies (functions,
        # types, foreign/catalog relations) that a text scan cannot see.
        await _verify_view_dependencies(pg_conn, name, allowed_relations)
        views.append(
            ViewSpec(
                name=name,
                definition=row["definition"],
                depends_on=tuple(r[0] for r in dep_rows),
            )
        )
    return tuple(views)


async def introspect_public_schema(
    pg_conn: Any,
    *,
    table_inventory: Mapping[str, str],
    view_inventory: Set[str] = frozenset(),
    canonical_tables: Mapping[str, TableSpec] | None = None,
    migrations: Sequence[MigrationSpec] = (),
    segments: Sequence[CopySegment] = (),
    data_copy_size: int = 0,
    source_postgres_version: int | None = None,
) -> DatabaseSchema:
    """Snapshot the ``public`` schema into a validated :class:`DatabaseSchema`.

    ``table_inventory`` maps every expected public table name to its kind
    (``system`` / ``base`` / ``auth``); ``view_inventory`` lists every expected
    collection view.  Reconciliation is **bidirectional**: any live table/view
    outside the inventory *and* any inventory entry missing from the live
    database aborts the backup — there is no silent classification.  When
    ``canonical_tables`` is supplied, each managed table's columns / primary key
    / unique constraints are compared against the PPBase canonical model so a
    right-name-but-divergent table is refused.  Fails closed via
    :func:`assert_no_unmanaged_objects` before capturing anything.
    """
    for name, kind in table_inventory.items():
        if kind not in _TABLE_KINDS:
            raise SchemaContractError(
                f"table inventory has unknown kind {kind!r} for {name!r}"
            )
    await assert_no_unmanaged_objects(pg_conn)
    tables = await _introspect_tables(pg_conn, table_inventory, canonical_tables)
    allowed_relations = frozenset(table_inventory) | frozenset(view_inventory)
    views = await _introspect_views(
        pg_conn, view_inventory, frozenset(table_inventory)
    )
    indexes = await _introspect_indexes(pg_conn, allowed_relations)
    return DatabaseSchema(
        tables=tables,
        views=views,
        indexes=indexes,
        migrations=tuple(migrations),
        segments=tuple(segments),
        data_copy_size=data_copy_size,
        source_postgres_version=source_postgres_version,
    )


__all__ = [
    "SCHEMA_CONTRACT_VERSION",
    "SYSTEM_SCHEMA_VERSION",
    "ColumnSpec",
    "DatabaseSchema",
    "IndexKeySpec",
    "IndexSpec",
    "MigrationSpec",
    "SchemaContractError",
    "TableSpec",
    "UnmanagedObjectError",
    "ViewSpec",
    "assert_no_unmanaged_objects",
    "introspect_public_schema",
    "normalize_column_type",
]
