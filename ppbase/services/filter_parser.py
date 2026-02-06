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

MACRO: /@[a-zA-Z_][a-zA-Z0-9_.]*/

field_path: FIELD_IDENT ("." FIELD_IDENT)*

FIELD_IDENT: /[a-zA-Z_][a-zA-Z0-9_]*/

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
# Transformer: AST -> (sql_fragment, params_dict)
# ---------------------------------------------------------------------------


class _FilterTransformer(Transformer):
    """Transform the parse tree into (SQL text, params dict)."""

    def __init__(self, request_context: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._param_counter = 0
        self._params: dict[str, Any] = {}
        self._request_context = request_context or {}

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

    def macro(self, items: list) -> tuple[str, str]:
        name = str(items[0])  # e.g. "@now", "@request.auth.id"
        return ("macro", name)

    def field_path(self, items: list) -> tuple[str, str]:
        parts = [str(t) for t in items]
        return ("field", ".".join(parts))

    # -- Comparisons ---------------------------------------------------------

    @v_args(inline=True)
    def comparison(
        self,
        left: tuple[str, Any],
        op_token: Any,
        right: tuple[str, Any],
    ) -> str:
        op = str(op_token).strip()
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
            return f"{right_sql} {sql_op} ANY({left_sql})"

        # ANY LIKE operators
        if op in _ANY_LIKE_OPS:
            sql_op = _ANY_LIKE_OPS[op]
            if right[0] == "literal" and isinstance(right[1], str):
                wrapped = f"%{right[1]}%"
                pname = self._next_param(wrapped)
                return f"EXISTS (SELECT 1 FROM unnest({left_sql}) AS _elem WHERE _elem {sql_op} :{pname})"
            return f"EXISTS (SELECT 1 FROM unnest({left_sql}) AS _elem WHERE _elem {sql_op} {right_sql})"

        return f"{left_sql} = {right_sql}"

    # -- Logical operators ---------------------------------------------------

    def or_expr(self, items: list) -> str:
        if len(items) == 1:
            return items[0]
        return "(" + " OR ".join(str(i) for i in items) + ")"

    def and_expr(self, items: list) -> str:
        if len(items) == 1:
            return items[0]
        return "(" + " AND ".join(str(i) for i in items) + ")"

    # -- Helpers -------------------------------------------------------------

    def _operand_to_sql(self, node: tuple[str, Any]) -> str:
        kind, value = node
        if kind == "literal":
            pname = self._next_param(value)
            return f":{pname}"
        if kind == "null":
            return "NULL"
        if kind == "field":
            # Sanitize: only allow alphanumeric, underscore, and dot
            safe = value
            for ch in safe:
                if ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.":
                    raise ValueError(f"Invalid character in field name: {ch!r}")
            return f'"{safe}"' if "." not in safe else safe
        if kind == "macro":
            return self._resolve_macro(value)
        return str(value)

    def _resolve_macro(self, macro_name: str) -> str:
        name = macro_name.lstrip("@")
        if name == "now":
            return "NOW()"
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
        if name.startswith("request.data."):
            field = name[len("request.data."):]
            data = self._request_context.get("data", {})
            val = data.get(field, "")
            pname = self._next_param(val)
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
) -> tuple[str, dict[str, Any]]:
    """Parse a PocketBase filter expression into a SQL WHERE clause.

    Args:
        filter_str: The PocketBase filter string.
        request_context: Optional dict with ``auth``, ``data``, ``query`` keys
            for resolving ``@request.*`` macros.

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

    transformer = _FilterTransformer(request_context)
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
