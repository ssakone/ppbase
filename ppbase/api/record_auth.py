"""FastAPI routes for auth collection endpoints.

Provides authentication endpoints for collections of type ``auth``:
- GET    /api/collections/{collection}/auth-methods
- POST   /api/collections/{collection}/auth-with-password
- POST   /api/collections/{collection}/auth-refresh
- POST   /api/collections/{collection}/request-verification
- POST   /api/collections/{collection}/confirm-verification
- POST   /api/collections/{collection}/request-password-reset
- POST   /api/collections/{collection}/confirm-password-reset
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from ppbase.services.expand_service import expand_records
from ppbase.services.record_service import get_all_collections, resolve_collection

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_response(status: int, message: str, data: Any = None) -> JSONResponse:
    body: dict[str, Any] = {
        "status": status,
        "message": message,
        "data": data or {},
    }
    return JSONResponse(content=body, status_code=status)


def _require_auth_collection(collection):
    """Return an error response if the collection is not type ``auth``."""
    col_type = getattr(collection, "type", "base") or "base"
    if col_type != "auth":
        return _error_response(
            404,
            f"The collection \"{collection.name}\" is not an auth collection.",
        )
    return None


def _apply_fields_filter(record: dict[str, Any], fields_param: str) -> dict[str, Any]:
    """Filter record keys to only those listed in the ``fields`` query param."""
    if not fields_param:
        return record
    allowed = {f.strip() for f in fields_param.split(",") if f.strip()}
    if not allowed or "*" in allowed:
        return record
    return {k: v for k, v in record.items() if k in allowed}


# ---------------------------------------------------------------------------
# GET auth-methods
# ---------------------------------------------------------------------------


@router.get("/api/collections/{collectionIdOrName}/auth-methods")
async def api_auth_methods(collectionIdOrName: str) -> JSONResponse:
    """Return available auth methods for the collection."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    # Read identity fields from collection options
    opts = collection.options or {}
    pa = opts.get("passwordAuth", {})
    identity_fields = pa.get("identityFields", ["email"])
    enabled = pa.get("enabled", True)

    # Determine legacy compatibility flags
    email_password = enabled and "email" in identity_fields
    username_password = enabled and "username" in identity_fields

    return JSONResponse(
        content={
            # PocketBase SDK v0.x compatibility fields
            "usernamePassword": username_password,
            "emailPassword": email_password,
            "authProviders": [],
            # Newer structured format
            "password": {
                "enabled": enabled,
                "identityFields": identity_fields,
            },
            "oauth2": {
                "enabled": False,
                "providers": [],
            },
        }
    )


