"""Pydantic models for record API responses.

Provides response models for single records and paginated lists, plus
helpers to build response dicts from raw database rows.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from ppbase.models.field_multiplicity import field_is_multiple


def format_datetime(dt: datetime | str | None) -> str:
    """Format a datetime value to PocketBase's string format.

    PocketBase uses ``YYYY-MM-DD HH:MM:SS.mmmZ``.
    """
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# Field types whose value is a PocketBase ``types.DateTime``.  PPBase stores
# an empty date as NULL (``TIMESTAMPTZ NULL``) where PocketBase stores ''
# (``TEXT DEFAULT '' NOT NULL``, v0.22.34 models/schema/schema_field.go
# l.163, v0.40.6 core/field_date.go l.109-110); the zero DateTime always
# marshals as "" (v0.22.34 tools/types/datetime.go l.47-58, v0.40.6
# l.100-111), so NULL is served "" and never null.
_DATETIME_FIELD_TYPES = frozenset({"date", "autodate"})

# PocketBase reads a SQL NULL as ``nil`` (v0.22.34 models/record.go l.48-58,
# v0.40.6 core/record_model.go l.491-500) and serves every schema field
# through its type's ``PrepareValue`` (v0.22.34 models/record.go l.328-359
# and l.528-531, models/schema/schema_field.go l.304-390; v0.40.6
# core/record_model.go l.496-499 and l.1274-1289).  For ``nil`` this gives
# the zero value of the type, identical in both releases:
#
# * text, email, url, editor: ``cast.ToString(nil)`` = "" (v0.22.34 l.310-311;
#   v0.40.6 core/field_text.go l.174-176, field_email.go l.117-119,
#   field_url.go l.117-119, field_editor.go l.121-123);
# * number: ``cast.ToFloat64(nil)`` = 0 (l.344-345; field_number.go l.134-136);
# * bool: ``cast.ToBool(nil)`` = false (l.346-347; field_bool.go l.104-106);
# * select, file, relation: ``list.ToUniqueStringSlice(nil)`` is an empty,
#   non-nil slice (v0.22.34 tools/list/list.go l.90-108 and l.111-140): ""
#   for a single field, [] for a multiple one (l.351-386; field_select.go
#   l.156-167, field_file.go l.714-726 and l.764-777, field_relation.go
#   l.169-180); multiplicity is ``field_is_multiple`` (D-0019 rule);
# * json: ``ParseJsonRaw(nil)`` is an empty ``JsonRaw`` that marshals as
#   ``null`` (l.312-343, tools/types/json_raw.go l.26-32 and l.56-83;
#   field_json.go l.121-150): a NULL json stays null.
#
# ``geoPoint`` only exists in current PocketBase (v0.40.6
# core/field_geo_point.go l.114-118 serves {"lon":0,"lat":0}) and unknown
# types are left unmodified by 0.22.34 (l.387-388): both keep ``None``.
_EMPTY_STRING_FIELD_TYPES = frozenset({"text", "email", "url", "editor"})
_MULTI_VALUE_FIELD_TYPES = frozenset({"select", "file", "relation"})


def null_field_value(field_def: Mapping[str, Any]) -> Any:
    """Return the value PocketBase serves for a NULL value of ``field_def``.

    ``field_def`` is a stored schema field (nested 0.22 ``options`` or flat
    v0.23+ keys).  Returns ``None`` when PocketBase serves ``null`` (json,
    geoPoint, unknown types).
    """
    raw_type = field_def.get("type")
    field_type = str(getattr(raw_type, "value", raw_type) or "")
    if field_type in _DATETIME_FIELD_TYPES or field_type in _EMPTY_STRING_FIELD_TYPES:
        return ""
    if field_type == "number":
        return 0
    if field_type == "bool":
        return False
    if field_type in _MULTI_VALUE_FIELD_TYPES:
        return [] if field_is_multiple(field_def) else ""
    return None


# Auth collection columns that must NEVER appear in API responses
_AUTH_HIDDEN_COLUMNS = frozenset({
    "password_hash", "token_key",
})

# Auth collection columns that need snake_case → camelCase mapping
_AUTH_COLUMN_MAP = {
    "email_visibility": "emailVisibility",
}


def build_record_response(
    row: dict[str, Any],
    collection_id: str,
    collection_name: str,
    schema: list[dict[str, Any]],
    *,
    fields_filter: list[str] | None = None,
    hidden_fields: set[str] | None = None,
    is_auth_collection: bool = False,
    is_view_collection: bool = False,
    request_auth: dict[str, Any] | None = None,
    apply_email_visibility: bool = False,
) -> dict[str, Any]:
    """Build a record response dict from a raw database row.

    A NULL (or absent) schema column is served as the PocketBase zero value
    of its field type (``null_field_value``, ``docs/null-values.md``).

    Args:
        row: Dict of column_name -> value from the DB query.
        collection_id: The parent collection's ID.
        collection_name: The parent collection's name.
        schema: The collection's field schema list.
        fields_filter: Optional list of field names to include in response.
            If None, all non-hidden fields are included.
        hidden_fields: Set of field names marked as hidden in the schema.
        is_auth_collection: If True, add auth system columns (email,
            emailVisibility, verified) and hide password_hash/token_key.
        is_view_collection: If True and no schema is defined, include raw row
            columns (except system/hidden auth internals).

    Returns:
        A dict suitable for JSON serialization as a record response.
    """
    hidden = hidden_fields or set()

    # A NULL id (e.g. a view row without one) is served "" like any other
    # NULL text: v0.22.34 models/record.go l.294-295 and l.544
    # (``cast.ToString``), v0.40.6 core/record_model.go l.488-512.
    record_id = row.get("id")
    result: dict[str, Any] = {
        "id": "" if record_id is None else record_id,
        "collectionId": collection_id,
        "collectionName": collection_name,
        "created": format_datetime(row.get("created")),
        "updated": format_datetime(row.get("updated")),
    }
    if is_view_collection:
        # A view record exports created/updated only when they are not zero
        # (v0.22.34 models/record.go l.545-550): a view that does not select
        # them, or selects an empty value, has no such key.
        for key in ("created", "updated"):
            if not result[key]:
                del result[key]

    def _can_view_auth_email() -> bool:
        if not apply_email_visibility:
            return True
        if bool(row.get("email_visibility", False)):
            return True
        if not request_auth:
            return False
        auth_type = request_auth.get("type")
        if auth_type == "admin":
            return True
        if auth_type not in {"auth", "authRecord"}:
            return False
        auth_id = str(request_auth.get("id", "") or "")
        row_id = str(row.get("id", "") or "")
        if auth_id != row_id:
            return False
        auth_coll_id = str(request_auth.get("collectionId", "") or "")
        auth_coll_name = str(request_auth.get("collectionName", "") or "")
        return auth_coll_id == collection_id or auth_coll_name == collection_name

    # Auth collection: add system auth columns (camelCase)
    # A NULL auth value is served as its zero value: v0.22.34
    # models/record.go l.346-354 and l.534-541 (``cast.ToString`` /
    # ``cast.ToBool``), v0.40.6 email and bool fields (see above).
    if is_auth_collection:
        if _can_view_auth_email():
            email = row.get("email")
            result["email"] = "" if email is None else email
        email_visibility = row.get("email_visibility")
        result["emailVisibility"] = (
            False if email_visibility is None else email_visibility
        )
        verified = row.get("verified")
        result["verified"] = False if verified is None else verified

    # Add schema-defined fields
    if schema:
        for field_def in schema:
            fname = field_def.get("name", "")
            if not fname:
                continue
            if is_auth_collection and fname in {
                "id",
                "email",
                "emailVisibility",
                "email_visibility",
                "verified",
                "password",
                "tokenKey",
                "token_key",
            }:
                continue
            # Skip hidden fields unless specifically requested
            if fname in hidden and (fields_filter is None or fname not in fields_filter):
                continue
            # Skip password-type fields always
            if field_def.get("type") == "password":
                continue

            val = row.get(fname)
            if val is None:
                # NULL (or absent column): the PocketBase zero value of the
                # field type, see ``null_field_value``.
                val = null_field_value(field_def)
            elif isinstance(val, datetime):
                val = format_datetime(val)
            # Return integers for whole-number floats (PocketBase compat)
            if isinstance(val, float) and val == int(val):
                val = int(val)
            result[fname] = val
    elif is_view_collection:
        # View collections have no schema — include all row columns.
        system_keys = {"id", "created", "updated"}
        for key, val in row.items():
            if key in system_keys:
                continue
            # Always hide auth internal columns
            if key in _AUTH_HIDDEN_COLUMNS:
                continue
            if isinstance(val, datetime):
                val = format_datetime(val)
            if isinstance(val, float) and val == int(val):
                val = int(val)
            # Map snake_case auth columns to camelCase
            output_key = _AUTH_COLUMN_MAP.get(key, key)
            result[output_key] = val

    # Strip auth-internal columns if they leaked through schema
    for col in _AUTH_HIDDEN_COLUMNS:
        result.pop(col, None)
    # Remove snake_case duplicates of mapped auth columns
    for snake in _AUTH_COLUMN_MAP:
        if snake in result and _AUTH_COLUMN_MAP[snake] in result:
            del result[snake]

    # Apply fields filter if specified
    if fields_filter:
        filter_set = set(fields_filter)
        # Always keep system fields if wildcard or explicitly listed
        if "*" in filter_set:
            pass  # keep everything
        else:
            filtered: dict[str, Any] = {}
            for key in filter_set:
                if key in result:
                    filtered[key] = result[key]
            result = filtered

    return result


def build_list_response(
    items: list[dict[str, Any]],
    page: int,
    per_page: int,
    total_items: int,
) -> dict[str, Any]:
    """Build a paginated list response.

    Args:
        items: List of record response dicts.
        page: Current page number (1-based).
        per_page: Number of items per page.
        total_items: Total count of matching items. ``-1`` if skipped.

    Returns:
        A dict matching PocketBase's paginated list format.
    """
    if total_items < 0:
        total_pages = -1
    elif per_page <= 0:
        total_pages = 0
    else:
        # PocketBase: ceil(totalItems / perPage) (tools/search/provider.go
        # l.254), so an empty result set has totalPages 0, not 1.
        total_pages = math.ceil(total_items / per_page)

    return {
        "page": page,
        "perPage": per_page,
        "totalItems": total_items,
        "totalPages": total_pages,
        "items": items,
    }


# ---------------------------------------------------------------------------
# OAuth2 Models
# ---------------------------------------------------------------------------


class OAuth2AuthRequest(BaseModel):
    """Request model for OAuth2 authentication."""

    provider: str
    code: str
    codeVerifier: str
    redirectUrl: str
    createData: dict[str, Any] = {}


class OAuth2Meta(BaseModel):
    """OAuth2 metadata returned in auth response."""

    id: str
    name: str | None = None
    email: str | None = None
    username: str | None = None
    avatarURL: str | None = None
    isNew: bool
    accessToken: str
    refreshToken: str | None = None
    expiry: float | None = None
