"""PocketBase filter syntax parser using Lark.

Translates PocketBase filter expressions into SQL WHERE clauses with
parameterized bindings.  Also provides ``parse_sort`` for sort expressions.

All literal values are passed as bound parameters -- never interpolated into
SQL strings -- to prevent SQL injection.
"""

from __future__ import annotations

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
# @collection condition — grouped into EXISTS subqueries
# ---------------------------------------------------------------------------


class _CollectionCondition:
    """A WHERE condition involving ``@collection.X.Y``.

    Multiple conditions referencing the **same** collection inside the same
    ``&&`` (AND) clause are grouped into a **single** ``EXISTS`` subquery so
    that all conditions apply to the same row.  In ``||`` (OR) clauses each
    condition gets its own ``EXISTS``.
    """

    __slots__ = ("table_name", "group_key", "inner_sql")

    def __init__(self, table_name: str, group_key: str, inner_sql: str) -> None:
        self.table_name = table_name
        self.group_key = group_key
        self.inner_sql = inner_sql


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

    @property
    def is_multiple(self) -> bool:
        if self.multiple is not None:
            return self.multiple
        return self.max_select > 1


@dataclass(frozen=True)
class _ResolvedPath:
    """Resolved SQL expression and traversal hops for a dotted field path."""

    steps: list[_TraversalStep]
    sql: str
    is_json: bool = False
    field_type: str = ""
    # Whether the last segment is a multi-value field (shared rule).
    leaf_multiple: bool = False


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


class _CollectionRef:
    """Resolved ``@collection.X.path`` operand metadata."""

    __slots__ = (
        "table_name",
        "group_key",
        "column_sql",
        "relation_steps",
        "leaf_sql",
        "leaf_is_json",
        "leaf_type",
        "leaf_multiple",
        "modifier",
    )

    def __init__(
        self,
        table_name: str,
        group_key: str,
        *,
        column_sql: str | None = None,
        relation_steps: list[_TraversalStep] | None = None,
        leaf_sql: str | None = None,
        leaf_is_json: bool = False,
        leaf_type: str = "",
        leaf_multiple: bool = False,
        modifier: str | None = None,
    ) -> None:
        self.table_name = table_name
        self.group_key = group_key
        self.column_sql = column_sql
        self.relation_steps = relation_steps or []
        self.leaf_sql = leaf_sql
        self.leaf_is_json = leaf_is_json
        self.leaf_type = leaf_type
        self.leaf_multiple = leaf_multiple
        self.modifier = modifier


# ---------------------------------------------------------------------------
# Relation field traversal — e.g. ``author.name = "John"``
# ---------------------------------------------------------------------------


