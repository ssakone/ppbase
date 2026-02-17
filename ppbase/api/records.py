"""FastAPI routes for the Records API.

Provides CRUD endpoints for records in dynamic collection tables:
- GET    /api/collections/{collectionIdOrName}/records
- POST   /api/collections/{collectionIdOrName}/records
- GET    /api/collections/{collectionIdOrName}/records/{recordId}
- PATCH  /api/collections/{collectionIdOrName}/records/{recordId}
- DELETE /api/collections/{collectionIdOrName}/records/{recordId}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from ppbase.models.record import build_list_response
from ppbase.services.expand_service import expand_records
from ppbase.services.record_service import (
    _ValidationErrors,
    check_record_rule,
    create_record,
    delete_record,
    get_all_collections,
    get_record,
    list_records,
    resolve_collection,
    update_record,
)
from ppbase.services.rule_engine import check_rule

router = APIRouter()


# ---------------------------------------------------------------------------
# Rule enforcement helpers
# ---------------------------------------------------------------------------


def _prepare_rule_context(
    request: Request,
    token_payload: dict[str, Any] | None,
    data: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build auth context (for rule engine) and request context (for filter macros).

    Returns:
        ``(auth_context, request_context)`` tuple.

    - ``auth_context`` is passed to :func:`check_rule` to determine admin bypass.
    - ``request_context`` is passed to :func:`parse_filter` so that
      ``@request.context``, ``@request.method``, ``@request.headers.*``,
      ``@request.auth.*`` and ``@request.data.*`` macros resolve correctly
      when a rule expression is used as a SQL WHERE filter.
    """
    # Auth context for the rule engine ---
    if token_payload is None:
        auth_ctx: dict[str, Any] | None = None
    elif token_payload.get("type") == "admin":
        auth_ctx = {
            "is_admin": True,
            "@request.auth.id": token_payload.get("id", ""),
            "@request.auth.email": token_payload.get("email", ""),
        }
    else:
        auth_ctx = {
            "is_admin": False,
            "@request.auth.id": token_payload.get("id", ""),
            "@request.auth.collectionId": token_payload.get("collectionId", ""),
            "@request.auth.collectionName": token_payload.get("collectionName", ""),
            "@request.auth.type": token_payload.get("type", ""),
        }

    # Request context for the filter parser (macro resolution) ---
    auth_info: dict[str, Any] = {}
    if token_payload:
        auth_info = {
            "id": token_payload.get("id", ""),
            "email": token_payload.get("email", ""),
            "type": token_payload.get("type", ""),
            "collectionId": token_payload.get("collectionId", ""),
            "collectionName": token_payload.get("collectionName", ""),
        }

    headers_info: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        headers_info[lower] = value
        headers_info[lower.replace("-", "_")] = value

    request_context: dict[str, Any] = {
        "context": "default",
        "method": request.method.upper(),
        "headers": headers_info,
        "auth": auth_info,
        "data": data or {},
        "query": dict(request.query_params),
    }

    return auth_ctx, request_context


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _error_response(status: int, message: str, data: Any = None) -> JSONResponse:
    body: dict[str, Any] = {
        "status": status,
        "message": message,
        "data": data or {},
    }
    return JSONResponse(content=body, status_code=status)


def _validation_message(base: str, errors: dict[str, Any]) -> str:
    """Build an error message that includes the failing field names.

    PocketBase SDK ``ClientResponseError.message`` is set from the top-level
    ``message`` field.  Tests match against it with regexes like ``/email/i``,
    so the field names must appear in the message string.
    """
    if errors:
        field_names = ", ".join(errors.keys())
        return f"{base} Validation failed for: {field_names}."
    return base


# ---------------------------------------------------------------------------
# GET /api/collections/{collectionIdOrName}/records
# ---------------------------------------------------------------------------


