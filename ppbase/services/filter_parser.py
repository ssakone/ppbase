"""PocketBase filter syntax parser using Lark.

Translates PocketBase filter expressions into SQL WHERE clauses with
parameterized bindings.  Also provides ``parse_sort`` for sort expressions.

All literal values are passed as bound parameters -- never interpolated into
SQL strings -- to prevent SQL injection.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lark import Lark, Transformer, v_args
from lark.exceptions import VisitError

from ppbase.models.field_multiplicity import (
    go_json_text,
    is_multiple,
    to_unique_string_list,
)

# ---------------------------------------------------------------------------
# Lark grammar for PocketBase filter syntax
# ---------------------------------------------------------------------------

_GRAMMAR = r"""
?start: expression

?expression: or_expr

?or_expr: and_expr ("||" and_expr)*   -> or_expr

?and_expr: atom ("&&" atom)*         -> and_expr

?atom: comparison
     | "(" expression ")"

?comparison: operand OPERATOR operand -> comparison

OPERATOR: "?!~" | "?!=" | "?>=" | "?<=" | "?>" | "?<" | "?~" | "?="
        | "!~" | "!=" | ">=" | "<=" | ">" | "<" | "~" | "="

?operand: string
        | number
        | boolean
        | null
        | macro
        | field_path

string: ESCAPED_STRING
      | SINGLE_STRING

