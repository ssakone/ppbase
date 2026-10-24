"""Single source of truth for the shape of multi-valued fields.

``select``, ``file`` and ``relation`` fields store either one string or an
array of strings.  Every layer that decides that shape (DDL, validation,
serialization, expand, filters, cascade delete, backups, files) must use the
helpers below instead of re-deriving it from ``maxSelect``.

PocketBase references (tag, file, line):

* v0.22.34 ``models/schema/schema_field.go``

  - l.693: ``RelationOptions.MaxSelect *int`` ("If nil no limits are applied").
  - l.709: ``MaxSelect`` is validated ``NilOrNotEmpty`` and ``Min(minSelect)``,
    so ``0`` is refused when a collection is saved.
  - l.715-716: ``IsMultiple() = MaxSelect == nil || *MaxSelect > 1``.
  - l.603 / l.649: ``SelectOptions.MaxSelect`` and ``FileOptions.MaxSelect``
    are plain ``int`` (a JSON ``null`` decodes to ``0``); their
    ``IsMultiple()`` (l.626-627, l.667-668) is ``MaxSelect > 1``.
  - l.150-165: multiple values use ``JSON DEFAULT '[]' NOT NULL``, single
    values ``TEXT DEFAULT '' NOT NULL``.
  - l.375-386: relation ``PrepareValue``: ``list.ToUniqueStringSlice`` then the
    last id for a single relation, the list for a multiple one.
  - ``RelationOptions`` marshals ``maxSelect`` without ``omitempty``, so every
    definition produced by PocketBase 0.22 (API, export, ``_collections`` row)
    carries the key explicitly, as ``null`` when unlimited.

* v0.22.34 ``forms/validators/record_data.go`` l.376: the ``maxSelect`` limit
  is only checked when it is not ``nil``.

* v0.40.6 (latest tag) ``core/field_relation.go`` l.98 ``MaxSelect int``,
  l.151-153 ``IsMultiple() = MaxSelect > 1``, l.156-162 ``JSON``/``TEXT``
  column, l.212 ``maxSelect := max(f.MaxSelect, 1)`` for validation.

* v0.23.0 ``migrations/1717233556_v0.23_migrate.go`` l.844-848: the upgrade
  turns a relation ``maxSelect <= 0`` (including ``null``) into
  ``2147483647``, i.e. an unlimited multiple relation.

PPBase stores every field with nested ``options`` (flat v0.23+ payloads are
normalized on input), so the rule is expressed on the normalized options:

* relation, ``maxSelect`` key present with ``null`` -> multiple, no upper
  limit (PocketBase 0.22 ``nil`` pointer, and the v0.23 upgrade result);
* any field, ``maxSelect`` numeric ``n`` -> multiple iff ``n > 1`` (identical
  in 0.22 and in the current release; ``0`` stays single as in 0.22.34
  ``IsMultiple`` and the current ``IsMultiple``);
* ``maxSelect`` key absent -> single.  PocketBase 0.22 never emits an absent
  key (see above), current PocketBase decodes an absent key as ``0`` (single)
  and PPBase has always treated it as ``1``; keeping it single leaves every
  existing PPBase collection unchanged;
* ``select``/``file`` with ``maxSelect: null`` -> single, exactly like the
  0.22 ``int`` decoding.

Two deliberate differences follow from storing one normalized shape for both
PocketBase formats (no BUMA field is affected: the 191 relation fields of the
BUMA export all carry the key, 188 with ``1`` and 3 with ``null``):

* a hand-written 0.22 relation *without* the ``maxSelect`` key is single in
  PPBase, while PocketBase 0.22.34 decodes it as a ``nil`` pointer (multiple)
  and the v0.23.0 upgrade turns it into ``2147483647``;
* a *flat* (current format) relation sent with ``maxSelect: null`` is multiple
  in PPBase (it is the round trip of the 0.22 ``null`` that PPBase exposes in
  the flat ``fields`` list too), while PocketBase v0.40.6 decodes ``null`` into
  its ``int`` as ``0``, i.e. single.

The stored schema is never rewritten: the API keeps returning
``maxSelect: null`` as PocketBase 0.22 does.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

MAX_SELECT_KEY = "maxSelect"

_MULTI_VALUE_TYPES = frozenset({"select", "file", "relation"})

# Keys that live at the top level of a stored field definition; everything
# else is a type-specific option (mirrors ``record_service._normalize_field``).
_FIELD_CORE_KEYS = frozenset(
    {"id", "name", "type", "required", "system", "hidden", "presentable", "options"}
)

_ABSENT = object()


def _field_type_name(field_type: Any) -> str:
    value = getattr(field_type, "value", field_type)
    return str(value or "")


def field_options(field: Any) -> dict[str, Any]:
    """Return the type-specific options of a field definition.

    Accepts a :class:`~ppbase.models.field_types.FieldDefinition`, a stored
    nested definition (``{"options": {...}}``) or a flat v0.23+ definition.
    Flat keys are merged under nested ``options``; nested values win, exactly
    like ``models.collection._fields_to_schema`` and
    ``record_service._normalize_field``.
    """
    if field is None:
        return {}
    if not isinstance(field, Mapping):
        options = getattr(field, "options", None)
        return dict(options) if isinstance(options, Mapping) else {}
    nested = field.get("options")
    flat = {key: value for key, value in field.items() if key not in _FIELD_CORE_KEYS}
    if isinstance(nested, Mapping):
        return {**flat, **nested}
    return flat


def _max_select_number(raw: Any) -> int | None:
    """Coerce a stored ``maxSelect`` number; ``None`` when not numeric."""
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if math.isfinite(raw) else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _raw_max_select(options: Mapping[str, Any] | None) -> Any:
    if not isinstance(options, Mapping):
        return _ABSENT
    return options.get(MAX_SELECT_KEY, _ABSENT)


def relation_is_multiple(options: Mapping[str, Any] | None) -> bool:
    """Return whether relation ``options`` describe a multiple relation."""
    raw = _raw_max_select(options)
    if raw is None:
        return True
    if raw is _ABSENT:
        return False
    number = _max_select_number(raw)
    return number is not None and number > 1


def relation_max_select(options: Mapping[str, Any] | None) -> int | None:
    """Return the relation ``maxSelect`` limit, ``None`` meaning unlimited.

    Single relations always keep one id (``PrepareValue``), so the limit only
    matters for multiple relations: ``null`` is unlimited (0.22.34
    ``record_data.go`` l.376), a number ``n > 1`` is the limit.
    """
    raw = _raw_max_select(options)
    if raw is None:
        return None
    number = _max_select_number(raw) if raw is not _ABSENT else None
    if number is None or number < 1:
        return 1
    return number


def select_or_file_is_multiple(options: Mapping[str, Any] | None) -> bool:
    """Return whether select/file ``options`` describe multiple values.

    ``maxSelect`` is a plain integer for these types in every PocketBase
    release, so ``null``/absent/``0``/``1`` are single.
    """
    raw = _raw_max_select(options)
    if raw is None or raw is _ABSENT:
        return False
    number = _max_select_number(raw)
    return number is not None and number > 1


def select_or_file_max_select(options: Mapping[str, Any] | None) -> int:
    """Return the select/file ``maxSelect`` count limit (at least ``1``)."""
    raw = _raw_max_select(options)
    number = None if raw is None or raw is _ABSENT else _max_select_number(raw)
    if number is None or number < 1:
        return 1
    return number


def is_multiple(field_type: Any, options: Mapping[str, Any] | None) -> bool:
    """Return whether a field of ``field_type`` stores an array of strings."""
    type_name = _field_type_name(field_type)
    if type_name == "relation":
        return relation_is_multiple(options)
    if type_name in ("select", "file"):
        return select_or_file_is_multiple(options)
    return False


def field_is_multiple(field: Any) -> bool:
    """Return whether a field definition (any representation) is multi-valued."""
    if field is None:
        return False
    if isinstance(field, Mapping):
        field_type = field.get("type")
    else:
        field_type = getattr(field, "type", None)
    if _field_type_name(field_type) not in _MULTI_VALUE_TYPES:
        return False
    return is_multiple(field_type, field_options(field))


def _go_cast_string(value: Any) -> str:
    """Approximate ``spf13/cast.ToString`` for JSON-decoded values."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if value.is_integer():
            return str(int(value))
        # strconv.FormatFloat(v, 'f', -1, 64): shortest round-trip digits,
        # never in exponent notation.
        return format(Decimal(repr(value)), "f")
    return ""


