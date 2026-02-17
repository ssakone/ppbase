"""Pydantic models for record API responses.

Provides response models for single records and paginated lists, plus
helpers to build response dicts from raw database rows.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel


def format_datetime(dt: datetime | str | None) -> str:
    """Format a datetime value to PocketBase's string format.

    PocketBase uses ``YYYY-MM-DD HH:MM:SS.mmmZ``.
    """
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


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
) -> dict[str, Any]:
    """Build a record response dict from a raw database row.

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

    Returns:
        A dict suitable for JSON serialization as a record response.
    """
    hidden = hidden_fields or set()

    result: dict[str, Any] = {
        "id": row.get("id", ""),
        "collectionId": collection_id,
        "collectionName": collection_name,
        "created": format_datetime(row.get("created")),
        "updated": format_datetime(row.get("updated")),
    }

    # Auth collection: add system auth columns (camelCase)
    if is_auth_collection:
        result["email"] = row.get("email", "")
        result["emailVisibility"] = row.get("email_visibility", False)
        result["verified"] = row.get("verified", False)

    # Add schema-defined fields
    if schema:
        for field_def in schema:
            fname = field_def.get("name", "")
            if not fname:
                continue
            # Skip hidden fields unless specifically requested
            if fname in hidden and (fields_filter is None or fname not in fields_filter):
                continue
            # Skip password-type fields always
            if field_def.get("type") == "password":
                continue

            val = row.get(fname)
            if isinstance(val, datetime):
                val = format_datetime(val)
            # Return integers for whole-number floats (PocketBase compat)
            if isinstance(val, float) and val == int(val):
                val = int(val)
            result[fname] = val
    else:
        # View collections have no schema — include all row columns
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
        total_pages = max(1, math.ceil(total_items / per_page))

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