SINGLE_STRING: "'" /[^']*/ "'"

number: SIGNED_NUMBER

boolean: "true"  -> true_val
       | "false" -> false_val

null: "null"
    | "''"
    | "\"\""

macro: MACRO

MACRO: /@[a-zA-Z_][a-zA-Z0-9_.:]*/

field_path: FIELD_IDENT ("." FIELD_IDENT)*

FIELD_IDENT: /[a-zA-Z_][a-zA-Z0-9_:]*/

%import common.ESCAPED_STRING
%import common.SIGNED_NUMBER
%import common.WS
%ignore WS
%ignore /\/\/[^\n]*/
"""

_parser = Lark(_GRAMMAR, parser="earley", ambiguity="resolve")

# ---------------------------------------------------------------------------
# Operator mapping
# ---------------------------------------------------------------------------

_STANDARD_OPS = {
    "=": "=",
    "!=": "!=",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
}

_LIKE_OPS = {
    "~": "ILIKE",
    "!~": "NOT ILIKE",
}

_ANY_STANDARD_OPS = {
    "?=": "=",
    "?!=": "!=",
    "?>": ">",
    "?>=": ">=",
    "?<": "<",
    "?<=": "<=",
}

_ANY_LIKE_OPS = {
    "?~": "ILIKE",
    "?!~": "NOT ILIKE",
}

_DATETIME_FIELD_TYPES = {"date", "autodate"}
# PocketBase "arrayable" field types: the only ones whose ``:length`` and
# ``:each`` modifiers iterate values (v0.22.34 ``schema.ArraybleFieldTypes``,
# v0.40.6 ``MultiValuer`` fields).
_ARRAYABLE_FIELD_TYPES = {"select", "file", "relation"}
_SYSTEM_FIELD_TYPES = {
    "id": "text",
    "created": "autodate",
    "updated": "autodate",
}


# ---------------------------------------------------------------------------
# PocketBase LEFT JOIN paths and multi-match
# ---------------------------------------------------------------------------
#
# PocketBase resolves every relation, back-relation and ``@collection`` path
# with LEFT JOINs on the main query (``SELECT DISTINCT``), so a missing target
# is a NULL row, and adds a "multi-match" subquery to non-``?`` comparisons
# whose path is multiple (v0.22.34 resolvers/record_field_resolve_runner.go
# l.144-182, l.347-716; resolvers/record_field_resolver.go l.121-138;
# resolvers/multi_match_subquery.go l.19-70; tools/search/filter.go l.183-215,
# l.290-368, l.569-706).  PPBase registers the same joins (one alias per path
# prefix, shared by every comparison of the filter) and renders them as
# ``EXISTS (SELECT 1 FROM (SELECT 1) LEFT JOIN ... WHERE ...)`` correlated to
# the current row, which keeps PocketBase's semantics: the EXISTS is pushed
# into each ``||`` operand and groups the ``&&`` operands that share a join.


@dataclass(frozen=True)
class _Join:
    """One LEFT JOIN of the main query (``sql`` is the full clause)."""

    alias: str
    sql: str
    parent: str | None


@dataclass(frozen=True)
class _MultiMatch:
    """PocketBase multi-match subquery: ``value`` over ``from_sql`` rows."""

    from_sql: str
    value: str


@dataclass(frozen=True)
class _PathOperand:
    """A resolved relation/``@collection`` operand.

    ``sql`` reads the main-query join aliases listed in ``aliases`` (with
    their parents); ``multi_match`` is set when PocketBase would attach a
    multi-match subquery to a non-``?`` comparison.
    """

    sql: str
    aliases: frozenset[str]
    multi_match: _MultiMatch | None
    field_type: str = ""
    is_json: bool = False


@dataclass(frozen=True)
class _Side:
    """One operand of a comparison: SQL, emptiness class and parse node.

    ``kind`` follows ``tools/search/filter.go`` ``resolveEqualExpr``:
    ``"empty"`` (NULL or a ``''`` value), ``"value"`` (a known non-empty
    value) or ``"column"`` (anything evaluated per row).
    """

    sql: str
    kind: str
    node: tuple
    is_json: bool = False


class _Cond:
    """A boolean condition tree and the join aliases it reads."""

    __slots__ = ("aliases", "children", "kind", "sql")

    def __init__(
        self,
        kind: str,
        sql: str = "",
        children: tuple[_Cond, ...] = (),
        aliases: frozenset[str] = frozenset(),
    ) -> None:
        self.kind = kind
        self.sql = sql
        self.children = children
        self.aliases = aliases

    @classmethod
    def atom(cls, sql: str, aliases: frozenset[str] = frozenset()) -> _Cond:
        return cls("atom", sql=sql, aliases=aliases)

    @classmethod
    def of(cls, item: Any) -> _Cond:
        return item if isinstance(item, _Cond) else cls.atom(str(item))

    @classmethod
    def combine(cls, kind: str, items: list[Any]) -> _Cond:
        children = tuple(cls.of(item) for item in items)
        if len(children) == 1:
            return children[0]
        aliases: frozenset[str] = frozenset()
        for child in children:
            aliases |= child.aliases
        return cls(kind, children=children, aliases=aliases)


@dataclass(frozen=True)
class _TraversalStep:
    """One relation or back-relation hop in a dotted filter path."""

    relation_key: str
    target_table: str
    max_select: int
    kind: str = "forward"
    source_field: str | None = None
    back_field: str | None = None
    # Explicit multiplicity from the relation index (``maxSelect: null`` is a
    # multiple relation); older metadata only carries ``max_select``.
    multiple: bool | None = None
    # Back relation only: the back field has a single-column unique index.
    unique: bool = False

    @property
    def is_multiple(self) -> bool:
        if self.multiple is not None:
            return self.multiple
        return self.max_select > 1

    @property
    def multi_match(self) -> bool:
        """Whether PocketBase enables its multi-match for this hop.

        A multiple relation always does; a back relation does unless its
        field is single with a single-column unique index (v0.22.34
        resolvers/record_field_resolve_runner.go l.561-565 and l.594-596,
        v0.40.6 core/record_field_resolver_runner.go l.589-596).
        """
        if self.kind == "back":
            return self.is_multiple or not self.unique
        return self.is_multiple


@dataclass(frozen=True)
class _ResolvedPath:
    """Resolved SQL expression and traversal hops for a dotted field path."""

    steps: list[_TraversalStep]
    sql: str
    is_json: bool = False
    field_type: str = ""
    # Whether the last segment is a multi-value field (shared rule).
    leaf_multiple: bool = False
    # Column read on the last table (the JSON column for a JSON path).
    leaf_field: str = ""
    json_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class _LeafOperand:
    """The last segment of a relation or ``@collection`` path.

    ``sql`` is the raw column; ``modifier`` is the optional ``:length``,
    ``:each`` or ``:lower`` suffix of the last segment.
    """

    sql: str
    field_type: str = ""
    multiple: bool = False
    modifier: str | None = None


# ---------------------------------------------------------------------------
# Transformer: AST -> (sql_fragment, params_dict)
# ---------------------------------------------------------------------------


_SAFE_IDENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_"
)
_MODIFIER_NAMES = {"isset", "changed", "length", "each", "lower"}


def _sanitize_ident(s: str) -> str:
    """Raise if *s* contains characters unsuitable for a SQL identifier."""
    for ch in s:
        if ch not in _SAFE_IDENT_CHARS:
            raise ValueError(f"Invalid character in identifier: {ch!r}")
    return s


class _FilterTransformer(Transformer):
    """Transform the parse tree into (SQL text, params dict)."""

    def __init__(
        self,
        request_context: dict[str, Any] | None = None,
        relation_resolver: dict[str, dict[str, Any]] | None = None,
        collection_resolver: dict[str, dict[str, Any]] | None = None,
        current_table: str | None = None,
    ) -> None:
        super().__init__()
        self._param_counter = 0
        self._params: dict[str, Any] = {}
        self._request_context = request_context or {}
        # Maps relation field name → recursive relation metadata.
        self._relation_resolver = relation_resolver or {}
        # Maps collection name → {"table": name, "relations": RelationResolver}.
        self._collection_resolver = collection_resolver or {}
        self._current_table = _sanitize_ident(current_table) if current_table else None
        # SQL of direct multi-value fields rendered as their JSON text: they
        # are plain scalars for every operator, like in PocketBase.
        self._json_text_operands: set[str] = set()
        # PocketBase-like LEFT JOINs of relation/@collection paths, keyed by
        # path prefix so that every comparison shares the same rows.
        self._joins: dict[str, _Join] = {}
        self._join_keys: dict[tuple[Any, ...], str] = {}
        self._alias_counter = 0

    def _next_param(self, value: Any) -> str:
        self._param_counter += 1
        name = f"_fp{self._param_counter}"
        self._params[name] = value
        return name

    # -- Leaf nodes ----------------------------------------------------------

    def string(self, items: list) -> tuple[str, str]:
        raw = str(items[0])
        # Strip surrounding quotes
        if raw.startswith("'") and raw.endswith("'"):
            val = raw[1:-1]
        elif raw.startswith('"') and raw.endswith('"'):
            val = raw[1:-1]
        else:
            val = raw
        return ("literal", val)

    def number(self, items: list) -> tuple[str, float | int]:
        raw = str(items[0])
        val = float(raw)
        if val == int(val) and "." not in raw:
            val = int(raw)
        return ("literal", val)

    def true_val(self, _items: list) -> tuple[str, bool]:
        return ("literal", True)

    def false_val(self, _items: list) -> tuple[str, bool]:
        return ("literal", False)

    def null(self, _items: list) -> tuple[str, None]:
        return ("null", None)

    def macro(self, items: list) -> tuple:
        name = str(items[0])  # e.g. "@now", "@request.auth.id"
        stripped = name.lstrip("@")

        # --- @collection.collectionName.fieldName --------------------------
        if stripped.startswith("collection."):
            parts = stripped.split(".")
            if len(parts) < 3 or not parts[1] or not parts[2]:
                raise ValueError(
                    f"Invalid @collection reference: @{stripped}. "
                    "Expected format: @collection.collectionName.fieldName"
                )
            coll_ref = parts[1]
            alias_key = ""
            if ":" in coll_ref:
                raw_coll, raw_alias = coll_ref.split(":", 1)
                coll_name = _sanitize_ident(raw_coll)
                alias_key = _sanitize_ident(raw_alias)
                group_key = f"{coll_name}:{alias_key}"
            else:
                coll_name = _sanitize_ident(coll_ref)
                group_key = coll_name
            raw_path = [part for part in parts[2:] if part]
            modifier: str | None = None
            if raw_path:
                raw_path[-1], modifier = self._split_modifier(raw_path[-1])
            field_path = tuple(_sanitize_ident(part) for part in raw_path)
            if not field_path:
                raise ValueError(
                    f"Invalid @collection reference: @{stripped}. "
                    "Expected format: @collection.collectionName.fieldName"
                )
            return ("collection_ref", coll_name, field_path, group_key, modifier)

        return ("macro", name)

    def _resolver_fields(
        self,
        resolver: dict[str, dict[str, Any]] | None,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(resolver, dict):
            return {}
        fields = resolver.get("__fields__")
        return fields if isinstance(fields, dict) else {}

    def _is_flat_resolver(self, resolver: dict[str, Any] | None) -> bool:
        return isinstance(resolver, dict) and resolver.get("__flat__") is True

    def _flat_collection(
        self,
        resolver: dict[str, Any] | None,
        table_name: str,
    ) -> dict[str, Any]:
        if not self._is_flat_resolver(resolver):
            return {}
        collections = resolver.get("collections")
        if not isinstance(collections, dict):
            return {}
        coll = collections.get(table_name)
        return coll if isinstance(coll, dict) else {}

    def _flat_fields(
        self,
        resolver: dict[str, Any] | None,
        table_name: str,
    ) -> dict[str, dict[str, Any]]:
        fields = self._flat_collection(resolver, table_name).get("fields")
        return fields if isinstance(fields, dict) else {}

    def _flat_relation_info(
        self,
        resolver: dict[str, Any] | None,
        table_name: str,
        segment: str,
    ) -> dict[str, Any] | None:
        coll = self._flat_collection(resolver, table_name)
        relations = coll.get("relations")
        if isinstance(relations, dict):
            info = relations.get(segment)
            if isinstance(info, dict):
                return info

        back_relations = resolver.get("back_relations") if isinstance(resolver, dict) else None
        if isinstance(back_relations, dict):
            by_collection = back_relations.get(table_name)
            if isinstance(by_collection, dict):
                info = by_collection.get(segment)
                if isinstance(info, dict):
                    return info
        return None

    def _field_info_for_table(
        self,
        table_name: str | None,
        field_name: str,
    ) -> dict[str, Any] | None:
        """Return the schema metadata (type/options) of a field, if known."""
        safe_field = _sanitize_ident(str(field_name))
        safe_table = _sanitize_ident(str(table_name)) if table_name else ""
        if safe_table and self._is_flat_resolver(self._relation_resolver):
            field_info = self._flat_fields(self._relation_resolver, safe_table).get(
                safe_field,
            )
            if isinstance(field_info, dict):
                return field_info
        fields = self._resolver_fields(self._relation_resolver)
        field_info = fields.get(safe_field) if isinstance(fields, dict) else None
        return field_info if isinstance(field_info, dict) else None

    def _is_multi_value_field(self, table_name: str | None, field_name: str) -> bool:
        """Return whether a current-collection field stores an array."""
        return self._info_is_multiple(self._field_info_for_table(table_name, field_name))

    def _field_type_for_table(self, table_name: str | None, field_name: str) -> str:
        safe_field = _sanitize_ident(str(field_name))
        if safe_field in _SYSTEM_FIELD_TYPES:
            return _SYSTEM_FIELD_TYPES[safe_field]

        safe_table = _sanitize_ident(str(table_name)) if table_name else ""
        if safe_table and self._is_flat_resolver(self._relation_resolver):
            field_info = self._flat_fields(self._relation_resolver, safe_table).get(
                safe_field,
            )
            if isinstance(field_info, dict):
                return str(field_info.get("type") or "")

        fields = self._resolver_fields(self._relation_resolver)
        field_info = fields.get(safe_field) if isinstance(fields, dict) else None
        if isinstance(field_info, dict):
            return str(field_info.get("type") or "")
        return ""

    def _collection_resolver_field_info(
        self,
        table_name: str,
        field_name: str,
    ) -> dict[str, Any] | None:
        safe_field = _sanitize_ident(str(field_name))
        safe_table = _sanitize_ident(str(table_name))
        if self._is_flat_resolver(self._collection_resolver):
            field_info = self._flat_fields(self._collection_resolver, safe_table).get(
                safe_field,
            )
        else:
            coll_info = self._collection_resolver.get(safe_table)
            fields = coll_info.get("fields", {}) if isinstance(coll_info, dict) else {}
            field_info = fields.get(safe_field) if isinstance(fields, dict) else None
        return field_info if isinstance(field_info, dict) else None

    def _field_type_for_collection_resolver(
        self,
        table_name: str,
        field_name: str,
    ) -> str:
        safe_field = _sanitize_ident(str(field_name))
        if safe_field in _SYSTEM_FIELD_TYPES:
            return _SYSTEM_FIELD_TYPES[safe_field]
        field_info = self._collection_resolver_field_info(table_name, safe_field)
        if isinstance(field_info, dict):
            return str(field_info.get("type") or "")
        return ""

    @staticmethod
    def _info_is_multiple(field_info: dict[str, Any] | None) -> bool:
        """Apply the shared multiplicity rule to resolver field metadata."""
        if not isinstance(field_info, dict):
            return False
        options = field_info.get("options")
        return is_multiple(
            field_info.get("type"),
            options if isinstance(options, dict) else {},
        )

    def _literal_for_field_type(
        self,
        node: tuple[str, Any],
        field_type: str,
    ) -> tuple[str, Any]:
        if (
            field_type not in _DATETIME_FIELD_TYPES
            or not isinstance(node, tuple)
            or len(node) < 2
            or node[0] != "literal"
            or not isinstance(node[1], str)
        ):
            return node

        value = node[1].strip()
        if not value:
            return node

        normalized = value.replace("Z", "+00:00")
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T", 1)
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return node
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        else:
            parsed = parsed.astimezone(UTC)
        return ("literal", parsed)

    def _operand_field_type(self, node: tuple[str, Any]) -> str:
        if not isinstance(node, tuple) or len(node) < 2:
            return ""
        kind = node[0]
        if kind == "field":
            if len(node) >= 3 and node[2]:
                return str(node[2])
            if "." in str(node[1]):
                return ""
            return self._field_type_for_table(self._current_table, str(node[1]))
        if kind == "relation_ref" and len(node) >= 2 and isinstance(node[1], _ResolvedPath):
            return node[1].field_type
        if kind == "sql_ref" and len(node) >= 4:
            return str(node[3] or "")
        return ""

    def _coerce_comparison_literals(
        self,
        left: tuple[str, Any],
        right: tuple[str, Any],
    ) -> tuple[tuple[str, Any], tuple[str, Any]]:
        left_type = self._operand_field_type(left)
        right_type = self._operand_field_type(right)
        if left_type in _DATETIME_FIELD_TYPES:
            right = self._literal_for_field_type(right, left_type)
        if right_type in _DATETIME_FIELD_TYPES:
            left = self._literal_for_field_type(left, right_type)
        return left, right

    def _step_from_info(self, segment: str, info: dict[str, Any]) -> _TraversalStep:
        kind = str(info.get("kind", "forward") or "forward")
        target_table = _sanitize_ident(str(info["table"]))
        max_select = int(info.get("max_select", 1) or 1)
        raw_multiple = info.get("multiple")
        multiple = bool(raw_multiple) if raw_multiple is not None else None
        source_field = _sanitize_ident(str(info.get("field") or segment))
        back_field_raw = info.get("back_field")
        back_field = (
            _sanitize_ident(str(back_field_raw))
            if back_field_raw is not None
            else None
        )
        if kind == "back" and not back_field:
            raise ValueError(f"Invalid back-relation metadata for {segment}.")
        return _TraversalStep(
            relation_key=_sanitize_ident(segment),
            target_table=target_table,
            max_select=max_select,
            kind=kind,
            source_field=source_field,
            back_field=back_field,
            multiple=multiple,
            unique=bool(info.get("unique", False)),
        )

    def _json_extract_sql(self, column_sql: str, path_parts: list[str] | tuple[str, ...]) -> str:
        if not path_parts:
            return column_sql
        args = ", ".join(f"'{_sanitize_ident(str(part))}'" for part in path_parts)
        return f"jsonb_extract_path({column_sql}, {args})"

    def _resolve_dotted_path(
        self,
        parts: list[str] | tuple[str, ...],
        *,
        start_table: str,
        relations: dict[str, dict[str, Any]] | None,
        fields: dict[str, dict[str, Any]] | None = None,
        allow_unresolved: bool = True,
        context: str = "field path",
    ) -> _ResolvedPath | None:
        """Resolve a PocketBase dotted path into SQL and traversal steps.

        This mirrors PocketBase's segment-by-segment resolver: each segment can
        be a direct relation, a synthetic back-relation, or a JSON field that
        consumes the remaining segments as a JSON path.  Like PocketBase
        0.22.34, ``rel.id`` joins the target (a missing target is NULL); the
        stored-column shortcut only exists since v0.23.0 (v0.40.6
        core/record_field_resolver_runner.go l.651-657).
        """
        safe_parts = [_sanitize_ident(str(part)) for part in parts]
        if not safe_parts:
            return None

        current_table = _sanitize_ident(start_table) if start_table else ""
        flat_resolver = relations if self._is_flat_resolver(relations) else None
        current_relations = (
            {}
            if flat_resolver
            else relations if isinstance(relations, dict) else {}
        )
        current_fields = (
            self._flat_fields(flat_resolver, current_table)
            if flat_resolver
            else fields if isinstance(fields, dict) else self._resolver_fields(current_relations)
        )
        steps: list[_TraversalStep] = []
        idx = 0

        while idx < len(safe_parts):
            part = safe_parts[idx]

            if idx == len(safe_parts) - 1:
                field_info = (
                    current_fields.get(part)
                    if isinstance(current_fields, dict)
                    else None
                )
                field_type = (
                    str(field_info.get("type") or "")
                    if isinstance(field_info, dict)
                    else _SYSTEM_FIELD_TYPES.get(part, "")
                )
                return _ResolvedPath(
                    steps,
                    self._column_expr_for_table(current_table, part),
                    field_type=field_type,
                    leaf_multiple=self._info_is_multiple(field_info),
                    leaf_field=part,
                )

            field_info = current_fields.get(part) if isinstance(current_fields, dict) else None
            field_type = str(field_info.get("type", "")) if isinstance(field_info, dict) else ""
            if field_type in {"json", "geoPoint"}:
                return _ResolvedPath(
                    steps,
                    self._json_extract_sql(
                        self._column_expr_for_table(current_table, part),
                        safe_parts[idx + 1:],
                    ),
                    is_json=True,
                    field_type="",
                    leaf_field=part,
                    json_path=tuple(safe_parts[idx + 1:]),
                )

            info = (
                self._flat_relation_info(flat_resolver, current_table, part)
                if flat_resolver
                else current_relations.get(part) if isinstance(current_relations, dict) else None
            )
            if isinstance(info, dict) and "table" in info:
                step = self._step_from_info(part, info)
                steps.append(step)
                current_table = step.target_table
                if flat_resolver:
                    current_fields = self._flat_fields(flat_resolver, current_table)
                else:
                    nested = info.get("relations")
                    current_relations = nested if isinstance(nested, dict) else {}
                    nested_fields = info.get("fields")
                    current_fields = (
                        nested_fields
                        if isinstance(nested_fields, dict) and nested_fields
                        else self._resolver_fields(current_relations)
                    )
                idx += 1
                continue

            if steps:
                raise ValueError(
                    f"Invalid relation traversal: {'.'.join(safe_parts)}. "
                    "Only relation, back-relation, or JSON segments may be chained "
                    "before the final field name."
                )
            if not allow_unresolved:
                raise ValueError(
                    f"Invalid {context}: {'.'.join(safe_parts)}. "
                    "Missing relation metadata for the referenced collection."
                )
            return None

        return None

    def field_path(self, items: list) -> tuple:
        parts = [str(t) for t in items]
        modifier: str | None = None

        # Support field modifiers suffixes, e.g. "tags:length".
        base_last, maybe_modifier = self._split_modifier(parts[-1])
        if maybe_modifier:
            parts[-1] = base_last
            modifier = maybe_modifier

        # Detect relation field traversal (e.g. author.name, author.company.name)
        if len(parts) >= 2 and self._relation_resolver:
            resolved = self._resolve_dotted_path(
                parts,
                start_table=self._current_table or "",
                relations=self._relation_resolver,
                fields=self._resolver_fields(self._relation_resolver),
                allow_unresolved=True,
            )
            if resolved is not None and resolved.steps:
                return (
                    "relation_ref",
                    resolved,
                    None if resolved.is_json else modifier,
                )
            if resolved is not None and resolved.is_json:
                return ("sql_ref", resolved.sql, resolved.is_json, resolved.field_type)

        if modifier:
            return ("field_modifier", ".".join(parts), modifier)
        field_name = ".".join(parts)
        field_type = (
            self._field_type_for_table(self._current_table, field_name)
            if len(parts) == 1
            else ""
        )
        return ("field", field_name, field_type)

    # -- Comparisons ---------------------------------------------------------

    @v_args(inline=True)
    def comparison(
        self,
        left: tuple[str, Any],
        op_token: Any,
        right: tuple[str, Any],
    ) -> str | _Cond:
        op = str(op_token).strip()
        if op in _STANDARD_OPS:
            left, right = self._coerce_comparison_literals(left, right)

        # ── Relation, back-relation and @collection paths (LEFT JOINs) ──
        if self._is_path_node(left) or self._is_path_node(right):
            return self._path_comparison(left, op, right)

        # ── :each modifier comparisons (array "all items match") ──────
        left_each = self._extract_each_operand(left)
        right_each = self._extract_each_operand(right)
        if left_each is not None or right_each is not None:
            return self._each_comparison(
                left, op, right, left_each, right_each,
            )

        # ── Regular comparisons ────────────────────────────────────────
        left_sql = self._operand_to_sql(left)
        right_sql = self._operand_to_sql(right)

        # Standard comparisons
        if op in _STANDARD_OPS:
            sql_op = _STANDARD_OPS[op]
            # NULL special handling
            if right[0] == "null":
                if sql_op == "=":
                    return f"{left_sql} IS NULL"
                elif sql_op == "!=":
                    return f"{left_sql} IS NOT NULL"
            if left[0] == "null":
                if sql_op == "=":
                    return f"{right_sql} IS NULL"
                elif sql_op == "!=":
                    return f"{right_sql} IS NOT NULL"
            return f"{left_sql} {sql_op} {right_sql}"

        # LIKE / NOT LIKE
        if op in _LIKE_OPS:
            sql_op = _LIKE_OPS[op]
            return self._like_comparison_sql(left_sql, right, right_sql, sql_op)

        # ANY standard operators (for array columns)
        if op in _ANY_STANDARD_OPS:
            sql_op = _ANY_STANDARD_OPS[op]
            return self._scalar_or_array_any_sql(left_sql, right_sql, sql_op)

        # ANY LIKE operators
        if op in _ANY_LIKE_OPS:
            sql_op = _ANY_LIKE_OPS[op]
            pattern_sql = self._like_pattern_sql(right, right_sql)
            return self._scalar_or_array_like_sql(left_sql, pattern_sql, sql_op)

        return f"{left_sql} = {right_sql}"

    # -- @collection comparison helpers --------------------------------------

    def _qualified_column(self, table_name: str, field_name: str) -> str:
        return f'"{_sanitize_ident(table_name)}"."{_sanitize_ident(field_name)}"'

    def _column_expr_for_table(self, table_name: str | None, field_name: str) -> str:
        if table_name:
            return self._qualified_column(table_name, field_name)
        return self._field_expr(field_name)

    def _text_cast_sql(self, sql_expr: str) -> str:
        if sql_expr.startswith(":"):
            return f"CAST({sql_expr} AS text)"
        return f"{sql_expr}::text"

    def _like_pattern_sql(self, node: tuple[str, Any], operand_sql: str) -> str:
        """Build a PocketBase-like contains pattern for LIKE comparisons."""
        if node[0] == "literal":
            pname = self._next_param(f"%{node[1]}%")
            return f":{pname}"
        if node[0] == "null":
            return "NULL"
        return f"('%' || {self._text_cast_sql(operand_sql)} || '%')"

    def _like_comparison_sql(
        self,
        match_sql: str,
        pattern_node: tuple[str, Any],
        pattern_sql: str,
        sql_op: str,
    ) -> str:
        return (
            f"{self._text_cast_sql(match_sql)} {sql_op} "
            f"{self._like_pattern_sql(pattern_node, pattern_sql)}"
        )

    def _is_bound_param(self, sql_expr: str) -> bool:
        """Return whether ``sql_expr`` is a bare ``:param`` bound while parsing.

        Literals and ``@request.*`` values are plain scalars (PocketBase binds
        a list or an object as its JSON text, see ``_request_value``), so they
        are compared directly: ``to_jsonb()`` on an untyped parameter cannot
        be resolved by PostgreSQL.
        """
        return (
            sql_expr.startswith(":")
            and sql_expr[1:].isidentifier()
            and sql_expr[1:] in self._params
        )

    def _scalar_or_array_any_sql(
        self,
        container_sql: str,
        value_sql: str,
        sql_op: str,
    ) -> str:
        """Build scalar-safe ``?=``/``?!=`` SQL.

        PocketBase applies ``?=`` to both multi-value fields and scalar relation
        fields. PostgreSQL ``ANY(varchar)`` is invalid, so equality-style
        operators use a JSONB shape check that works for scalar and array
        columns.
        """
        if container_sql in self._json_text_operands:
            # Direct multi-value field: plain comparison of its JSON text.
            if sql_op in {"=", "!="}:
                return f"{self._text_cast_sql(value_sql)} {sql_op} {container_sql}"
            return f"{container_sql} {sql_op} {value_sql}"

        if sql_op not in {"=", "!="}:
            return f"{value_sql} {sql_op} ANY({container_sql})"

        container_text = self._text_cast_sql(container_sql)
        value_text = self._text_cast_sql(value_sql)
        if self._is_bound_param(container_sql):
            return f"{value_text} {sql_op} {container_text}"
        return (
            "(CASE "
            f"WHEN jsonb_typeof(to_jsonb({container_sql})) = 'array' THEN "
            f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(to_jsonb({container_sql})) "
            f"AS _elem(value) WHERE {value_text} {sql_op} _elem.value) "
            f"ELSE {value_text} {sql_op} {container_text} "
            "END)"
        )

    def _scalar_or_array_like_sql(
        self,
        container_sql: str,
        pattern_sql: str,
        sql_op: str,
    ) -> str:
        """Build scalar-safe ``?~``/``?!~`` SQL."""
        container_text = self._text_cast_sql(container_sql)
        if container_sql in self._json_text_operands:
            return f"{container_text} {sql_op} {pattern_sql}"
        if self._is_bound_param(container_sql):
            return f"{container_text} {sql_op} {pattern_sql}"
        return (
            "(CASE "
            f"WHEN jsonb_typeof(to_jsonb({container_sql})) = 'array' THEN "
            f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(to_jsonb({container_sql})) "
            f"AS _elem(value) WHERE _elem.value {sql_op} {pattern_sql}) "
            f"ELSE {container_text} {sql_op} {pattern_sql} "
            "END)"
        )

    # -- Relation, back-relation and @collection paths ----------------------

    @staticmethod
    def _is_path_node(node: Any) -> bool:
        return isinstance(node, tuple) and bool(node) and node[0] in {
            "relation_ref",
            "collection_ref",
        }

    def _resolve_collection_path(
        self,
        coll_name: str,
        field_path: tuple[str, ...],
    ) -> _ResolvedPath:
        """Resolve the ``path`` of ``@collection.<coll_name>.<path>``."""
        if len(field_path) == 1:
            info = self._collection_resolver_field_info(coll_name, field_path[0])
            return _ResolvedPath(
                [],
                self._qualified_column(coll_name, field_path[0]),
                field_type=self._field_type_for_collection_resolver(
                    coll_name,
                    field_path[0],
                ),
                leaf_multiple=self._info_is_multiple(info),
                leaf_field=field_path[0],
            )

        if self._is_flat_resolver(self._collection_resolver):
            relations = self._collection_resolver
            fields = self._flat_fields(self._collection_resolver, coll_name)
        else:
            coll_info = self._collection_resolver.get(coll_name)
            relations = coll_info.get("relations", {}) if isinstance(coll_info, dict) else {}
            fields = coll_info.get("fields", {}) if isinstance(coll_info, dict) else {}
        resolved = self._resolve_dotted_path(
            field_path,
            start_table=coll_name,
            relations=relations,
            fields=fields if isinstance(fields, dict) else {},
            allow_unresolved=False,
            context="@collection relation traversal",
        )
        if resolved is None:
            raise ValueError(
                f"Invalid @collection relation traversal: "
                f"@collection.{coll_name}.{'.'.join(field_path)}."
            )
        return resolved

    def _next_alias(self, prefix: str) -> str:
        self._alias_counter += 1
        return f"{prefix}{self._alias_counter}"

    def _register_join(
        self,
        key: tuple[Any, ...],
        parent: str | None,
        template: str,
    ) -> str:
        """Register (once per path prefix) a main-query LEFT JOIN.

        ``template`` is the JOIN clause with ``@ALIAS@`` for the new alias.
        """
        alias = self._join_keys.get(key)
        if alias is None:
            alias = self._next_alias("__pb")
            self._joins[alias] = _Join(alias, template.replace("@ALIAS@", alias), parent)
            self._join_keys[key] = alias
        return alias

    def _alias_closure(self, alias: str | None) -> frozenset[str]:
        aliases: set[str] = set()
        while alias is not None and alias in self._joins:
            aliases.add(alias)
            alias = self._joins[alias].parent
        return frozenset(aliases)

    def _column_of(self, alias: str | None, field_name: str) -> str:
        if alias is None:
            return self._column_expr_for_table(None, field_name)
        return f'"{alias}"."{_sanitize_ident(field_name)}"'

    def _join_path(
        self,
        root: str | None,
        resolved: _ResolvedPath,
        modifier: str | None,
        make: Callable[[tuple[Any, ...], str | None, str], str],
    ) -> tuple[str, str | None, bool]:
        """Join the hops and the leaf of a path from ``root``.

        Mirrors ``processActiveProps``: a single relation joins its target, a
        multiple one ``json_each`` then the target, a back relation the
        referencing records; every join is a LEFT JOIN.  Returns the leaf SQL,
        the deepest alias and whether a hop or the leaf enables PocketBase's
        multi-match (v0.22.34 resolvers/record_field_resolve_runner.go
        l.347-477 and l.515-716).
        """
        parent = root
        multi_match = False
        for step in resolved.steps:
            target = _sanitize_ident(step.target_table)
            if step.kind == "back":
                back_field = _sanitize_ident(step.back_field or "")
                parent_id = self._column_of(parent, "id")
                if step.is_multiple:
                    on = f'{parent_id} = ANY("@ALIAS@"."{back_field}")'
                else:
                    on = f'"@ALIAS@"."{back_field}" = {parent_id}'
                parent = make(
                    ("back", parent, target, back_field),
                    parent,
                    f'LEFT JOIN "{target}" AS "@ALIAS@" ON {on}',
                )
            else:
                source = self._column_of(parent, step.source_field or step.relation_key)
                if step.is_multiple:
                    each = make(
                        ("each", parent, source),
                        parent,
                        f'LEFT JOIN LATERAL unnest({source}) AS "@ALIAS@"(value) ON TRUE',
                    )
                    parent = make(
                        ("forward", parent, source),
                        each,
                        f'LEFT JOIN "{target}" AS "@ALIAS@" '
                        f'ON "@ALIAS@"."id" = "{each}".value',
                    )
                else:
                    parent = make(
                        ("forward", parent, source),
                        parent,
                        f'LEFT JOIN "{target}" AS "@ALIAS@" ON "@ALIAS@"."id" = {source}',
                    )
            multi_match = multi_match or step.multi_match

        if resolved.is_json:
            return (
                self._json_extract_sql(
                    self._column_of(parent, resolved.leaf_field),
                    resolved.json_path,
                ),
                parent,
                multi_match,
            )

        column = self._column_of(parent, resolved.leaf_field)
        if modifier == "each" and resolved.field_type in _ARRAYABLE_FIELD_TYPES:
            elements = self._each_elements_sql(column, multiple=resolved.leaf_multiple)
            each = make(
                ("leaf_each", parent, column),
                parent,
                f'LEFT JOIN LATERAL unnest({elements}) AS "@ALIAS@"(value) ON TRUE',
            )
            return (
                f'"{each}".value',
                each,
                multi_match or resolved.leaf_multiple,
            )
        leaf = _LeafOperand(
            column,
            field_type=resolved.field_type,
            multiple=resolved.leaf_multiple,
            modifier=modifier,
        )
        return self._leaf_value_sql(leaf), parent, multi_match

    def _path_operand(self, node: tuple) -> _PathOperand:
        """Resolve a ``relation_ref``/``collection_ref`` node like PocketBase."""
        if node[0] == "relation_ref":
            resolved: _ResolvedPath = node[1]
            modifier = node[2] if len(node) > 2 else None
            collection: tuple[str, str] | None = None
        else:
            coll_name = _sanitize_ident(str(node[1]))
            field_path = tuple(_sanitize_ident(str(part)) for part in node[2])
            resolved = self._resolve_collection_path(coll_name, field_path)
            modifier = None if resolved.is_json else (node[4] if len(node) > 4 else None)
            collection = (coll_name, str(node[3]))

        # Main query: shared joins.
        root = None
        if collection is not None:
            # A @collection join has no ON condition: every row of the
            # collection, or one NULL row when it is empty (l.144-182).
            root = self._register_join(
                ("collection", collection[1]),
                None,
                f'LEFT JOIN "{collection[0]}" AS "@ALIAS@" ON TRUE',
            )
        leaf_sql, deepest, multi_match = self._join_path(
            root,
            resolved,
            modifier,
            self._register_join,
        )
        multi_match = multi_match or collection is not None

        # Multi-match subquery: the same joins with their own aliases.
        mm: _MultiMatch | None = None
        if multi_match:
            clauses: list[str] = []

            def make(
                _key: tuple[Any, ...],
                _parent: str | None,
                template: str,
            ) -> str:
                alias = self._next_alias("__pbm")
                clauses.append(template.replace("@ALIAS@", alias))
                return alias

            mm_root = None
            if collection is not None:
                mm_root = make(
                    ("collection",),
                    None,
                    f'LEFT JOIN "{collection[0]}" AS "@ALIAS@" ON TRUE',
                )
            mm_leaf, _deepest, _multi = self._join_path(mm_root, resolved, modifier, make)
            mm = _MultiMatch(
                from_sql='(SELECT 1) AS "__pbm_row" ' + " ".join(clauses),
                value=mm_leaf,
            )

        return _PathOperand(
            leaf_sql,
            self._alias_closure(deepest),
            mm,
            field_type=resolved.field_type,
            is_json=resolved.is_json,
        )

    def _side(self, node: tuple, path: _PathOperand | None) -> _Side:
        """Classify an operand like ``resolveEqualExpr`` (filter.go l.290-401)."""
        if path is not None:
            return _Side(path.sql, "column", ("sql_ref", path.sql), is_json=path.is_json)
        sql = self._operand_to_sql(node)
        kind = node[0] if isinstance(node, tuple) and node else ""
        if kind == "null" or sql == "NULL":
            return _Side(sql, "empty", node)
        if self._is_bound_param(sql):
            value = self._params[sql[1:]]
            return _Side(sql, "empty" if value in ("", None) else "value", node)
        if kind in {"literal", "macro"}:
            return _Side(sql, "value", node)
        return _Side(sql, "column", node)

    def _pb_equal_sql(self, left: _Side, right: _Side, *, equal: bool) -> str:
        """``=``/``!=`` like PocketBase ``resolveEqualExpr``.

        ``!=`` is NULL-safe (``IS NOT``); an empty operand also matches NULL;
        otherwise both sides are compared with ``COALESCE(x, '')``
        (v0.22.34 tools/search/filter.go l.290-368).
        """
        compare = "=" if equal else "IS DISTINCT FROM"
        concat = "OR" if equal else "AND"
        null_check = "IS NULL" if equal else "IS NOT NULL"
        left_empty = left.kind == "empty"
        right_empty = right.kind == "empty"
        if left_empty and right_empty:
            return "TRUE" if equal else "FALSE"
        if left.kind == "value" or right.kind == "value":
            left_sql = "''" if left_empty else left.sql
            right_sql = "''" if right_empty else right.sql
            return f"{left_sql} {compare} {right_sql}"
        if left_empty:
            return (
                f"({self._text_cast_sql(right.sql)} {compare} '' "
                f"{concat} {right.sql} {null_check})"
            )
        if right_empty:
            return (
                f"({self._text_cast_sql(left.sql)} {compare} '' "
                f"{concat} {left.sql} {null_check})"
            )
        return (
            f"COALESCE({self._text_cast_sql(left.sql)}, '') {compare} "
            f"COALESCE({self._text_cast_sql(right.sql)}, '')"
        )

    def _compare_sides(self, left: _Side, op: str, right: _Side) -> str:
        """One comparison of a path leaf (``?`` only disables multi-match)."""
        if left.is_json or right.is_json:
            # JSON leaves keep PPBase's JSON comparison.
            ref, other, ref_is_left = (left, right, True) if left.is_json else (
                right,
                left,
                False,
            )
            return self._operator_condition_sql(
                ref.sql,
                op,
                other.node,
                other.sql,
                ref_is_left=ref_is_left,
            )
        base_op = op.removeprefix("?")
        if base_op in {"=", "!="}:
            return self._pb_equal_sql(left, right, equal=base_op == "=")
        if base_op in _LIKE_OPS:
            return self._like_comparison_sql(
                left.sql,
                right.node,
                right.sql,
                _LIKE_OPS[base_op],
            )
        sql_op = _STANDARD_OPS.get(base_op, "=")
        return f"{left.sql} {sql_op} {right.sql}"

    @staticmethod
    def _multi_match_failure(condition: str, op: str, values: list[str]) -> str:
        """Rows that fail a multi-match (v0.22.34 filter.go l.680-706).

        ``NOT (condition)``; except for ``=``, a NULL value (missing target,
        empty list or empty collection) also fails.  PocketBase v0.24.0
        dropped this NULL clause (v0.40.6 filter.go l.708-711).
        """
        failing = f"NOT ({condition})"
        if op == "=":
            return failing
        return "(" + " OR ".join([failing, *(f"{v} IS NULL" for v in values)]) + ")"

    def _many_vs_one_sql(
        self,
        mm: _MultiMatch,
        path_side: _Side,
        op: str,
        other: _Side,
        *,
        path_is_left: bool,
    ) -> str:
        alias = self._next_alias("__pbsm")
        value = f'"{alias}"."v"'
        value_side = _Side(value, "column", ("sql_ref", value), is_json=path_side.is_json)
        condition = (
            self._compare_sides(value_side, op, other)
            if path_is_left
            else self._compare_sides(other, op, value_side)
        )
        return (
            f'NOT EXISTS (SELECT 1 FROM (SELECT {mm.value} AS "v" FROM {mm.from_sql}) '
            f'AS "{alias}" WHERE {self._multi_match_failure(condition, op, [value])})'
        )

    def _many_vs_many_sql(
        self,
        left_mm: _MultiMatch,
        left: _Side,
        op: str,
        right_mm: _MultiMatch,
        right: _Side,
    ) -> str:
        left_alias = self._next_alias("__pbml")
        right_alias = self._next_alias("__pbmr")
        left_value = f'"{left_alias}"."v"'
        right_value = f'"{right_alias}"."v"'
        condition = self._compare_sides(
            _Side(left_value, "column", ("sql_ref", left_value), is_json=left.is_json),
            op,
            _Side(right_value, "column", ("sql_ref", right_value), is_json=right.is_json),
        )
        failure = self._multi_match_failure(condition, op, [left_value, right_value])
        return (
            f'NOT EXISTS (SELECT 1 FROM (SELECT {left_mm.value} AS "v" '
            f'FROM {left_mm.from_sql}) AS "{left_alias}" '
            f'LEFT JOIN (SELECT {right_mm.value} AS "v" FROM {right_mm.from_sql}) '
            f'AS "{right_alias}" ON TRUE WHERE {failure})'
        )

    def _path_comparison(self, left: tuple, op: str, right: tuple) -> _Cond:
        """Compare relation/``@collection`` paths like PocketBase.

        The leaf is read through the shared LEFT JOINs (``?`` operators are
        any-match); other operators also need every row of PocketBase's
        multi-match subquery to match (v0.22.34 tools/search/filter.go
        l.183-215, l.569-706).
        """
        left_path = self._path_operand(left) if self._is_path_node(left) else None
        right_path = self._path_operand(right) if self._is_path_node(right) else None
        if (
            left_path is not None
            and right_path is None
            and left_path.field_type in _DATETIME_FIELD_TYPES
        ):
            right = self._literal_for_field_type(right, left_path.field_type)
        if (
            right_path is not None
            and left_path is None
            and right_path.field_type in _DATETIME_FIELD_TYPES
        ):
            left = self._literal_for_field_type(left, right_path.field_type)

        left_side = self._side(left, left_path)
        right_side = self._side(right, right_path)
        sql = self._compare_sides(left_side, op, right_side)

        left_mm = left_path.multi_match if left_path is not None else None
        right_mm = right_path.multi_match if right_path is not None else None
        if not op.startswith("?"):
            if left_mm is not None and right_mm is not None:
                sql = (
                    f"({sql} AND "
                    f"{self._many_vs_many_sql(left_mm, left_side, op, right_mm, right_side)})"
                )
            elif left_mm is not None:
                mm_sql = self._many_vs_one_sql(
                    left_mm, left_side, op, right_side, path_is_left=True
                )
                sql = f"({sql} AND {mm_sql})"
            elif right_mm is not None:
                mm_sql = self._many_vs_one_sql(
                    right_mm, right_side, op, left_side, path_is_left=False
                )
                sql = f"({sql} AND {mm_sql})"

        aliases: frozenset[str] = frozenset()
        for path in (left_path, right_path):
            if path is not None:
                aliases |= path.aliases
        return _Cond.atom(sql, aliases)

    def _operator_condition_sql(
        self,
        ref_sql: str,
        op: str,
        other: tuple,
        other_sql: str,
        *,
        ref_is_left: bool,
    ) -> str:
        """Compare a resolved path leaf with the other operand."""
        if op in _STANDARD_OPS:
            sql_op = _STANDARD_OPS[op]
            if other[0] == "null":
                if sql_op == "=":
                    return f"{ref_sql} IS NULL"
                if sql_op == "!=":
                    return f"{ref_sql} IS NOT NULL"
                return f"{ref_sql} {sql_op} NULL"
            if ref_is_left:
                return f"{ref_sql} {sql_op} {other_sql}"
            return f"{other_sql} {sql_op} {ref_sql}"
        if op in _LIKE_OPS:
            sql_op = _LIKE_OPS[op]
            if ref_is_left:
                return self._like_comparison_sql(ref_sql, other, other_sql, sql_op)
            return self._like_comparison_sql(
                other_sql,
                ("sql_ref", ref_sql),
                ref_sql,
                sql_op,
            )
        if op in _ANY_STANDARD_OPS:
            sql_op = _ANY_STANDARD_OPS[op]
            if ref_is_left:
                return self._scalar_or_array_any_sql(ref_sql, other_sql, sql_op)
            return self._scalar_or_array_any_sql(other_sql, ref_sql, sql_op)
        if op in _ANY_LIKE_OPS:
            sql_op = _ANY_LIKE_OPS[op]
            if ref_is_left:
                pattern_sql = self._like_pattern_sql(other, other_sql)
                return self._scalar_or_array_like_sql(ref_sql, pattern_sql, sql_op)
            pattern_sql = self._like_pattern_sql(("sql_ref", ref_sql), ref_sql)
            return self._scalar_or_array_like_sql(other_sql, pattern_sql, sql_op)
        if ref_is_left:
            return f"{ref_sql} = {other_sql}"
        return f"{other_sql} = {ref_sql}"

    # -- :each modifier helpers ---------------------------------------------

    def _extract_request_data_field_value(
        self,
        field_name: str,
    ) -> tuple[bool, Any]:
        """Resolve a field from ``request_context.data`` with leaf fallback."""
        data = self._request_context.get("data", {}) if isinstance(
            self._request_context, dict
        ) else {}
        if not isinstance(data, dict):
            return (False, None)
        if field_name in data:
            return (True, data.get(field_name))
        leaf = field_name.split(".")[-1]
        if leaf in data:
            return (True, data.get(leaf))
        return (False, None)

    def _request_data_field_is_multiple(self, field_name: str) -> bool:
        """Multiplicity of a ``@request.data.<field>`` schema field.

        PocketBase applies its all-items multi-match to ``:each`` only when the
        schema field is multiple (v0.22.34 resolvers/record_field_resolve_runner.go
        l.243-286).  Unknown fields keep PPBase's historical all-items reading.
        """
        info = self._field_info_for_table(self._current_table, field_name)
        if info is None:
            return True
        return self._info_is_multiple(info)

    def _extract_each_operand(self, node: tuple[str, Any]) -> tuple[str, bool] | None:
        """Return ``(text[] SQL, multiple)`` for an ``:each`` operand."""
        if not isinstance(node, tuple) or len(node) < 2:
            return None

        # Collection field modifier: field:each
        if node[0] == "field_modifier" and len(node) >= 3 and node[2] == "each":
            field_name = str(node[1])
            field_sql = self._field_expr(field_name)
            info = (
                self._field_info_for_table(self._current_table, field_name)
                if "." not in field_name
                else None
            )
            if info is not None and info.get("type") in _ARRAYABLE_FIELD_TYPES:
                multiple = self._info_is_multiple(info)
                return (self._each_elements_sql(field_sql, multiple=multiple), multiple)
            # Unknown metadata: historical text[] reading.
            return (f"COALESCE({field_sql}::text[], ARRAY[]::text[])", True)

        # Request body/data modifier: @request.body.field:each
        if node[0] == "macro":
            name = str(node[1]).lstrip("@")
            if name.startswith("request.data.") or name.startswith("request.body."):
                prefix = "request.body." if name.startswith("request.body.") else "request.data."
                field_and_modifier = name[len(prefix):]
                field_name, modifier = self._split_modifier(field_and_modifier)
                if modifier != "each":
                    return None

                has_field, raw_val = self._extract_request_data_field_value(field_name)
                # PocketBase iterates list.ToUniqueStringSlice(data[field]).
                values = to_unique_string_list(raw_val) if has_field else []
                pname = self._next_param(values)
                return (
                    f"COALESCE(CAST(:{pname} AS text[]), ARRAY[]::text[])",
                    self._request_data_field_is_multiple(field_name),
                )

        return None

    def _bound_value_is_empty(self, sql_expr: str) -> bool:
        """Return whether a bare ``:param`` operand is bound to ``''``/``None``."""
        if not (sql_expr.startswith(":") and sql_expr[1:].isidentifier()):
            return False
        name = sql_expr[1:]
        return name in self._params and self._params[name] in (None, "")

    def _each_item_condition(
        self,
        item_sql: str,
        op: str,
        other: tuple[str, Any],
        *,
        each_is_left: bool,
    ) -> str:
        """Compare one ``:each`` element like PocketBase compares a value.

        ``=``/``!=`` follow ``tools/search/filter.go`` ``resolveEqualExpr``
        (v0.22.34 l.290-368): an empty operand also matches NULL, ``!=`` is
        NULL-safe (``IS NOT``).  The ``?`` prefix only selects any-match.
        """
        base_op = op.removeprefix("?")
        other_sql = self._operand_to_sql(other)

        if base_op in _STANDARD_OPS:
            sql_op = _STANDARD_OPS[base_op]
            empty = other[0] == "null" or self._bound_value_is_empty(other_sql)
            if sql_op == "=":
                if empty:
                    return f"({item_sql} = '' OR {item_sql} IS NULL)"
                return f"{item_sql} = {self._text_cast_sql(other_sql)}"
            if sql_op == "!=":
                if empty:
                    return f"({item_sql} IS DISTINCT FROM '' AND {item_sql} IS NOT NULL)"
                return f"{item_sql} IS DISTINCT FROM {self._text_cast_sql(other_sql)}"
            if other[0] == "null":
                return f"{item_sql} {sql_op} NULL"
            if each_is_left:
                return f"{item_sql} {sql_op} {other_sql}"
            return f"{other_sql} {sql_op} {item_sql}"

        if base_op in _LIKE_OPS:
            sql_op = _LIKE_OPS[base_op]
            if each_is_left:
                return self._like_comparison_sql(item_sql, other, other_sql, sql_op)
            return self._like_comparison_sql(
                other_sql,
                ("sql_ref", item_sql),
                item_sql,
                sql_op,
            )

        # Fallback to "=" semantics for unsupported operator combos.
        if each_is_left:
            return f"{item_sql} = {other_sql}"
        return f"{other_sql} = {item_sql}"

    def _each_condition_sql(
        self,
        elements_sql: str,
        op: str,
        other: tuple[str, Any],
        *,
        each_is_left: bool,
        multiple: bool,
    ) -> str:
        """Build PocketBase ``:each`` semantics over a ``text[]`` expression.

        PocketBase LEFT JOINs ``json_each(field)`` (an empty array yields one
        NULL row) and, for a multiple field with a non-``?`` operator, adds a
        multi-match subquery that no element may fail (v0.22.34
        resolvers/record_field_resolve_runner.go l.418-452 and
        tools/search/filter.go l.183-215, l.630-706; v0.40.6
        core/record_field_resolver_runner.go l.776-809).  Result: ``?``
        operators match when any element matches; other operators need a
        non-empty array whose every element matches.
        """
        elements = (
            f"unnest(CASE WHEN COALESCE(cardinality({elements_sql}), 0) > 0 "
            f"THEN {elements_sql} ELSE ARRAY[NULL]::text[] END) AS _elem(value)"
        )
        item_sql = "_elem.value"
        condition = self._each_item_condition(
            item_sql,
            op,
            other,
            each_is_left=each_is_left,
        )
        any_match = f"EXISTS (SELECT 1 FROM {elements} WHERE {condition})"
        if op.startswith("?") or not multiple:
            return any_match
        if op == "=":
            failing = f"NOT ({condition})"
        else:
            failing = f"(NOT ({condition}) OR {item_sql} IS NULL)"
        return f"({any_match} AND NOT EXISTS (SELECT 1 FROM {elements} WHERE {failing}))"

    def _each_comparison(
        self,
        left: tuple[str, Any],
        op: str,
        right: tuple[str, Any],
        left_each: tuple[str, bool] | None,
        right_each: tuple[str, bool] | None,
    ) -> str:
        """Handle comparisons where at least one side uses ``:each``."""
        if left_each and right_each:
            raise ValueError("Comparisons with :each on both sides are not supported.")

        if left_each:
            return self._each_condition_sql(
                left_each[0],
                op,
                right,
                each_is_left=True,
                multiple=left_each[1],
            )
        assert right_each is not None
        return self._each_condition_sql(
            right_each[0],
            op,
            left,
            each_is_left=False,
            multiple=right_each[1],
        )

    # -- Path leaves -----------------------------------------------------------

    def _json_text_sql(self, column_sql: str) -> str:
        """JSON text of a multi-value column, compared like a plain scalar.

        PocketBase stores the compact JSON array and compares that text
        (``array_to_json`` renders the same compact JSON for text arrays).
        """
        json_text = f"array_to_json({column_sql})::text"
        self._json_text_operands.add(json_text)
        return json_text

    @staticmethod
    def _array_length_sql(column_sql: str, *, multiple: bool) -> str:
        """``:length`` of a select/file/relation value.

        PocketBase counts the JSON array items and counts a single value as one
        item (``''``/NULL as zero): v0.22.34 tools/dbutils/json.go l.17-28,
        v0.40.6 tools/dbutils/json.go l.19-32.
        """
        if multiple:
            return f"COALESCE(cardinality({column_sql}), 0)"
        return f"(CASE WHEN COALESCE({column_sql}::text, '') = '' THEN 0 ELSE 1 END)"

    @staticmethod
    def _each_elements_sql(column_sql: str, *, multiple: bool) -> str:
        """``text[]`` iterated by ``:each``: the array, or the single value."""
        if multiple:
            return f"COALESCE(CAST({column_sql} AS text[]), ARRAY[]::text[])"
        return f"ARRAY[CAST({column_sql} AS text)]"

    def _leaf_value_sql(self, leaf: _LeafOperand) -> str:
        """Render a path leaf (default, ``:length`` or ``:lower``) as SQL."""
        if leaf.modifier == "length" and leaf.field_type in _ARRAYABLE_FIELD_TYPES:
            return self._array_length_sql(leaf.sql, multiple=leaf.multiple)
        sql = self._json_text_sql(leaf.sql) if leaf.multiple else leaf.sql
        if leaf.modifier == "lower":
            return f"LOWER(COALESCE({sql}::text, ''))"
        return sql

    # -- Logical operators ---------------------------------------------------

    def or_expr(self, items: list) -> _Cond:
        return _Cond.combine("or", items)

    def and_expr(self, items: list) -> _Cond:
        return _Cond.combine("and", items)

    # -- Rendering -------------------------------------------------------------

    def render(self, node: Any, bound: frozenset[str] = frozenset()) -> str:
        """Render a condition tree; join aliases become correlated EXISTS.

        PocketBase evaluates the whole filter on each LEFT JOIN row of the
        main query.  An EXISTS over those joins distributes over ``||``; an
        ``&&`` keeps the operands that share a join in one EXISTS so that
        they read the same row, exactly like PocketBase's shared aliases.
        """
        node = _Cond.of(node)
        if node.kind == "atom":
            free = node.aliases - bound
            return self._exists_sql(free, node.sql) if free else node.sql
        if node.kind == "or":
            parts = [self.render(child, bound) for child in node.children]
            return "(" + " OR ".join(parts) + ")"

        parts: list[str] = []
        groups: list[tuple[set[str], list[int]]] = []
        for index, child in enumerate(node.children):
            free = set(child.aliases - bound)
            if not free:
                continue
            members = [index]
            kept: list[tuple[set[str], list[int]]] = []
            for aliases, indexes in groups:
                if aliases & free:
                    free |= aliases
                    members.extend(indexes)
                else:
                    kept.append((aliases, indexes))
            groups = [*kept, (free, sorted(members))]
        grouped = {index for _aliases, indexes in groups for index in indexes}
        for index, child in enumerate(node.children):
            if index not in grouped:
                parts.append(self.render(child, bound))
        for aliases, indexes in sorted(groups, key=lambda group: group[1][0]):
            inner_bound = bound | frozenset(aliases)
            inner = " AND ".join(
                self.render(node.children[index], inner_bound) for index in indexes
            )
            parts.append(self._exists_sql(frozenset(aliases), inner))
        return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"

    def _exists_sql(self, aliases: frozenset[str] | set[str], condition: str) -> str:
        joins = " ".join(
            join.sql for alias, join in self._joins.items() if alias in aliases
        )
        return (
            f'EXISTS (SELECT 1 FROM (SELECT 1) AS "__pb_row" {joins} '
            f"WHERE {condition})"
        )

    # -- Helpers -------------------------------------------------------------

    def _operand_to_sql(self, node: tuple[str, Any]) -> str:
        kind = node[0]
        value = node[1] if len(node) > 1 else None
        if kind == "literal":
            if isinstance(value, bool):
                return "TRUE" if value else "FALSE"
            if isinstance(value, (int, float)):
                return str(value)
            pname = self._next_param(value)
            return f":{pname}"
        if kind == "null":
            return "NULL"
        if kind == "field":
            column_sql = self._field_expr(value)
            if "." not in str(value) and self._is_multi_value_field(
                self._current_table,
                str(value),
            ):
                # PocketBase compares a direct multi-value field through its
                # stored JSON text (no json_each multi-match, v0.22.34
                # resolvers/record_field_resolve_runner.go l.454-476, v0.40.6
                # core/record_field_resolver_runner.go l.811-852).
                return self._json_text_sql(column_sql)
            return column_sql
        if kind == "field_modifier":
            field_name = value
            modifier = node[2]
            return self._resolve_field_modifier(field_name, modifier)
        if kind == "sql_ref":
            return str(value)
        if kind == "macro":
            return self._resolve_macro(value)
        return str(value)

    def _split_modifier(self, raw_value: str) -> tuple[str, str | None]:
        """Split optional modifier suffix from an operand token."""
        if ":" not in raw_value:
            return raw_value, None
        base, suffix = raw_value.rsplit(":", 1)
        if base and suffix in _MODIFIER_NAMES:
            return base, suffix
        return raw_value, None

    def _field_expr(self, field_name: str) -> str:
        """Build a SQL expression for a field reference."""
        safe = field_name
        for ch in safe:
            if ch not in _SAFE_IDENT_CHARS and ch != ".":
                raise ValueError(f"Invalid character in field name: {ch!r}")
        if "." in safe:
            return safe
        if self._current_table:
            return self._qualified_column(self._current_table, safe)
        return f'"{safe}"'

    @staticmethod
    def _request_value(value: Any) -> Any:
        """Bind a ``@request.*`` value like PocketBase.

        Plain values (strings, numbers, booleans, ``None``) are bound as they
        are; a list or an object is bound as its JSON text, so it compares
        like the stored JSON of a multi-value field and never multi-matches
        (v0.22.34 resolvers/record_field_resolver.go l.203-215, v0.40.6
        core/record_field_resolver.go l.350-361).
        """
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            return go_json_text(value)
        return value

    def _value_length(self, value: Any) -> int:
        """Compute a PocketBase-like length for request payload values."""
        if value is None:
            return 0
        if isinstance(value, (str, bytes, bytearray, list, tuple, set, dict)):
            return len(value)
        return 1

    def _resolve_field_modifier(self, field_name: str, modifier: str) -> str:
        """Resolve a ``field:modifier`` operand to SQL."""
        field_sql = self._field_expr(field_name)
        info = (
            self._field_info_for_table(self._current_table, field_name)
            if "." not in field_name
            else None
        )
        if modifier == "isset":
            return f"({field_sql} IS NOT NULL)"
        if (
            modifier == "length"
            and info is not None
            and info.get("type") in _ARRAYABLE_FIELD_TYPES
        ):
            return self._array_length_sql(
                field_sql,
                multiple=self._info_is_multiple(info),
            )
        if modifier == "lower" and self._info_is_multiple(info):
            return f"LOWER({self._json_text_sql(field_sql)})"
        if modifier == "length":
            return (
                f"(CASE "
                f"WHEN {field_sql} IS NULL THEN 0 "
                f"WHEN jsonb_typeof(to_jsonb({field_sql})) = 'array' "
                f"THEN jsonb_array_length(to_jsonb({field_sql})) "
                f"ELSE char_length({field_sql}::text) END)"
            )
        if modifier == "changed":
            # Mirrors @request.body.field:changed behavior for convenience.
            data = self._request_context.get("data", {}) if isinstance(
                self._request_context, dict
            ) else {}
            raw_val = None
            has_field = False
            if isinstance(data, dict):
                if field_name in data:
                    has_field = True
                    raw_val = data.get(field_name)
                else:
                    leaf = field_name.split(".")[-1]
                    if leaf in data:
                        has_field = True
                        raw_val = data.get(leaf)
            if not has_field:
                return "FALSE"
            pname = self._next_param(raw_val)
            return (
                f"(COALESCE({field_sql}::text, '') "
                f"<> COALESCE(CAST(:{pname} AS TEXT), ''))"
            )
        if modifier == "lower":
            return f"LOWER(COALESCE({field_sql}::text, ''))"
        if modifier == "each":
            raise ValueError(
                "The :each modifier can only be used as part of a comparison expression."
            )
        raise ValueError(f"Unknown field modifier: {modifier}")

    def _resolve_macro(self, macro_name: str) -> str:
        name = macro_name.lstrip("@")
        if name == "now":
            return "(TIMEZONE('UTC', NOW()) AT TIME ZONE 'UTC')"
        if name == "second":
            return "CAST(EXTRACT(SECOND FROM TIMEZONE('UTC', NOW())) AS INT)"
        if name == "minute":
            return "CAST(EXTRACT(MINUTE FROM TIMEZONE('UTC', NOW())) AS INT)"
        if name == "hour":
            return "CAST(EXTRACT(HOUR FROM TIMEZONE('UTC', NOW())) AS INT)"
        if name == "weekday":
            return "CAST(EXTRACT(DOW FROM TIMEZONE('UTC', NOW())) AS INT)"
        if name == "day":
            return "CAST(EXTRACT(DAY FROM TIMEZONE('UTC', NOW())) AS INT)"
        if name == "month":
            return "CAST(EXTRACT(MONTH FROM TIMEZONE('UTC', NOW())) AS INT)"
        if name == "year":
            return "CAST(EXTRACT(YEAR FROM TIMEZONE('UTC', NOW())) AS INT)"
        if name == "yesterday":
            return "((TIMEZONE('UTC', NOW()) - INTERVAL '1 day') AT TIME ZONE 'UTC')"
        if name == "tomorrow":
            return "((TIMEZONE('UTC', NOW()) + INTERVAL '1 day') AT TIME ZONE 'UTC')"
        if name == "todayStart":
            return "(date_trunc('day', TIMEZONE('UTC', NOW())) AT TIME ZONE 'UTC')"
        if name == "todayEnd":
            return (
                "((date_trunc('day', TIMEZONE('UTC', NOW())) + INTERVAL '1 day' "
                "- INTERVAL '1 microsecond') AT TIME ZONE 'UTC')"
            )
        if name == "monthStart":
            return "(date_trunc('month', TIMEZONE('UTC', NOW())) AT TIME ZONE 'UTC')"
        if name == "monthEnd":
            return (
                "((date_trunc('month', TIMEZONE('UTC', NOW())) + INTERVAL '1 month' "
                "- INTERVAL '1 microsecond') AT TIME ZONE 'UTC')"
            )
        if name == "yearStart":
            return "(date_trunc('year', TIMEZONE('UTC', NOW())) AT TIME ZONE 'UTC')"
        if name == "yearEnd":
            return (
                "((date_trunc('year', TIMEZONE('UTC', NOW())) + INTERVAL '1 year' "
                "- INTERVAL '1 microsecond') AT TIME ZONE 'UTC')"
            )
        if name == "request.context":
            context = self._request_context.get("context", "")
            pname = self._next_param(context)
            return f":{pname}"
        if name == "request.method":
            method = self._request_context.get("method", "")
            pname = self._next_param(method)
            return f":{pname}"
        if name.startswith("request.headers."):
            field = name[len("request.headers."):]
            headers = self._request_context.get("headers", {})
            val = ""
            if isinstance(headers, dict):
                normalized = field.lower().replace("-", "_")
                # Header macros can use either header-name or header_name style.
                val = (
                    headers.get(normalized)
                    or headers.get(field.lower())
                    or headers.get(field)
                    or ""
                )
            pname = self._next_param(val)
            return f":{pname}"
        if name.startswith("request.auth."):
            field = name[len("request.auth."):]
            auth = self._request_context.get("auth", {})
            if field == "id":
                val = auth.get("id", "")
            elif field == "collectionId":
                val = auth.get("collectionId", "")
            elif field == "collectionName":
                val = auth.get("collectionName", "")
            else:
                val = auth.get(field, "")
            pname = self._next_param(self._request_value(val))
            return f":{pname}"
        if name.startswith("request.data.") or name.startswith("request.body."):
            # PocketBase v0.22: @request.data.*  /  v0.23+: @request.body.*
            prefix = "request.body." if name.startswith("request.body.") else "request.data."
            field_and_modifier = name[len(prefix):]
            field, modifier = self._split_modifier(field_and_modifier)
            data = self._request_context.get("data", {})
            has_field = isinstance(data, dict) and field in data
            val = data.get(field) if isinstance(data, dict) else None

            if modifier == "isset":
                pname = self._next_param(has_field)
                return f":{pname}"
            if modifier == "length":
                info = self._field_info_for_table(self._current_table, field)
                if info is not None and info.get("type") in _ARRAYABLE_FIELD_TYPES:
                    # PocketBase: len(list.ToUniqueStringSlice(data[field]))
                    # (v0.22.34 resolvers/record_field_resolve_runner.go
                    # l.233-241).
                    length = len(to_unique_string_list(val)) if has_field else 0
                else:
                    length = self._value_length(val) if has_field else 0
                pname = self._next_param(length)
                return f":{pname}"
            if modifier == "changed":
                if not has_field:
                    return "FALSE"
                safe_field = _sanitize_ident(field)
                field_sql = f'"{safe_field}"'
                pname = self._next_param(val)
                return (
                    f"(COALESCE({field_sql}::text, '') "
                    f"<> COALESCE(CAST(:{pname} AS TEXT), ''))"
                )
            if modifier == "each":
                values = to_unique_string_list(val) if has_field else []
                pname = self._next_param(values)
                return f"CAST(:{pname} AS text[])"
            if modifier == "lower":
                lowered = ""
                if has_field and val is not None:
                    lowered = str(val).lower()
                pname = self._next_param(lowered)
                return f":{pname}"

            pname = self._next_param(self._request_value(val) if has_field else "")
            return f":{pname}"
        if name.startswith("request.query."):
            field = name[len("request.query."):]
            query = self._request_context.get("query", {})
            val = query.get(field, "")
            pname = self._next_param(val)
            return f":{pname}"
        # Fallback: treat unknown macros as empty string
        pname = self._next_param("")
        return f":{pname}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_filter(
    filter_str: str,
    request_context: dict[str, Any] | None = None,
    relation_resolver: dict[str, dict[str, Any]] | None = None,
    collection_resolver: dict[str, dict[str, Any]] | None = None,
    current_table: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Parse a PocketBase filter expression into a SQL WHERE clause.

    Args:
        filter_str: The PocketBase filter string.
        request_context: Optional dict with ``context``, ``method``,
            ``headers``, ``auth``, ``data``, ``query`` keys
            for resolving ``@request.*`` macros.
        relation_resolver: Optional recursive mapping of relation field names.
            When provided, dotted field paths like ``author.company.name`` are
            resolved as nested relation traversals and generate ``EXISTS``
            subqueries.
        collection_resolver: Optional mapping used to resolve relation paths in
            ``@collection.collection.relation.field`` references.
        current_table: Optional current table name used to qualify outer field
            references inside correlated ``@collection`` subqueries.

    Returns:
        A tuple of ``(where_sql, params)`` where ``where_sql`` is a SQL
        fragment using ``:param_name`` placeholders and ``params`` is a dict
        of parameter values.

    Raises:
        ValueError: If the filter string has invalid syntax.
    """
    if not filter_str or not filter_str.strip():
        return ("1=1", {})

    try:
        tree = _parser.parse(filter_str)
    except Exception as exc:
        raise ValueError(f"Invalid filter syntax: {exc}") from exc

    transformer = _FilterTransformer(
        request_context,
        relation_resolver,
        collection_resolver,
        current_table,
    )
    try:
        where_sql = transformer.render(transformer.transform(tree))
    except VisitError as exc:
        # Invalid identifiers, modifiers or paths are client errors (400),
        # not server errors.
        if isinstance(exc.orig_exc, ValueError):
            raise exc.orig_exc from exc
        raise
    return (where_sql, transformer._params)


def parse_sort(sort_str: str) -> list[tuple[str, str]]:
    """Parse a PocketBase sort expression into column/direction pairs.

    Args:
        sort_str: Comma-separated sort expression, e.g. ``"-created,+title"``.

    Returns:
        List of ``(column_or_expression, "ASC"|"DESC")`` tuples.
        Special: ``@random`` becomes ``"RANDOM()"``.
    """
    if not sort_str or not sort_str.strip():
        return []

    result: list[tuple[str, str]] = []
    for part in sort_str.split(","):
        part = part.strip()
        if not part:
            continue

        if part == "@random":
            result.append(("RANDOM()", "ASC"))
            continue

        if part == "@rowid":
            result.append(("ctid", "ASC"))
            continue

        direction = "ASC"
        if part.startswith("-"):
            direction = "DESC"
            part = part[1:]
        elif part.startswith("+"):
            part = part[1:]

        # Sanitize field name
        for ch in part:
            if ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.":
                raise ValueError(f"Invalid character in sort field: {ch!r}")

        if "." in part:
            result.append((part, direction))
        else:
            result.append((f'"{part}"', direction))

    return result
