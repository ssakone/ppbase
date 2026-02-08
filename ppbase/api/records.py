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

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ppbase.db.engine import get_engine
from ppbase.services.expand_service import expand_records
from ppbase.services.record_service import (
    _ValidationErrors,
    create_record,
    delete_record,
    get_all_collections,
    get_record,
    list_records,
    resolve_collection,
    update_record,
)

router = APIRouter()


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
) -> JSONResponse:
    """List / search records in a collection."""
    engine = get_engine()

    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    # Build request context for filter macros
    request_context: dict[str, Any] = {
        "auth": {},
        "data": {},
        "query": dict(request.query_params),
    }

    try:
        result = await list_records(
            engine,
            collection,
            page=page,
            per_page=perPage,
            sort=sort,
            filter_str=filter,
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

    try:
        data = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    if not isinstance(data, dict):
        return _error_response(400, "Request body must be a JSON object.")

    try:
        record = await create_record(engine, collection, data)
    except _ValidationErrors as exc:
        return _error_response(400, "Failed to create record.", exc.errors)
    except Exception as exc:
        import logging
        logging.getLogger("ppbase").exception("Record creation failed")
        return _error_response(400, "Failed to create record.", {})

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
    expand: str | None = Query(None),
    fields: str | None = Query(None),
) -> JSONResponse:
    """View a single record."""
    engine = get_engine()

    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    record = await get_record(engine, collection, recordId, fields=fields)
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
# PATCH /api/collections/{collectionIdOrName}/records/{recordId}
# ---------------------------------------------------------------------------


@router.patch("/api/collections/{collectionIdOrName}/records/{recordId}")
async def api_update_record(
    collectionIdOrName: str,
    recordId: str,
    request: Request,
    expand: str | None = Query(None),
    fields: str | None = Query(None),
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

    try:
        data = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    if not isinstance(data, dict):
        return _error_response(400, "Request body must be a JSON object.")

    try:
        record = await update_record(engine, collection, recordId, data)
    except _ValidationErrors as exc:
        return _error_response(400, "Failed to update record.", exc.errors)
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

    all_colls = await get_all_collections(engine)
    deleted = await delete_record(
        engine, collection, recordId, all_collections=all_colls,
    )

    if not deleted:
        return _error_response(404, "The requested resource wasn't found.")

    return JSONResponse(content=None, status_code=204)