def to_unique_string_list(value: Any) -> list[str]:
    """Port of PocketBase ``list.ToUniqueStringSlice`` (v0.22.34 l.110-140).

    ``None`` -> ``[]``; a string is one element unless it contains ``[`` and
    decodes as a JSON array of strings; lists are cast element by element.
    Empty strings and duplicates are dropped, keeping the first occurrence.
    """
    items: list[str]
    if value is None:
        items = []
    elif isinstance(value, str):
        if value == "":
            items = []
        elif "[" in value:
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = None
            if isinstance(decoded, list) and all(
                isinstance(item, str) or item is None for item in decoded
            ):
                items = ["" if item is None else item for item in decoded]
            else:
                items = [value]
        else:
            items = [value]
    elif isinstance(value, (list, tuple)):
        items = [_go_cast_string(item) for item in value]
    elif isinstance(value, (bool, int, float)):
        items = [_go_cast_string(value)]
    else:
        items = []

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item == "" or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def prepare_relation_value(value: Any, *, multiple: bool) -> str | list[str]:
    """Port of PocketBase relation ``PrepareValue`` (v0.22.34 l.375-386).

    A single relation keeps the last selected id (``""`` when empty); a
    multiple relation keeps the ordered, de-duplicated list.
    """
    ids = to_unique_string_list(value)
    if not multiple:
        return ids[-1] if ids else ""
    return ids


# Go's encoding/json escapes these characters by default (HTMLEscape).
_GO_JSON_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}


def _go_json_compatible(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _go_json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_go_json_compatible(item) for item in value]
    if isinstance(value, Decimal):
        value = float(value)
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
        and abs(value) < 1e21
    ):
        # Go formats float64(1) as 1, not 1.0.
        return int(value)
    return value


def go_json_text(value: Any) -> str:
    """Encode ``value`` like Go ``json.Marshal`` (compact, sorted map keys).

    PocketBase binds a non-plain ``@request.*`` value (a list or an object) as
    its JSON text (v0.22.34 ``resolvers/record_field_resolver.go``
    ``resolveStaticRequestField``: ``cast.ToStringE`` fails, then
    ``json.Marshal``), and a multi-value column holds the same compact JSON.
    """
    encoded = json.dumps(
        _go_json_compatible(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    for char, escaped in _GO_JSON_ESCAPES.items():
        encoded = encoded.replace(char, escaped)
    return encoded


__all__ = [
    "MAX_SELECT_KEY",
    "field_is_multiple",
    "field_options",
    "go_json_text",
    "is_multiple",
    "prepare_relation_value",
    "relation_is_multiple",
    "relation_max_select",
    "select_or_file_is_multiple",
    "select_or_file_max_select",
    "to_unique_string_list",
]