class _RelationCondition:
    """A WHERE condition involving a relation field traversal.

    ``author.name = "John"`` where ``author`` is a relation field pointing
    to the ``users`` collection generates::

        EXISTS (SELECT 1 FROM "users"
                WHERE "users"."id" = "author"          -- join_cond
                  AND "users"."name" = :param)          -- where_cond

    Multiple traversals on the **same** relation field inside an ``&&`` are
    grouped into a single ``EXISTS`` (same as ``_CollectionCondition``).
    """

    __slots__ = ("relation_key", "target_table", "join_cond", "where_cond")

    def __init__(
        self,
        relation_key: str,   # field name used for grouping (e.g. "author")
        target_table: str,    # resolved table name (e.g. "users")
        join_cond: str,       # e.g. '"users"."id" = "author"'
        where_cond: str,      # e.g. '"users"."name" = :param'
    ) -> None:
        self.relation_key = relation_key
        self.target_table = target_table
        self.join_cond = join_cond
        self.where_cond = where_cond


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
        if kind == "relation_ref" and len(node) >= 5:
            return str(node[4] or "")
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
        optimize_relation_id: bool = False,
        context: str = "field path",
    ) -> _ResolvedPath | None:
        """Resolve a PocketBase dotted path into SQL and traversal steps.

        This mirrors PocketBase's segment-by-segment resolver: each segment can
        be a direct relation, a synthetic back-relation, or a JSON field that
        consumes the remaining segments as a JSON path.
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
                )

            info = (
                self._flat_relation_info(flat_resolver, current_table, part)
                if flat_resolver
                else current_relations.get(part) if isinstance(current_relations, dict) else None
            )
            if isinstance(info, dict) and "table" in info:
                step = self._step_from_info(part, info)

                # PocketBase optimizes single relation `.id` lookups by using
                # the stored relation id column instead of joining the target.
                if (
                    optimize_relation_id
                    and step.kind == "forward"
                    and not step.is_multiple
                    and idx == len(safe_parts) - 2
                    and safe_parts[idx + 1] == "id"
                ):
                    return _ResolvedPath(
                        steps,
                        self._column_expr_for_table(
                            current_table,
                            step.source_field or part,
                        ),
                        field_type="relation",
                    )

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
                optimize_relation_id=False,
            )
            if resolved is not None and resolved.steps:
                return (
                    "relation_ref",
                    resolved.steps,
                    resolved.sql,
                    resolved.is_json,
                    resolved.field_type,
                    _LeafOperand(
                        resolved.sql,
                        field_type=resolved.field_type,
                        multiple=resolved.leaf_multiple,
                        modifier=None if resolved.is_json else modifier,
                    ),
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
    ) -> str | _CollectionCondition | _RelationCondition:
        op = str(op_token).strip()
        if op in _STANDARD_OPS:
            left, right = self._coerce_comparison_literals(left, right)

        # ── Relation field traversal → generate _RelationCondition ─────
        left_is_rel = (
            isinstance(left, tuple)
            and len(left) >= 3
            and left[0] == "relation_ref"
        )
        right_is_rel = (
            isinstance(right, tuple)
            and len(right) >= 3
            and right[0] == "relation_ref"
        )
        if left_is_rel or right_is_rel:
            return self._relation_comparison(
                left, op, right, left_is_rel, right_is_rel,
            )

        # ── @collection references → generate _CollectionCondition ──────
        left_is_coll = (
            isinstance(left, tuple)
            and len(left) >= 4
            and left[0] == "collection_ref"
        )
        right_is_coll = (
            isinstance(right, tuple)
            and len(right) >= 4
            and right[0] == "collection_ref"
        )
        if left_is_coll or right_is_coll:
            return self._collection_comparison(
                left, op, right, left_is_coll, right_is_coll,
            )

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
        date_sql = _empty_date_condition(self, left, left_sql, op, right, right_sql)
        if date_sql is not None:
            return date_sql

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

    def _resolve_collection_ref(self, node: tuple) -> _CollectionRef:
        coll_name = _sanitize_ident(str(node[1]))
        field_path = tuple(_sanitize_ident(str(part)) for part in node[2])
        group_key = str(node[3])
        modifier = node[4] if len(node) > 4 else None

        if len(field_path) == 1:
            field_type = self._field_type_for_collection_resolver(
                coll_name,
                field_path[0],
            )
            return _CollectionRef(
                coll_name,
                group_key,
                column_sql=self._qualified_column(coll_name, field_path[0]),
                leaf_type=field_type,
                leaf_multiple=self._info_is_multiple(
                    self._collection_resolver_field_info(coll_name, field_path[0])
                ),
                modifier=modifier,
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
            optimize_relation_id=True,
            context="@collection relation traversal",
        )
        if resolved is None:
            raise ValueError(
                f"Invalid @collection relation traversal: "
                f"@collection.{coll_name}.{'.'.join(field_path)}."
            )

        leaf_modifier = None if resolved.is_json else modifier
        if not resolved.steps:
            return _CollectionRef(
                coll_name,
                group_key,
                column_sql=resolved.sql,
                leaf_type=resolved.field_type,
                leaf_multiple=resolved.leaf_multiple,
                modifier=leaf_modifier,
            )

        return _CollectionRef(
            coll_name,
            group_key,
            relation_steps=resolved.steps,
            leaf_sql=resolved.sql,
            leaf_is_json=resolved.is_json,
            leaf_type=resolved.field_type,
            leaf_multiple=resolved.leaf_multiple,
            modifier=leaf_modifier,
        )

    def _collection_relation_leaf_sql(self, ref: _CollectionRef) -> str:
        if not ref.relation_steps or not ref.leaf_sql:
            if ref.column_sql:
                return ref.column_sql
            raise ValueError("Invalid @collection relation reference.")
        return ref.leaf_sql

    def _join_condition_for_step(
        self,
        step: _TraversalStep,
        parent_table: str | None,
    ) -> str:
        if step.kind == "back":
            back_field = step.back_field or ""
            target_expr = self._qualified_column(step.target_table, back_field)
            parent_id = self._column_expr_for_table(parent_table, "id")
            if step.is_multiple:
                return self._scalar_or_array_any_sql(target_expr, parent_id, "=")
            return f"{target_expr} = {parent_id}"

        source_field = step.source_field or step.relation_key
        source_expr = self._column_expr_for_table(parent_table, source_field)
        target_id = self._qualified_column(step.target_table, "id")
        if step.is_multiple:
            return self._scalar_or_array_any_sql(source_expr, target_id, "=")
        return f"{target_id} = {source_expr}"

    def _wrap_collection_relation_exists(
        self,
        ref: _CollectionRef,
        leaf_condition: str,
    ) -> str:
        wrapped = leaf_condition
        for idx in range(len(ref.relation_steps) - 1, -1, -1):
            step = ref.relation_steps[idx]
            parent_table = ref.table_name if idx == 0 else ref.relation_steps[idx - 1].target_table
            join_cond = self._join_condition_for_step(step, parent_table)
            wrapped = (
                f'EXISTS (SELECT 1 FROM "{step.target_table}" '
                f'WHERE {join_cond} AND {wrapped})'
            )
        return wrapped

    def _collection_ref_leaf(self, ref: _CollectionRef) -> _LeafOperand:
        sql = (
            ref.column_sql
            if ref.column_sql is not None
            else self._collection_relation_leaf_sql(ref)
        )
        return _LeafOperand(
            sql,
            field_type=ref.leaf_type,
            multiple=ref.leaf_multiple,
            modifier=ref.modifier,
        )

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

    def _leaf_condition_sql(
        self,
        leaf: _LeafOperand,
        op: str,
        other: tuple,
        *,
        leaf_is_left: bool,
    ) -> str:
        """Compare the last segment of a relation/``@collection`` path."""
        if leaf.modifier == "each" and leaf.field_type in _ARRAYABLE_FIELD_TYPES:
            return self._each_condition_sql(
                self._each_elements_sql(leaf.sql, multiple=leaf.multiple),
                op,
                other,
                each_is_left=leaf_is_left,
                multiple=leaf.multiple,
            )
        leaf_sql = self._leaf_value_sql(leaf)
        other_sql = self._operand_to_sql(other)
        if leaf.modifier is None and not leaf.multiple:
            ref = ("sql_ref", leaf_sql, False, leaf.field_type)
            operands = (ref, leaf_sql, other, other_sql)
            if not leaf_is_left:
                operands = (other, other_sql, ref, leaf_sql)
            left, left_sql, right, right_sql = operands
            date_sql = _empty_date_condition(self, left, left_sql, op, right, right_sql)
            if date_sql is not None:
                return date_sql
        return self._operator_condition_sql(
            leaf_sql,
            op,
            other,
            other_sql,
            ref_is_left=leaf_is_left,
        )

    def _collection_ref_condition(
        self,
        ref: _CollectionRef,
        op: str,
        other: tuple,
        *,
        ref_is_left: bool,
    ) -> str:
        if ref.leaf_type in _DATETIME_FIELD_TYPES:
            other = self._literal_for_field_type(other, ref.leaf_type)
        condition = self._leaf_condition_sql(
            self._collection_ref_leaf(ref),
            op,
            other,
            leaf_is_left=ref_is_left,
        )
        if ref.column_sql is not None:
            return condition
        return self._wrap_collection_relation_exists(ref, condition)

    def _collection_comparison(
        self,
        left: tuple,
        op: str,
        right: tuple,
        left_is_coll: bool,
        right_is_coll: bool,
    ) -> _CollectionCondition | str:
        """Handle comparisons where at least one side is ``@collection.X.Y``."""

        # Both sides reference @collection -----------------------------------
        if left_is_coll and right_is_coll:
            l_ref = self._resolve_collection_ref(left)
            r_ref = self._resolve_collection_ref(right)
            if "each" in (l_ref.modifier, r_ref.modifier):
                raise ValueError(
                    "Comparisons between two @collection references cannot use :each."
                )
            l_col = self._leaf_value_sql(self._collection_ref_leaf(l_ref))
            r_col = self._leaf_value_sql(self._collection_ref_leaf(r_ref))
            sql_op = _STANDARD_OPS.get(op, "=")
            inner = f"{l_col} {sql_op} {r_col}"
            if l_ref.column_sql is None:
                inner = self._wrap_collection_relation_exists(l_ref, inner)
            if r_ref.column_sql is None:
                inner = self._wrap_collection_relation_exists(r_ref, inner)
            if l_ref.group_key == r_ref.group_key:
                return _CollectionCondition(l_ref.table_name, l_ref.group_key, inner)
            # Cross-collection → inline EXISTS with two tables
            return (
                f'EXISTS (SELECT 1 FROM "{l_ref.table_name}", "{r_ref.table_name}" '
                f"WHERE {inner})"
            )

        # One side is @collection -------------------------------------------
        if left_is_coll:
            ref = self._resolve_collection_ref(left)
            other = right
            coll_is_left = True
        else:
            ref = self._resolve_collection_ref(right)
            other = left
            coll_is_left = False

        condition = self._collection_ref_condition(
            ref,
            op,
            other,
            ref_is_left=coll_is_left,
        )
        return _CollectionCondition(ref.table_name, ref.group_key, condition)

    # -- Relation traversal comparison helpers --------------------------------

    def _relation_comparison(
        self,
        left: tuple,
        op: str,
        right: tuple,
        left_is_rel: bool,
        right_is_rel: bool,
    ) -> _RelationCondition | str:
        """Handle comparisons where at least one side is a relation traversal."""

        def _wrap_nested_relation_exists(
            steps: list[_TraversalStep],
            leaf_condition: str,
            root_parent_table: str | None = None,
        ) -> str:
            wrapped = leaf_condition
            for idx in range(len(steps) - 1, -1, -1):
                step = steps[idx]
                parent_table = (
                    root_parent_table
                    if idx == 0
                    else steps[idx - 1].target_table
                )
                join_cond = self._join_condition_for_step(step, parent_table)
                wrapped = (
                    f'EXISTS (SELECT 1 FROM "{step.target_table}" '
                    f'WHERE {join_cond} AND {wrapped})'
                )
            return wrapped

        def _relation_condition_for_steps(
            steps: list[_TraversalStep],
            leaf_condition: str,
        ) -> _RelationCondition:
            first = steps[0]
            first_join = self._join_condition_for_step(first, None)
            where_cond = (
                leaf_condition
                if len(steps) == 1
                else _wrap_nested_relation_exists(
                    steps[1:],
                    leaf_condition,
                    first.target_table,
                )
            )
            return _RelationCondition(
                first.relation_key,
                first.target_table,
                first_join,
                where_cond,
            )

        def _leaf_of(node: tuple) -> _LeafOperand:
            if len(node) >= 6 and isinstance(node[5], _LeafOperand):
                return node[5]
            return _LeafOperand(
                str(node[2]),
                field_type=str(node[4]) if len(node) >= 5 else "",
            )

        # Both sides are relation refs (rare) --------------------------------
        if left_is_rel and right_is_rel:
            l_steps, r_steps = left[1], right[1]
            if len(l_steps) > 1 or len(r_steps) > 1:
                raise ValueError(
                    "Comparisons between two nested relation traversals are not supported."
                )
            l_leaf, r_leaf = _leaf_of(left), _leaf_of(right)
            if "each" in (l_leaf.modifier, r_leaf.modifier):
                raise ValueError(
                    "Comparisons between two relation traversals cannot use :each."
                )
            l_col = self._leaf_value_sql(l_leaf)
            r_col = self._leaf_value_sql(r_leaf)

            l_step = l_steps[0]
            r_step = r_steps[0]
            l_join = self._join_condition_for_step(l_step, None)
            r_join = self._join_condition_for_step(r_step, None)
            sql_op = _STANDARD_OPS.get(op, "=")
            return (
                f'EXISTS (SELECT 1 FROM "{l_step.target_table}", "{r_step.target_table}" '
                f"WHERE {l_join} AND {r_join} AND {l_col} {sql_op} {r_col})"
            )

        # One side is a relation ref -----------------------------------------
        if left_is_rel:
            rel_steps, leaf = left[1], _leaf_of(left)
            other = right
            rel_is_left = True
        else:
            rel_steps, leaf = right[1], _leaf_of(right)
            other = left
            rel_is_left = False

        # PocketBase resolves the last segment like a direct field: a
        # multi-value leaf is its JSON text, ``:length``/``:each`` iterate it
        # (v0.22.34 resolvers/record_field_resolve_runner.go l.356-477,
        # v0.40.6 core/record_field_resolver_runner.go l.758-852).
        where_cond = self._leaf_condition_sql(
            leaf,
            op,
            other,
            leaf_is_left=rel_is_left,
        )
        return _relation_condition_for_steps(rel_steps, where_cond)

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

    def or_expr(self, items: list) -> str:
        """OR: each @collection / relation condition becomes its own EXISTS."""
        parts: list[str] = []
        for item in items:
            if isinstance(item, _CollectionCondition):
                parts.append(
                    f'EXISTS (SELECT 1 FROM "{item.table_name}" WHERE {item.inner_sql})'
                )
            elif isinstance(item, _RelationCondition):
                conds = " AND ".join([item.join_cond, item.where_cond])
                parts.append(
                    f'EXISTS (SELECT 1 FROM "{item.target_table}" WHERE {conds})'
                )
            else:
                parts.append(str(item))
        if len(parts) == 1:
            return parts[0]
        return "(" + " OR ".join(parts) + ")"

    def and_expr(self, items: list) -> str:
        """AND: @collection / relation conditions for the **same** target are
        merged into a single EXISTS so all conditions match the same row."""
        coll_groups: dict[str, list[str]] = {}
        coll_tables: dict[str, str] = {}
        rel_groups: dict[str, list[_RelationCondition]] = {}
        regular_parts: list[str] = []

        for item in items:
            if isinstance(item, _CollectionCondition):
                coll_groups.setdefault(item.group_key, []).append(item.inner_sql)
                coll_tables.setdefault(item.group_key, item.table_name)
            elif isinstance(item, _RelationCondition):
                rel_groups.setdefault(item.relation_key, []).append(item)
            else:
                regular_parts.append(str(item))

        # @collection groups
        for group_key, conditions in coll_groups.items():
            table_name = coll_tables[group_key]
            inner = " AND ".join(conditions)
            regular_parts.append(
                f'EXISTS (SELECT 1 FROM "{table_name}" WHERE {inner})'
            )

        # Relation traversal groups — join_cond is the same for all items
        # in the same group so we include it once.
        for _rel_key, conditions in rel_groups.items():
            table = conditions[0].target_table
            join = conditions[0].join_cond
            all_conds = [join] + [c.where_cond for c in conditions]
            regular_parts.append(
                f'EXISTS (SELECT 1 FROM "{table}" WHERE {" AND ".join(all_conds)})'
            )

        if len(regular_parts) == 1:
            return regular_parts[0]
        return "(" + " AND ".join(regular_parts) + ")"

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
        where_sql = transformer.transform(tree)
    except VisitError as exc:
        # Invalid identifiers, modifiers or paths are client errors (400),
        # not server errors.
        if isinstance(exc.orig_exc, ValueError):
            raise exc.orig_exc from exc
        raise
    return (str(where_sql), transformer._params)


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


# ---------------------------------------------------------------------------
# Empty dates
# ---------------------------------------------------------------------------
#
# PocketBase stores an empty date as '' (``TEXT DEFAULT '' NOT NULL``,
# v0.22.34 models/schema/schema_field.go l.163, v0.40.6 core/field_date.go
# l.109-110) and compares dates as SQLite text: ``=``/``!=`` treat '' and
# NULL alike (``resolveEqualExpr``, v0.22.34 tools/search/filter.go
# l.290-368, v0.40.6 l.340-422), while ``<``, ``<=``, ``>``, ``>=``, ``~``
# and ``!~`` compare the stored text directly (v0.22.34 l.155-176), so ''
# sorts before every date.  PPBase stores the empty date as NULL in a
# ``TIMESTAMPTZ NULL`` column; the SQL below gives that NULL the semantics
# of ''.  The ``null`` identifier stays SQL NULL for ``<``-like operators,
# as in PocketBase.

# Only ``date`` columns are nullable; ``autodate`` columns are NOT NULL.
_NULLABLE_DATE_FIELD_TYPES = {"date"}
_FLIPPED_STANDARD_OPS = {"=": "=", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


def _date_operand_kind(parser: _FilterTransformer, node: Any, sql: str) -> str:
    """Classify a comparison operand for the empty-date semantics.

    ``"date"`` is a nullable date column (NULL stands for ''), ``"empty"`` an
    empty string value, ``"null"`` the ``null`` identifier or a missing
    request value, ``"value"`` anything else (date literal, macro, column).
    """
    kind = node[0] if isinstance(node, tuple) and node else ""
    # A ``sql_ref`` whose third item is True is a JSON path, never a column.
    is_column = kind == "field" or (kind == "sql_ref" and node[2:3] != (True,))
    if is_column and parser._operand_field_type(node) in _NULLABLE_DATE_FIELD_TYPES:
        return "date"
    if kind == "null":
        return "null"
    if parser._is_bound_param(sql):
        value = parser._params[sql[1:]]
        if value is None:
            return "null"
        if value == "":
            return "empty"
    return "value"


def _pb_date_text_sql(sql: str) -> str:
    """The text PocketBase stores for a date: '' or ``YYYY-MM-DD HH:MM:SS.mmmZ``."""
    return (
        f"COALESCE(to_char({sql} AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD HH24:MI:SS.MS\"Z\"'), '')"
    )


def _empty_date_sql(date_sql: str, sql_op: str, other_sql: str, other_kind: str) -> str:
    """Compare a nullable date column like the SQLite text PocketBase stores."""
    d, o = date_sql, other_sql
    if other_kind == "date":
        return {
            "=": f"({d} IS NOT DISTINCT FROM {o})",
            "!=": f"({d} IS DISTINCT FROM {o})",
            "<": f"({d} < {o} OR ({d} IS NULL AND {o} IS NOT NULL))",
            "<=": f"({d} <= {o} OR {d} IS NULL)",
            ">": f"({d} > {o} OR ({d} IS NOT NULL AND {o} IS NULL))",
            ">=": f"({d} >= {o} OR {o} IS NULL)",
        }[sql_op]
    if other_kind == "empty":
        return {
            "=": f"{d} IS NULL",
            "!=": f"{d} IS NOT NULL",
            "<": "FALSE",
            "<=": f"{d} IS NULL",
            ">": f"{d} IS NOT NULL",
            ">=": "TRUE",
        }[sql_op]
    if other_kind == "null":
        if sql_op in {"=", "!="}:
            return f"{d} IS {'' if sql_op == '=' else 'NOT '}NULL"
        return f"{d} {sql_op} NULL"
    return {
        "=": f"{d} = {o}",
        "!=": f"({d} <> {o} OR {d} IS NULL)",
        "<": f"({d} < {o} OR {d} IS NULL)",
        "<=": f"({d} <= {o} OR {d} IS NULL)",
        ">": f"{d} > {o}",
        ">=": f"{d} >= {o}",
    }[sql_op]


def _empty_date_condition(
    parser: _FilterTransformer,
    left: Any,
    left_sql: str,
    op: str,
    right: Any,
    right_sql: str,
) -> str | None:
    """SQL of ``left op right`` when one operand is a nullable date column.

    Returns ``None`` for any other comparison, which keeps its usual SQL.
    """
    left_kind = _date_operand_kind(parser, left, left_sql)
    right_kind = _date_operand_kind(parser, right, right_sql)
    if "date" not in (left_kind, right_kind):
        return None
    if op in _LIKE_OPS:
        if left_kind == "date":
            left_sql = _pb_date_text_sql(left_sql)
        if right_kind == "date":
            right_sql = _pb_date_text_sql(right_sql)
            right = ("sql_ref", right_sql)
        return parser._like_comparison_sql(left_sql, right, right_sql, _LIKE_OPS[op])
    if op not in _STANDARD_OPS:
        return None
    sql_op = _STANDARD_OPS[op]
    if left_kind != "date":
        # ``value op date`` is ``date flipped(op) value``.
        left_sql, right_sql, right_kind = right_sql, left_sql, left_kind
        sql_op = _FLIPPED_STANDARD_OPS[sql_op]
    return _empty_date_sql(left_sql, sql_op, right_sql, right_kind)