# ---------------------------------------------------------------------------
# POST auth-with-password
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/auth-with-password")
async def api_auth_with_password(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Authenticate a user with identity + password."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    # Block _superusers — they use the admin auth API
    if collection.name == "_superusers":
        return _error_response(
            404,
            "Use the admin auth endpoints for _superusers.",
        )

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    identity = body.get("identity", "")
    password = body.get("password", "")
    identity_field = body.get("identityField")

    if not identity or not password:
        return _error_response(
            400,
            "Failed to authenticate.",
            {
                "identity": {"code": "validation_required", "message": "Cannot be blank."}
                if not identity
                else {},
                "password": {"code": "validation_required", "message": "Cannot be blank."}
                if not password
                else {},
            },
        )

    settings = request.app.state.settings

    from ppbase.services.record_auth_service import auth_with_password

    result = await auth_with_password(
        engine,
        collection,
        identity,
        password,
        settings,
        identity_field=identity_field,
    )

    if result is None:
        return _error_response(
            400,
            "Failed to authenticate.",
            {
                "identity": {
                    "code": "validation_invalid_credentials",
                    "message": "Invalid login credentials.",
                }
            },
        )

    # Apply expand if requested
    expand_str = request.query_params.get("expand", "")
    if expand_str and result.get("record"):
        all_colls = await get_all_collections(engine)
        records = await expand_records(
            engine, collection, [result["record"]], expand_str, all_colls,
        )
        result["record"] = records[0] if records else result["record"]

    # Apply fields filter if requested
    fields_param = request.query_params.get("fields", "")
    if fields_param and result.get("record"):
        result["record"] = _apply_fields_filter(result["record"], fields_param)

    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# POST auth-refresh
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/auth-refresh")
async def api_auth_refresh(
    collectionIdOrName: str,
    request: Request,
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """Refresh an existing auth token."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    # Extract raw token for full verification
    auth_header = request.headers.get("Authorization", "")
    token_str = auth_header
    if token_str.lower().startswith("bearer "):
        token_str = token_str[7:]
    token_str = token_str.strip()

    if not token_str:
        return _error_response(401, "The request requires valid auth token to be set.")

    settings = request.app.state.settings

    from ppbase.services.record_auth_service import (
        auth_refresh,
        verify_record_auth_token,
    )

    payload = await verify_record_auth_token(engine, collection, token_str, settings)
    if payload is None:
        return _error_response(401, "The request requires valid auth token to be set.")

    result = await auth_refresh(engine, collection, payload, settings)
    if result is None:
        return _error_response(401, "The request requires valid auth token to be set.")

    # Apply expand if requested
    expand_str = request.query_params.get("expand", "")
    if expand_str and result.get("record"):
        all_colls = await get_all_collections(engine)
        records = await expand_records(
            engine, collection, [result["record"]], expand_str, all_colls,
        )
        result["record"] = records[0] if records else result["record"]

    # Apply fields filter if requested
    fields_param = request.query_params.get("fields", "")
    if fields_param and result.get("record"):
        result["record"] = _apply_fields_filter(result["record"], fields_param)

    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# POST request-verification
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/request-verification")
async def api_request_verification(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Request a verification email."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    email = body.get("email", "").strip()
    if not email:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"email": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    settings = request.app.state.settings
    base_url = str(request.base_url).rstrip("/")

    from ppbase.services.record_auth_service import request_verification as _req_verif

    await _req_verif(engine, collection, email, settings, base_url=base_url)

    # Always 204 to avoid enumeration
    return JSONResponse(content=None, status_code=204)


# ---------------------------------------------------------------------------
# POST confirm-verification
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/confirm-verification")
async def api_confirm_verification(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Confirm a verification token."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    token = body.get("token", "").strip()
    if not token:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"token": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    settings = request.app.state.settings

    from ppbase.services.record_auth_service import confirm_verification as _confirm_verif

    ok = await _confirm_verif(engine, collection, token, settings)
    if not ok:
        return _error_response(
            400,
            "Failed to confirm verification.",
            {"token": {"code": "validation_invalid_token", "message": "Invalid or expired token."}},
        )

    return JSONResponse(content=None, status_code=204)


# ---------------------------------------------------------------------------
# POST request-password-reset
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/request-password-reset")
async def api_request_password_reset(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Request a password reset email."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    email = body.get("email", "").strip()
    if not email:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"email": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    settings = request.app.state.settings
    base_url = str(request.base_url).rstrip("/")

    from ppbase.services.record_auth_service import request_password_reset as _req_reset

    await _req_reset(engine, collection, email, settings, base_url=base_url)

    return JSONResponse(content=None, status_code=204)


# ---------------------------------------------------------------------------
# POST confirm-password-reset
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/confirm-password-reset")
async def api_confirm_password_reset(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Confirm a password reset token and set a new password."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    token = body.get("token", "").strip()
    password = body.get("password", "")
    password_confirm = body.get("passwordConfirm", "")

    if not token:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"token": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    settings = request.app.state.settings

    from ppbase.services.record_auth_service import confirm_password_reset as _confirm_reset

    ok, errors = await _confirm_reset(
        engine, collection, token, password, password_confirm, settings
    )

    if not ok:
        return _error_response(
            400,
            "Failed to confirm password reset.",
            errors or {},
        )

    return JSONResponse(content=None, status_code=204)
