"""PocketBase filter syntax parser using Lark.

Translates PocketBase filter expressions into SQL WHERE clauses with
parameterized bindings.  Also provides ``parse_sort`` for sort expressions.

All literal values are passed as bound parameters -- never interpolated into
SQL strings -- to prevent SQL injection.
"""

from __future__ import annotations

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


class _CollectionRef:
    """Resolved ``@collection.X.path`` operand metadata."""

    __slots__ = (
        "table_name",
        "group_key",
        "column_sql",
        "relation_steps",
        "leaf_field",
    )

    def __init__(
        self,
        table_name: str,
        group_key: str,
        *,
        column_sql: str | None = None,
        relation_steps: list[tuple[str, str, int]] | None = None,
        leaf_field: str | None = None,
    ) -> None:
        self.table_name = table_name
        self.group_key = group_key
        self.column_sql = column_sql
        self.relation_steps = relation_steps or []
        self.leaf_field = leaf_field


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
            relation_steps: list[tuple[str, str, int]] = []
            current_resolver = self._relation_resolver

            for part in parts[:-1]:
                info = current_resolver.get(part)
                if not isinstance(info, dict):
                    break
                relation_steps.append(
                    (
                        _sanitize_ident(part),
                        _sanitize_ident(str(info["table"])),
                        int(info.get("max_select", 1) or 1),
                    )
                )
                nested = info.get("relations")
                current_resolver = nested if isinstance(nested, dict) else {}

            if relation_steps:
                leaf_parts = parts[len(relation_steps):]
                if len(leaf_parts) != 1:
                    raise ValueError(
                        f"Invalid relation traversal: {'.'.join(parts)}. "
                        "Only relation segments may be chained before the final field name."
                    )
                return ("relation_ref", relation_steps, _sanitize_ident(leaf_parts[0]))

        if modifier:
            return ("field_modifier", ".".join(parts), modifier)
        return ("field", ".".join(parts))

    # -- Comparisons ---------------------------------------------------------

    @v_args(inline=True)
    def comparison(
        self,
        left: tuple[str, Any],
        op_token: Any,
        right: tuple[str, Any],
    ) -> str | _CollectionCondition | _RelationCondition:
        op = str(op_token).strip()

        # ── Relation field traversal → generate _RelationCondition ─────
        left_is_rel = (
            isinstance(left, tuple)
            and len(left) == 3
            and left[0] == "relation_ref"
        )
        right_is_rel = (
            isinstance(right, tuple)
            and len(right) == 3
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
            return _CollectionRef(
                coll_name,
                group_key,
                column_sql=self._qualified_column(coll_name, field_path[0]),
            )

        coll_info = self._collection_resolver.get(coll_name)
        relations = coll_info.get("relations", {}) if isinstance(coll_info, dict) else {}

        relation_steps: list[tuple[str, str, int]] = []
        current_relations = relations if isinstance(relations, dict) else {}
        for part in field_path[:-1]:
            info = current_relations.get(part)
            if not isinstance(info, dict):
                break
            relation_steps.append(
                (
                    _sanitize_ident(part),
                    _sanitize_ident(str(info["table"])),
                    int(info.get("max_select", 1) or 1),
                )
            )
            nested = info.get("relations")
            current_relations = nested if isinstance(nested, dict) else {}

        if not relation_steps:
            raise ValueError(
                f"Invalid @collection relation traversal: "
                f"@collection.{coll_name}.{'.'.join(field_path)}. "
                "Missing relation metadata for the referenced collection."
            )

        leaf_parts = field_path[len(relation_steps):]
        if len(leaf_parts) != 1:
            raise ValueError(
                f"Invalid @collection relation traversal: "
                f"@collection.{coll_name}.{'.'.join(field_path)}. "
                "Only relation segments may be chained before the final field name."
            )

        leaf = leaf_parts[0]

        # @collection.X.relation.id can use the stored relation id directly.
        if len(relation_steps) == 1 and leaf == "id":
            return _CollectionRef(
                coll_name,
                group_key,
                column_sql=self._qualified_column(coll_name, relation_steps[0][0]),
            )

        return _CollectionRef(
            coll_name,
            group_key,
            relation_steps=relation_steps,
            leaf_field=leaf,
        )

    def _collection_relation_leaf_sql(self, ref: _CollectionRef) -> str:
        if not ref.relation_steps or not ref.leaf_field:
            if ref.column_sql:
                return ref.column_sql
            raise ValueError("Invalid @collection relation reference.")
        return self._qualified_column(ref.relation_steps[-1][1], ref.leaf_field)

    def _wrap_collection_relation_exists(
        self,
        ref: _CollectionRef,
        leaf_condition: str,
    ) -> str:
        wrapped = leaf_condition
        for idx in range(len(ref.relation_steps) - 1, -1, -1):
            field_name, target_table, max_select = ref.relation_steps[idx]
            if idx == 0:
                source_expr = self._qualified_column(ref.table_name, field_name)
            else:
                parent_table = ref.relation_steps[idx - 1][1]
                source_expr = self._qualified_column(parent_table, field_name)
            join_cond = (
                self._scalar_or_array_any_sql(
                    source_expr,
                    self._qualified_column(target_table, "id"),
                    "=",
                )
                if max_select > 1
                else f'{self._qualified_column(target_table, "id")} = {source_expr}'
            )
            wrapped = (
                f'EXISTS (SELECT 1 FROM "{target_table}" '
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

        def _relation_leaf_sql(steps: list[tuple[str, str, int]], leaf_field: str) -> str:
            return f'"{steps[-1][1]}"."{leaf_field}"'

        def _wrap_nested_relation_exists(
            steps: list[tuple[str, str, int]],
            leaf_condition: str,
        ) -> str:
            wrapped = leaf_condition
            for idx in range(len(steps) - 1, -1, -1):
                field_name, target_table, max_select = steps[idx]
                if idx == 0:
                    source_expr = self._field_expr(field_name)
                else:
                    parent_table = steps[idx - 1][1]
                    source_expr = self._qualified_column(parent_table, field_name)
                join_cond = (
                    self._scalar_or_array_any_sql(
                        source_expr,
                        self._qualified_column(target_table, "id"),
                        "=",
                    )
                    if max_select > 1
                    else f'{self._qualified_column(target_table, "id")} = {source_expr}'
                )
                wrapped = (
                    f'EXISTS (SELECT 1 FROM "{target_table}" '
                    f'WHERE {join_cond} AND {wrapped})'
                )
            return wrapped

        # Both sides are relation refs (rare) --------------------------------
        if left_is_rel and right_is_rel:
            l_steps, l_leaf = left[1], left[2]
            r_steps, r_leaf = right[1], right[2]
            if len(l_steps) > 1 or len(r_steps) > 1:
                raise ValueError(
                    "Comparisons between two nested relation traversals are not supported."
                )

            l_field, l_tbl, l_max = l_steps[0]
            r_field, r_tbl, r_max = r_steps[0]
            l_col = f'"{l_tbl}"."{l_leaf}"'
            r_col = f'"{r_tbl}"."{r_leaf}"'
            l_join = (
                self._scalar_or_array_any_sql(
                    self._field_expr(l_field),
                    self._qualified_column(l_tbl, "id"),
                    "=",
                )
                if l_max > 1
                else f'{self._qualified_column(l_tbl, "id")} = {self._field_expr(l_field)}'
            )
            r_join = (
                self._scalar_or_array_any_sql(
                    self._field_expr(r_field),
                    self._qualified_column(r_tbl, "id"),
                    "=",
                )
                if r_max > 1
                else f'{self._qualified_column(r_tbl, "id")} = {self._field_expr(r_field)}'
            )
            sql_op = _STANDARD_OPS.get(op, "=")
            return (
                f'EXISTS (SELECT 1 FROM "{l_tbl}", "{r_tbl}" '
                f"WHERE {l_join} AND {r_join} AND {l_col} {sql_op} {r_col})"
            )

        # One side is a relation ref -----------------------------------------
        if left_is_rel:
            rel_steps, traversed = left[1], left[2]
            other = right
            rel_is_left = True
        else:
            rel_steps, traversed = right[1], right[2]
            other = left
            rel_is_left = False

        rel_col = _relation_leaf_sql(rel_steps, traversed)
        other_sql = self._operand_to_sql(other)

        # Single-level paths keep grouped EXISTS behavior for AND clauses.
        if len(rel_steps) == 1:
            rel_field, target, max_sel = rel_steps[0]
            join_cond = (
                self._scalar_or_array_any_sql(
                    self._field_expr(rel_field),
                    self._qualified_column(target, "id"),
                    "=",
                )
                if max_sel > 1
                else f'{self._qualified_column(target, "id")} = {self._field_expr(rel_field)}'
            )
        else:
            join_cond = ""

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
            if len(rel_steps) == 1:
                return _RelationCondition(rel_steps[0][0], rel_steps[0][1], join_cond, where_cond)
            return _wrap_nested_relation_exists(rel_steps, where_cond)

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
            if len(rel_steps) == 1:
                return _RelationCondition(rel_steps[0][0], rel_steps[0][1], join_cond, where_cond)
            return _wrap_nested_relation_exists(rel_steps, where_cond)

        if op in _ANY_STANDARD_OPS:
            sql_op = _ANY_STANDARD_OPS[op]
            if rel_is_left:
                where_cond = self._scalar_or_array_any_sql(rel_col, other_sql, sql_op)
            else:
                where_cond = self._scalar_or_array_any_sql(other_sql, rel_col, sql_op)
            if len(rel_steps) == 1:
                return _RelationCondition(rel_steps[0][0], rel_steps[0][1], join_cond, where_cond)
            return _wrap_nested_relation_exists(rel_steps, where_cond)

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
            if len(rel_steps) == 1:
                return _RelationCondition(rel_steps[0][0], rel_steps[0][1], join_cond, where_cond)
            return _wrap_nested_relation_exists(rel_steps, where_cond)

        # Fallback
        if rel_is_left:
            where_cond = f"{rel_col} = {other_sql}"
        else:
            where_cond = f"{other_sql} = {rel_col}"
        if len(rel_steps) == 1:
            return _RelationCondition(rel_steps[0][0], rel_steps[0][1], join_cond, where_cond)
        return _wrap_nested_relation_exists(rel_steps, where_cond)

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
            return "((date_trunc('day', TIMEZONE('UTC', NOW())) + INTERVAL '1 day' - INTERVAL '1 microsecond') AT TIME ZONE 'UTC')"
        if name == "monthStart":
            return "(date_trunc('month', TIMEZONE('UTC', NOW())) AT TIME ZONE 'UTC')"
        if name == "monthEnd":
            return "((date_trunc('month', TIMEZONE('UTC', NOW())) + INTERVAL '1 month' - INTERVAL '1 microsecond') AT TIME ZONE 'UTC')"
        if name == "yearStart":
            return "(date_trunc('year', TIMEZONE('UTC', NOW())) AT TIME ZONE 'UTC')"
        if name == "yearEnd":
            return "((date_trunc('year', TIMEZONE('UTC', NOW())) + INTERVAL '1 year' - INTERVAL '1 microsecond') AT TIME ZONE 'UTC')"
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