@router.get("/api/collections/{collectionIdOrName}/records")
async def api_list_records(
    collectionIdOrName: str,
    request: Request,
    page: int = Query(1),
    perPage: int = Query(30),
    sort: str | None = Query(None),
    filter: str | None = Query(None),
    expand: str | None = Query(None),
    fields: str | None = Query(None),
    skipTotal: bool = Query(False),
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """List / search records in a collection."""
    engine = get_engine()

    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    # --- Rule enforcement ---
    auth_ctx, request_context = _prepare_rule_context(request, auth)
    rule_result = check_rule(collection.list_rule, auth_ctx)

    if rule_result is False:
        # PocketBase returns 403 for locked (null) rules
        return _error_response(
            403,
            "Only superusers can perform this action.",
        )

    # Merge rule expression with user-supplied filter
    effective_filter = filter
    if isinstance(rule_result, str):
        if effective_filter:
            effective_filter = f"({rule_result}) && ({effective_filter})"
        else:
            effective_filter = rule_result

    try:
        result = await list_records(
            engine,
            collection,
            page=page,
            per_page=perPage,
            sort=sort,
            filter_str=effective_filter,
            fields=fields,
            skip_total=skipTotal,
            request_context=request_context,
        )
    except ValueError as exc:
        return _error_response(400, str(exc))

    # Expand relations if requested
    if expand and result["items"]:
        all_colls = await get_all_collections(engine)
        await expand_records(
            engine, collection, result["items"], expand, all_colls,
        )

    return JSONResponse(content=result, status_code=200)


# ---------------------------------------------------------------------------
# POST /api/collections/{collectionIdOrName}/records
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/records")
async def api_create_record(
    collectionIdOrName: str,
    request: Request,
    expand: str | None = Query(None),
    fields: str | None = Query(None),
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """Create a new record in a collection."""
    engine = get_engine()

    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    # _superusers records are managed via the admin auth API, not here
    if collection.name == "_superusers":
        return _error_response(
            400,
            "You cannot create _superusers records via the records API. "
            "Use the admin auth endpoints instead.",
        )

    # Parse body - support both JSON and multipart/form-data
    content_type = request.headers.get("content-type", "")
    data: dict[str, Any] = {}
    files: dict[str, list[tuple[str, bytes]]] = {}

    if "multipart/form-data" in content_type:
        form = await request.form()
        for key in form.keys():
            values = form.getlist(key)
            file_values: list[tuple[str, bytes]] = []
            str_values: list[str] = []
            for v in values:
                if hasattr(v, "read"):  # UploadFile
                    content = await v.read()
                    file_values.append((v.filename or "file", content))
                else:
                    str_values.append(str(v))
            if file_values:
                files[key] = file_values
            elif len(str_values) == 1:
                data[key] = str_values[0]
            elif str_values:
                data[key] = str_values
    else:
        try:
            data = await request.json()
        except Exception:
            return _error_response(400, "Invalid JSON body.")

    if not isinstance(data, dict):
        return _error_response(400, "Request body must be a JSON object.")

    # --- Rule enforcement ---
    auth_ctx, request_context = _prepare_rule_context(request, auth, data=data)
    rule_result = check_rule(collection.create_rule, auth_ctx)

    if rule_result is False:
        # PocketBase returns 403 for locked (null) rules
        return _error_response(
            403,
            "Only superusers can perform this action.",
        )

    try:
        record = await create_record(engine, collection, data, files=files)
    except _ValidationErrors as exc:
        return _error_response(400, _validation_message("Failed to create record.", exc.errors), exc.errors)
    except Exception as exc:
        import logging
        logging.getLogger("ppbase").exception("Record creation failed")
        return _error_response(400, "Failed to create record.", {})

    # If rule is an expression, verify the created record matches it.
    # PocketBase evaluates the rule via a CTE before commit; we check
    # after insert and rollback (delete) on mismatch.
    if isinstance(rule_result, str):
        matches = await check_record_rule(
            engine, collection, record["id"], rule_result, request_context,
        )
        if not matches:
            await delete_record(engine, collection, record["id"])
            return _error_response(
                400,
                "Failed to create record.",
                {"rule": {"code": "validation_rule_failed", "message": "Action not allowed."}},
            )

    # Expand relations if requested
    if expand and record:
        all_colls = await get_all_collections(engine)
        records = await expand_records(
            engine, collection, [record], expand, all_colls,
        )
        record = records[0] if records else record

    return JSONResponse(content=record, status_code=200)


# ---------------------------------------------------------------------------
# GET /api/collections/{collectionIdOrName}/records/{recordId}
# ---------------------------------------------------------------------------


@router.get("/api/collections/{collectionIdOrName}/records/{recordId}")
async def api_get_record(
    collectionIdOrName: str,
    recordId: str,
    request: Request,
    expand: str | None = Query(None),
    fields: str | None = Query(None),
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """View a single record."""
    engine = get_engine()

    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    # --- Rule enforcement ---
    auth_ctx, request_context = _prepare_rule_context(request, auth)
    rule_result = check_rule(collection.view_rule, auth_ctx)

    if rule_result is False:
        # PocketBase returns 403 for locked (null) rules
        return _error_response(
            403,
            "Only superusers can perform this action.",
        )

    record = await get_record(engine, collection, recordId, fields=fields)
    if record is None:
        return _error_response(404, "The requested resource wasn't found.")

    # If rule is an expression, verify the record matches it
    if isinstance(rule_result, str):
        matches = await check_record_rule(
            engine, collection, recordId, rule_result, request_context,
        )
        if not matches:
            return _error_response(404, "The requested resource wasn't found.")

    # Expand relations if requested
    if expand and record:
        all_colls = await get_all_collections(engine)
        records = await expand_records(
            engine, collection, [record], expand, all_colls,
        )
        record = records[0] if records else record

    return JSONResponse(content=record, status_code=200)


# ---------------------------------------------------------------------------
# PATCH /api/collections/{collectionIdOrName}/records/{recordId}
# ---------------------------------------------------------------------------


@router.patch("/api/collections/{collectionIdOrName}/records/{recordId}")
async def api_update_record(
    collectionIdOrName: str,
    recordId: str,
    request: Request,
    expand: str | None = Query(None),
    fields: str | None = Query(None),
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """Update an existing record."""
    engine = get_engine()

    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    # _superusers records are managed via the admin auth API, not here
    if collection.name == "_superusers":
        return _error_response(
            400,
            "You cannot update _superusers records via the records API. "
            "Use the admin auth endpoints instead.",
        )

    # Parse body - support both JSON and multipart/form-data
    content_type = request.headers.get("content-type", "")
    data: dict[str, Any] = {}
    files: dict[str, list[tuple[str, bytes]]] = {}

    if "multipart/form-data" in content_type:
        form = await request.form()
        for key in form.keys():
            values = form.getlist(key)
            file_values: list[tuple[str, bytes]] = []
            str_values: list[str] = []
            for v in values:
                if hasattr(v, "read"):  # UploadFile
                    content = await v.read()
                    file_values.append((v.filename or "file", content))
                else:
                    str_values.append(str(v))
            if file_values:
                files[key] = file_values
            elif len(str_values) == 1:
                data[key] = str_values[0]
            elif str_values:
                data[key] = str_values
    else:
        try:
            data = await request.json()
        except Exception:
            return _error_response(400, "Invalid JSON body.")

    if not isinstance(data, dict):
        return _error_response(400, "Request body must be a JSON object.")

    # --- Rule enforcement ---
    auth_ctx, request_context = _prepare_rule_context(request, auth, data=data)
    rule_result = check_rule(collection.update_rule, auth_ctx)

    if rule_result is False:
        # PocketBase returns 403 for locked (null) rules
        return _error_response(
            403,
            "Only superusers can perform this action.",
        )

    # If rule is an expression, verify the existing record matches it
    if isinstance(rule_result, str):
        matches = await check_record_rule(
            engine, collection, recordId, rule_result, request_context,
        )
        if not matches:
            return _error_response(404, "The requested resource wasn't found.")

    try:
        record = await update_record(engine, collection, recordId, data, files=files)
    except _ValidationErrors as exc:
        return _error_response(400, _validation_message("Failed to update record.", exc.errors), exc.errors)
    except Exception as exc:
        import logging
        logging.getLogger("ppbase").exception("Record update failed")
        return _error_response(400, "Failed to update record.", {})

    if record is None:
        return _error_response(404, "The requested resource wasn't found.")

    # Expand relations if requested
    if expand and record:
        all_colls = await get_all_collections(engine)
        records = await expand_records(
            engine, collection, [record], expand, all_colls,
        )
        record = records[0] if records else record

    return JSONResponse(content=record, status_code=200)


# ---------------------------------------------------------------------------
# DELETE /api/collections/{collectionIdOrName}/records/{recordId}
# ---------------------------------------------------------------------------


@router.delete("/api/collections/{collectionIdOrName}/records/{recordId}")
async def api_delete_record(
    collectionIdOrName: str,
    recordId: str,
    request: Request,
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """Delete a record."""
    engine = get_engine()

    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    # _superusers records are managed via the admin auth API, not here
    if collection.name == "_superusers":
        return _error_response(
            400,
            "You cannot delete _superusers records via the records API. "
            "Use the admin auth endpoints instead.",
        )

    # --- Rule enforcement ---
    auth_ctx, request_context = _prepare_rule_context(request, auth)
    rule_result = check_rule(collection.delete_rule, auth_ctx)

    if rule_result is False:
        # PocketBase returns 403 for locked (null) rules
        return _error_response(
            403,
            "Only superusers can perform this action.",
        )

    # If rule is an expression, verify the existing record matches it
    if isinstance(rule_result, str):
        matches = await check_record_rule(
            engine, collection, recordId, rule_result, request_context,
        )
        if not matches:
            return _error_response(404, "The requested resource wasn't found.")

    all_colls = await get_all_collections(engine)
    deleted = await delete_record(
        engine, collection, recordId, all_collections=all_colls,
    )

    if not deleted:
        return _error_response(404, "The requested resource wasn't found.")

    return JSONResponse(content=None, status_code=204)
