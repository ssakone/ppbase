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


@dataclass(frozen=True)
class _ResolvedPath:
    """Resolved SQL expression and traversal hops for a dotted field path."""

    steps: list[_TraversalStep]
    sql: str
    is_json: bool = False
    field_type: str = ""


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
    ) -> None:
        self.table_name = table_name
        self.group_key = group_key
        self.column_sql = column_sql
        self.relation_steps = relation_steps or []
        self.leaf_sql = leaf_sql
        self.leaf_is_json = leaf_is_json
        self.leaf_type = leaf_type


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
            field_path = tuple(_sanitize_ident(part) for part in parts[2:] if part)
            if not field_path:
                raise ValueError(
                    f"Invalid @collection reference: @{stripped}. "
                    "Expected format: @collection.collectionName.fieldName"
                )
            return ("collection_ref", coll_name, field_path, group_key)

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

    def _field_type_for_collection_resolver(
        self,
        table_name: str,
        field_name: str,
    ) -> str:
        safe_field = _sanitize_ident(str(field_name))
        if safe_field in _SYSTEM_FIELD_TYPES:
            return _SYSTEM_FIELD_TYPES[safe_field]
        safe_table = _sanitize_ident(str(table_name))
        if self._is_flat_resolver(self._collection_resolver):
            field_info = self._flat_fields(self._collection_resolver, safe_table).get(
                safe_field,
            )
        else:
            coll_info = self._collection_resolver.get(safe_table)
            fields = coll_info.get("fields", {}) if isinstance(coll_info, dict) else {}
            field_info = fields.get(safe_field) if isinstance(fields, dict) else None
        if isinstance(field_info, dict):
            return str(field_info.get("type") or "")
        return ""

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
                    and step.max_select <= 1
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
            and len(left) == 4
            and left[0] == "collection_ref"
        )
        right_is_coll = (
            isinstance(right, tuple)
            and len(right) == 4
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
            # Auto-wrap value with % for LIKE
            if right[0] == "literal" and isinstance(right[1], str):
                wrapped = f"%{right[1]}%"
                pname = self._next_param(wrapped)
                return f"{left_sql} {sql_op} :{pname}"
            return f"{left_sql} {sql_op} {right_sql}"

        # ANY standard operators (for array columns)
        if op in _ANY_STANDARD_OPS:
            sql_op = _ANY_STANDARD_OPS[op]
            return self._scalar_or_array_any_sql(left_sql, right_sql, sql_op)

        # ANY LIKE operators
        if op in _ANY_LIKE_OPS:
            sql_op = _ANY_LIKE_OPS[op]
            if right[0] == "literal" and isinstance(right[1], str):
                wrapped = f"%{right[1]}%"
                pname = self._next_param(wrapped)
                return self._scalar_or_array_like_sql(left_sql, f":{pname}", sql_op)
            return self._scalar_or_array_like_sql(left_sql, right_sql, sql_op)

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
        if sql_op not in {"=", "!="}:
            return f"{value_sql} {sql_op} ANY({container_sql})"

        container_text = self._text_cast_sql(container_sql)
        value_text = self._text_cast_sql(value_sql)
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
        return (
            "(CASE "
            f"WHEN jsonb_typeof(to_jsonb({container_sql})) = 'array' THEN "
            f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(to_jsonb({container_sql})) "
            f"AS _elem(value) WHERE _elem.value {sql_op} {pattern_sql}) "
            f"ELSE {container_sql}::text {sql_op} {pattern_sql} "
            "END)"
        )

    def _resolve_collection_ref(self, node: tuple) -> _CollectionRef:
        coll_name = _sanitize_ident(str(node[1]))
        field_path = tuple(_sanitize_ident(str(part)) for part in node[2])
        group_key = str(node[3])

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

        if not resolved.steps:
            return _CollectionRef(
                coll_name,
                group_key,
                column_sql=resolved.sql,
                leaf_type=resolved.field_type,
            )

        return _CollectionRef(
            coll_name,
            group_key,
            relation_steps=resolved.steps,
            leaf_sql=resolved.sql,
            leaf_is_json=resolved.is_json,
            leaf_type=resolved.field_type,
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
            if step.max_select > 1:
                return self._scalar_or_array_any_sql(target_expr, parent_id, "=")
            return f"{target_expr} = {parent_id}"

        source_field = step.source_field or step.relation_key
        source_expr = self._column_expr_for_table(parent_table, source_field)
        target_id = self._qualified_column(step.target_table, "id")
        if step.max_select > 1:
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

    def _collection_ref_condition(
        self,
        ref: _CollectionRef,
        op: str,
        other: tuple,
        *,
        ref_is_left: bool,
    ) -> str:
        ref_sql = (
            ref.column_sql
            if ref.column_sql is not None
            else self._collection_relation_leaf_sql(ref)
        )
        if ref.leaf_type in _DATETIME_FIELD_TYPES:
            other = self._literal_for_field_type(other, ref.leaf_type)
        other_sql = self._operand_to_sql(other)

        if op in _STANDARD_OPS:
            sql_op = _STANDARD_OPS[op]
            if other[0] == "null":
                if sql_op == "=":
                    condition = f"{ref_sql} IS NULL"
                elif sql_op == "!=":
                    condition = f"{ref_sql} IS NOT NULL"
                else:
                    condition = f"{ref_sql} {sql_op} NULL"
            elif ref_is_left:
                condition = f"{ref_sql} {sql_op} {other_sql}"
            else:
                condition = f"{other_sql} {sql_op} {ref_sql}"
        elif op in _LIKE_OPS:
            sql_op = _LIKE_OPS[op]
            if other[0] == "literal" and isinstance(other[1], str):
                wrapped = f"%{other[1]}%"
                pname = self._next_param(wrapped)
                other_sql = f":{pname}"
            if ref_is_left:
                condition = f"{ref_sql} {sql_op} {other_sql}"
            else:
                condition = f"{other_sql} {sql_op} {ref_sql}"
        elif op in _ANY_STANDARD_OPS:
            sql_op = _ANY_STANDARD_OPS[op]
            if ref_is_left:
                condition = self._scalar_or_array_any_sql(ref_sql, other_sql, sql_op)
            else:
                condition = self._scalar_or_array_any_sql(other_sql, ref_sql, sql_op)
        elif op in _ANY_LIKE_OPS:
            sql_op = _ANY_LIKE_OPS[op]
            if other[0] == "literal" and isinstance(other[1], str):
                wrapped = f"%{other[1]}%"
                pname = self._next_param(wrapped)
                other_sql = f":{pname}"
            if ref_is_left:
                condition = self._scalar_or_array_like_sql(ref_sql, other_sql, sql_op)
            else:
                condition = self._scalar_or_array_like_sql(other_sql, ref_sql, sql_op)
        else:
            condition = (
                f"{ref_sql} = {other_sql}"
                if ref_is_left
                else f"{other_sql} = {ref_sql}"
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
            l_col = (
                l_ref.column_sql
                if l_ref.column_sql is not None
                else self._collection_relation_leaf_sql(l_ref)
            )
            r_col = (
                r_ref.column_sql
                if r_ref.column_sql is not None
                else self._collection_relation_leaf_sql(r_ref)
            )
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

        # Both sides are relation refs (rare) --------------------------------
        if left_is_rel and right_is_rel:
            l_steps, l_col = left[1], left[2]
            r_steps, r_col = right[1], right[2]
            if len(l_steps) > 1 or len(r_steps) > 1:
                raise ValueError(
                    "Comparisons between two nested relation traversals are not supported."
                )

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
            rel_steps, rel_col = left[1], left[2]
            other = right
            rel_is_left = True
        else:
            rel_steps, rel_col = right[1], right[2]
            other = left
            rel_is_left = False

        other_sql = self._operand_to_sql(other)

        # WHERE condition — mirrors operator handling of _collection_comparison
        where_cond: str

        if op in _STANDARD_OPS:
            sql_op = _STANDARD_OPS[op]
            if other[0] == "null":
                if sql_op == "=":
                    where_cond = f"{rel_col} IS NULL"
                elif sql_op == "!=":
                    where_cond = f"{rel_col} IS NOT NULL"
                else:
                    where_cond = f"{rel_col} {sql_op} NULL"
            elif rel_is_left:
                where_cond = f"{rel_col} {sql_op} {other_sql}"
            else:
                where_cond = f"{other_sql} {sql_op} {rel_col}"
            return _relation_condition_for_steps(rel_steps, where_cond)

        if op in _LIKE_OPS:
            sql_op = _LIKE_OPS[op]
            if rel_is_left and other[0] == "literal" and isinstance(other[1], str):
                wrapped = f"%{other[1]}%"
                pname = self._next_param(wrapped)
                where_cond = f"{rel_col} {sql_op} :{pname}"
            elif rel_is_left:
                where_cond = f"{rel_col} {sql_op} {other_sql}"
            else:
                where_cond = f"{other_sql} {sql_op} {rel_col}"
            return _relation_condition_for_steps(rel_steps, where_cond)

        if op in _ANY_STANDARD_OPS:
            sql_op = _ANY_STANDARD_OPS[op]
            if rel_is_left:
                where_cond = self._scalar_or_array_any_sql(rel_col, other_sql, sql_op)
            else:
                where_cond = self._scalar_or_array_any_sql(other_sql, rel_col, sql_op)
            return _relation_condition_for_steps(rel_steps, where_cond)

        if op in _ANY_LIKE_OPS:
            sql_op = _ANY_LIKE_OPS[op]
            if rel_is_left and other[0] == "literal" and isinstance(other[1], str):
                wrapped = f"%{other[1]}%"
                pname = self._next_param(wrapped)
                where_cond = self._scalar_or_array_like_sql(rel_col, f":{pname}", sql_op)
            elif rel_is_left:
                where_cond = self._scalar_or_array_like_sql(rel_col, other_sql, sql_op)
            else:
                where_cond = self._scalar_or_array_like_sql(other_sql, rel_col, sql_op)
            return _relation_condition_for_steps(rel_steps, where_cond)

        # Fallback
        if rel_is_left:
            where_cond = f"{rel_col} = {other_sql}"
        else:
            where_cond = f"{other_sql} = {rel_col}"
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

    def _extract_each_operand(self, node: tuple[str, Any]) -> tuple[str, str] | None:
        """Extract SQL array expression for ``:each`` from an operand."""
        if not isinstance(node, tuple) or len(node) < 2:
            return None

        # Collection field modifier: field:each
        if node[0] == "field_modifier" and len(node) >= 3 and node[2] == "each":
            field_sql = self._field_expr(node[1])
            return ("field", f"COALESCE({field_sql}::text[], ARRAY[]::text[])")

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
                values: list[str]
                if not has_field or raw_val is None:
                    values = []
                elif isinstance(raw_val, (list, tuple, set)):
                    values = [str(v) for v in raw_val]
                else:
                    values = [str(raw_val)]

                pname = self._next_param(values)
                return ("request", f"COALESCE(CAST(:{pname} AS text[]), ARRAY[]::text[])")

        return None

    def _build_each_item_condition(
        self,
        item_sql: str,
        op: str,
        other: tuple[str, Any],
        *,
        each_is_left: bool,
    ) -> str:
        """Build per-item comparison SQL for an ``:each`` operand."""
        other_sql = self._operand_to_sql(other)

        if op in _STANDARD_OPS:
            sql_op = _STANDARD_OPS[op]
            if other[0] == "null":
                if sql_op == "=":
                    return f"{item_sql} IS NULL"
                if sql_op == "!=":
                    return f"{item_sql} IS NOT NULL"
                return f"{item_sql} {sql_op} NULL"
            if each_is_left:
                return f"{item_sql} {sql_op} {other_sql}"
            return f"{other_sql} {sql_op} {item_sql}"

        if op in _LIKE_OPS:
            sql_op = _LIKE_OPS[op]
            if each_is_left:
                if other[0] == "literal" and isinstance(other[1], str):
                    wrapped = f"%{other[1]}%"
                    pname = self._next_param(wrapped)
                    return f"{item_sql} {sql_op} :{pname}"
                return f"{item_sql} {sql_op} {other_sql}"
            if other[0] == "literal" and isinstance(other[1], str):
                wrapped = f"%{other[1]}%"
                pname = self._next_param(wrapped)
                return f":{pname} {sql_op} {item_sql}"
            return f"{other_sql} {sql_op} {item_sql}"

        # Fallback to "=" semantics for unsupported operator combos.
        if each_is_left:
            return f"{item_sql} = {other_sql}"
        return f"{other_sql} = {item_sql}"

    def _each_comparison(
        self,
        left: tuple[str, Any],
        op: str,
        right: tuple[str, Any],
        left_each: tuple[str, str] | None,
        right_each: tuple[str, str] | None,
    ) -> str:
        """Handle comparisons where at least one side uses ``:each``."""
        if left_each and right_each:
            raise ValueError("Comparisons with :each on both sides are not supported.")

        if left_each:
            array_sql = left_each[1]
            condition = self._build_each_item_condition(
                "_elem", op, right, each_is_left=True,
            )
        else:
            array_sql = right_each[1] if right_each else "ARRAY[]::text[]"
            condition = self._build_each_item_condition(
                "_elem", op, left, each_is_left=False,
            )

        # "All items match" semantics; empty arrays evaluate to true.
        return (
            f"NOT EXISTS (SELECT 1 FROM unnest({array_sql}) AS _elem "
            f"WHERE NOT COALESCE(({condition}), FALSE))"
        )

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
            return self._field_expr(value)
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
        if modifier == "isset":
            return f"({field_sql} IS NOT NULL)"
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
            pname = self._next_param(val)
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
                pname = self._next_param(self._value_length(val) if has_field else 0)
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
                values: list[str]
                if has_field and isinstance(val, (list, tuple, set)):
                    values = [str(v) for v in val]
                elif has_field and val is not None:
                    values = [str(val)]
                else:
                    values = []
                pname = self._next_param(values)
                return f"CAST(:{pname} AS text[])"
            if modifier == "lower":
                lowered = ""
                if has_field and val is not None:
                    lowered = str(val).lower()
                pname = self._next_param(lowered)
                return f":{pname}"

            pname = self._next_param(val if has_field else "")
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
    where_sql = transformer.transform(tree)
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
