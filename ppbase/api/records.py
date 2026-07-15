"""FastAPI routes for the Records API.

Provides CRUD endpoints for records in dynamic collection tables:
- GET    /api/collections/{collectionIdOrName}/records
- POST   /api/collections/{collectionIdOrName}/records
- GET    /api/collections/{collectionIdOrName}/records/{recordId}
- PATCH  /api/collections/{collectionIdOrName}/records/{recordId}
- DELETE /api/collections/{collectionIdOrName}/records/{recordId}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.datastructures import Headers, QueryParams, URL

from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from ppbase.ext.events import RecordRequestEvent
from ppbase.ext.registry import (
    HOOK_RECORD_CREATE_REQUEST,
    HOOK_RECORD_DELETE_REQUEST,
    HOOK_RECORDS_LIST_REQUEST,
    HOOK_RECORD_UPDATE_REQUEST,
    HOOK_RECORD_VIEW_REQUEST,
    get_extension_registry,
)
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
logger = logging.getLogger(__name__)


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


# Auth collection managed fields during create that require manageRule
# (or superuser).
_AUTH_MANAGED_CREATE_FIELDS = frozenset(
    {
        "emailVisibility",
        "email_visibility",
        "verified",
    }
)

# Auth collection managed fields during update that require manageRule
# (or superuser), except self password update.
_AUTH_MANAGED_UPDATE_FIELDS = frozenset(
    {
        "email",
        "emailVisibility",
        "email_visibility",
        "verified",
        "password",
        "passwordConfirm",
    }
)
_AUTH_SELF_ALLOWED_UPDATE_FIELDS = frozenset({"password", "passwordConfirm"})

_DEFAULT_BATCH_ENABLED = True
_DEFAULT_BATCH_MAX_REQUESTS = 50
_DEFAULT_BATCH_MAX_BODY_SIZE = 0
_DEFAULT_BATCH_TIMEOUT = 3

_BATCH_RECORDS_RE = re.compile(r"^/api/collections/([^/]+)/records/?$")
_BATCH_RECORD_RE = re.compile(r"^/api/collections/([^/]+)/records/([^/]+)/?$")
_BATCH_FILE_KEY_RE = re.compile(r"^requests(?:\.|\[)(\d+)\]?\.([A-Za-z0-9_]+)$")


class _BatchRequestFailed(Exception):
    """Signals a failed nested request inside ``/api/batch``."""

    def __init__(self, index: int, response: dict[str, Any]):
        super().__init__("Batch request failed.")
        self.index = index
        self.response = response


class _ConnectionEngineAdapter:
    """Minimal async-engine adapter that pins all work to one connection."""

    def __init__(self, conn: AsyncConnection):
        self._conn = conn

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        yield self._conn

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncConnection]:
        yield self._conn


class _AbortWithResponse(Exception):
    """Rollback marker carrying an HTTP response."""

    def __init__(self, response: Response):
        super().__init__("Abort request with HTTP response.")
        self.response = response


def _cleanup_created_record_files(
    created_targets: set[tuple[str, str, str]],
) -> None:
    """Remove only files created by a failed batch, then prune empty dirs."""
    from ppbase.core.storage_safety import StorageSafetyError
    from ppbase.services.file_storage import (
        delete_files,
        delete_storage_dir_if_empty,
    )

    record_targets: set[tuple[str, str]] = set()
    for collection_id, record_id, filename in created_targets:
        record_targets.add((collection_id, record_id))
        try:
            delete_files(collection_id, record_id, [filename])
        except (OSError, StorageSafetyError):
            logger.warning("Skipped unsafe or failed batch storage cleanup")

    for collection_id, record_id in record_targets:
        try:
            delete_storage_dir_if_empty(collection_id, record_id)
        except (OSError, StorageSafetyError):
            logger.warning("Skipped unsafe or failed empty storage-dir cleanup")


def _raw_batch_file_references(value: Any) -> list[str]:
    """Return stored file references from a raw PostgreSQL field value."""
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, list):
                return [str(item) for item in decoded if item]
        return [value]
    if value:
        return [str(value)]
    return []


async def _final_batch_file_references(
    engine: Any,
    all_collections: list[Any],
    targets: set[tuple[str, str]],
) -> set[tuple[str, str, str]]:
    """Read final in-transaction file references before committing a batch."""
    collections_by_id = {
        str(getattr(collection, "id", "") or ""): collection
        for collection in all_collections
    }
    preserved: set[tuple[str, str, str]] = set()

    async with engine.connect() as conn:
        for collection_id, record_id in sorted(targets):
            collection = collections_by_id.get(collection_id)
            if collection is None:
                raise RuntimeError("Missing collection during batch file reconciliation.")
            table_name = str(getattr(collection, "name", "") or "")
            quoted_table = table_name.replace('"', '""')
            result = await conn.execute(
                text(f'SELECT * FROM "{quoted_table}" WHERE "id" = :rid LIMIT 1'),
                {"rid": record_id},
            )
            row = result.mappings().first()
            if row is None:
                continue
            row_dict = dict(row)
            for raw_field in getattr(collection, "schema", None) or []:
                if not isinstance(raw_field, dict) or raw_field.get("type") != "file":
                    continue
                field_name = str(raw_field.get("name", "") or "")
                if not field_name:
                    continue
                preserved.update(
                    (collection_id, record_id, filename)
                    for filename in _raw_batch_file_references(
                        row_dict.get(field_name)
                    )
                )
    return preserved


def _affected_storage_records(
    created_file_targets: set[tuple[str, str, str]],
    deferred_storage_deletes: list[tuple[str, str, str, tuple[str, ...]]],
) -> set[tuple[str, str]]:
    """Return record identities touched by coordinated storage changes."""
    affected = {
        (collection_id, record_id)
        for collection_id, record_id, _filename in created_file_targets
    }
    affected.update(
        (collection_id, record_id)
        for _action, collection_id, record_id, _filenames
        in deferred_storage_deletes
    )
    return affected


async def _reconcile_storage_file_references(
    engine: Any,
    targets: set[tuple[str, str]],
) -> set[tuple[str, str, str]] | None:
    """Read durable file references after an ambiguous commit outcome."""
    if not targets:
        return set()
    try:
        all_collections = await get_all_collections(engine)
        return await _final_batch_file_references(
            engine,
            all_collections,
            targets,
        )
    except Exception:
        logger.exception(
            "Unable to reconcile durable storage references after an "
            "ambiguous database commit"
        )
        return None


def _cleanup_unreferenced_created_files(
    created_file_targets: set[tuple[str, str, str]],
    final_file_references: set[tuple[str, str, str]],
) -> None:
    orphaned_writes = created_file_targets - final_file_references
    if orphaned_writes:
        _cleanup_created_record_files(orphaned_writes)


async def _run_storage_transaction(
    engine: Any,
    operation: Callable[[_ConnectionEngineAdapter], Awaitable[Any]],
) -> Any:
    """Coordinate DB commit with exact file-write/delete compensation."""
    created_file_targets: set[tuple[str, str, str]] = set()
    deferred_storage_deletes: list[
        tuple[str, str, str, tuple[str, ...]]
    ] = []
    final_file_references: set[tuple[str, str, str]] = set()
    transaction_body_finished = False

    from ppbase.services.file_storage import (
        capture_storage_writes,
        defer_storage_deletes,
        flush_deferred_storage_deletes,
        pin_storage_config,
    )

    with pin_storage_config():
        try:
            with (
                capture_storage_writes(created_file_targets),
                defer_storage_deletes(deferred_storage_deletes),
            ):
                async with engine.begin() as conn:
                    active_engine = _ConnectionEngineAdapter(conn)
                    result = await operation(active_engine)
                    if isinstance(result, Response) and result.status_code >= 400:
                        raise _AbortWithResponse(result)
                    affected_records = _affected_storage_records(
                        created_file_targets,
                        deferred_storage_deletes,
                    )
                    if affected_records:
                        all_collections = await get_all_collections(active_engine)
                        final_file_references = await _final_batch_file_references(
                            active_engine,
                            all_collections,
                            affected_records,
                        )
                    transaction_body_finished = True
        except BaseException as exc:
            if transaction_body_finished and isinstance(exc, Exception):
                durable_references = await _reconcile_storage_file_references(
                    engine,
                    _affected_storage_records(
                        created_file_targets,
                        deferred_storage_deletes,
                    ),
                )
                if durable_references is not None:
                    _cleanup_unreferenced_created_files(
                        created_file_targets,
                        durable_references,
                    )
                elif created_file_targets:
                    logger.warning(
                        "Preserving %d new storage file(s) because the database "
                        "commit outcome could not be reconciled",
                        len(created_file_targets),
                    )
                if deferred_storage_deletes:
                    logger.warning(
                        "Preserving %d deferred storage deletion(s) after an "
                        "ambiguous database commit; a janitor may reconcile them",
                        len(deferred_storage_deletes),
                    )
            elif transaction_body_finished:
                logger.warning(
                    "Preserving storage changes because database commit was "
                    "interrupted before its outcome could be reconciled"
                )
            elif created_file_targets:
                _cleanup_created_record_files(created_file_targets)
            raise

        _cleanup_unreferenced_created_files(
            created_file_targets,
            final_file_references,
        )
        cleanup_failures = flush_deferred_storage_deletes(
            deferred_storage_deletes,
            preserved_files=final_file_references,
        )
        if cleanup_failures:
            logger.warning(
                "Skipped %d unsafe or conflicting committed storage cleanup(s)",
                cleanup_failures,
            )
        return result


class _BatchRequestProxy:
    """Request-like object exposing a batch subrequest to record hooks and rules."""

    def __init__(
        self,
        base_request: Request,
        *,
        method: str,
        raw_url: str,
        query: dict[str, str],
        headers: Headers,
    ) -> None:
        self._base_request = base_request
        self.method = method
        self.headers = headers
        self.query_params = QueryParams(query)
        self.url = _build_batch_url(base_request, raw_url)
        self.scope = _build_batch_scope(base_request, method, raw_url, headers)
        self.path_params: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base_request, name)


async def _trigger_record_request_hook(
    request: Request,
    hook_name: str,
    event: RecordRequestEvent,
    default_handler,
):
    extensions = get_extension_registry(request.app)
    if extensions is None:
        return await default_handler(event)
    hook = extensions.hooks.get(hook_name)
    return await hook.trigger(event, default_handler)


def _is_self_auth_target(
    auth_payload: dict[str, Any] | None,
    collection: Any,
    record_id: str,
) -> bool:
    if not auth_payload:
        return False
    if auth_payload.get("type") != "authRecord":
        return False
    return (
        auth_payload.get("collectionId") == getattr(collection, "id", "")
        and auth_payload.get("id") == record_id
    )


async def _has_manage_access_for_auth_record(
    engine: Any,
    collection: Any,
    record_id: str,
    auth_ctx: dict[str, Any] | None,
    request_context: dict[str, Any],
) -> bool:
    """Evaluate auth collection ``options.manageRule`` for a target record."""
    if auth_ctx and auth_ctx.get("is_admin"):
        return True

    options = getattr(collection, "options", None) or {}
    manage_rule = options.get("manageRule")
    rule_result = check_rule(manage_rule, auth_ctx)
    if rule_result is True:
        return True
    if rule_result is False:
        return False
    return await check_record_rule(
        engine,
        collection,
        record_id,
        str(rule_result),
        request_context,
    )


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


def _response_json_body(response: JSONResponse) -> dict[str, Any]:
    """Decode a JSONResponse payload to dict."""
    try:
        raw = response.body.decode("utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {
        "status": response.status_code,
        "message": "Unexpected error.",
        "data": {},
    }


def _response_body(response: Response) -> Any:
    """Decode a route response body for batch result embedding."""
    body = getattr(response, "body", b"")
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return body.decode("utf-8", errors="replace")


def _http_exception_response(exc: HTTPException) -> JSONResponse:
    """Convert FastAPI HTTPException raised by hooks into a PPBase-style response."""
    if isinstance(exc.detail, dict):
        content = dict(exc.detail)
        content.setdefault("status", exc.status_code)
        content.setdefault("message", "")
        content.setdefault("data", {})
    else:
        content = {
            "status": exc.status_code,
            "message": str(exc.detail or ""),
            "data": {},
        }
    return JSONResponse(content=content, status_code=exc.status_code)


def _batch_result_from_response(response: Any) -> tuple[int, Any] | JSONResponse:
    """Convert a nested record route response into a batch item or error response."""
    if isinstance(response, Response):
        if response.status_code >= 400:
            if isinstance(response, JSONResponse):
                return response
            return _error_response(
                response.status_code,
                str(_response_body(response) or "Batch request failed."),
            )
        return response.status_code, _response_body(response)
    return 200, response


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _coerce_non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except Exception:
        return default


def _coerce_positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


async def _parse_record_request_body(
    request: Request,
) -> tuple[
    dict[str, Any] | None,
    dict[str, list[tuple[str, bytes]]],
    JSONResponse | None,
]:
    """Parse a record create/update body from JSON or multipart form data.

    PocketBase clients send regular fields as a serialized JSON object under
    ``@jsonPayload`` when the request also contains uploaded files.
    """
    content_type = request.headers.get("content-type", "")
    data: dict[str, Any] = {}
    files: dict[str, list[tuple[str, bytes]]] = {}

    if "multipart/form-data" in content_type:
        form = await request.form()
        json_payloads: list[str] = []

        for key in form.keys():
            values = form.getlist(key)
            if key == "@jsonPayload":
                json_payloads.extend(
                    str(value) for value in values if not hasattr(value, "read")
                )
                continue

            file_values: list[tuple[str, bytes]] = []
            str_values: list[str] = []
            for value in values:
                if hasattr(value, "read"):  # UploadFile
                    content = await value.read()
                    file_values.append((value.filename or "file", content))
                else:
                    str_values.append(str(value))

            if file_values:
                files[key] = file_values
            if len(str_values) == 1:
                data[key] = str_values[0]
            elif str_values:
                data[key] = str_values

        for raw_payload in json_payloads:
            try:
                payload = json.loads(raw_payload)
            except Exception:
                return None, files, _error_response(400, "Invalid JSON body.")
            if not isinstance(payload, dict):
                return (
                    None,
                    files,
                    _error_response(
                        400,
                        "Request body must be a JSON object.",
                    ),
                )
            data.update(payload)

        return data, files, None

    try:
        payload = await request.json()
    except Exception:
        return None, files, _error_response(400, "Invalid JSON body.")

    if not isinstance(payload, dict):
        return (
            None,
            files,
            _error_response(
                400,
                "Request body must be a JSON object.",
            ),
        )

    return payload, files, None


async def _get_batch_settings(engine: Any) -> tuple[bool, int, int, float]:
    """Return ``(enabled, max_requests, max_body_size, timeout_seconds)`` from app settings."""
    enabled = _DEFAULT_BATCH_ENABLED
    max_requests = _DEFAULT_BATCH_MAX_REQUESTS
    max_body_size = _DEFAULT_BATCH_MAX_BODY_SIZE
    timeout_seconds = float(_DEFAULT_BATCH_TIMEOUT)

    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text('SELECT "value" FROM "_params" WHERE "key" = :key LIMIT 1'),
                {"key": "settings"},
            )
            row = result.mappings().first()
    except Exception:
        return enabled, max_requests, max_body_size, timeout_seconds

    settings_value = row.get("value") if row else None
    if isinstance(settings_value, dict):
        batch_value = settings_value.get("batch")
        if isinstance(batch_value, dict):
            if "enabled" in batch_value:
                enabled = bool(batch_value.get("enabled"))
            max_requests = _coerce_positive_int(
                batch_value.get("maxRequests", max_requests),
                max_requests,
            )
            max_body_size = _coerce_non_negative_int(
                batch_value.get("maxBodySize", max_body_size),
                max_body_size,
            )
            timeout_seconds = _coerce_positive_float(
                batch_value.get("timeout", timeout_seconds),
                timeout_seconds,
            )

    return enabled, max_requests, max_body_size, timeout_seconds


async def _parse_batch_payload(
    request: Request,
) -> tuple[
    dict[str, Any] | None,
    dict[int, dict[str, list[tuple[str, bytes]]]],
    JSONResponse | None,
]:
    """Parse batch request body from JSON or multipart payload."""
    content_type = request.headers.get("content-type", "")
    files_by_request: dict[int, dict[str, list[tuple[str, bytes]]]] = {}

    if "multipart/form-data" in content_type:
        form = await request.form()
        raw_payload = form.get("@jsonPayload")
        if raw_payload is None:
            return (
                None,
                files_by_request,
                _error_response(400, "Invalid batch request body."),
            )

        try:
            payload = json.loads(str(raw_payload))
        except Exception:
            return (
                None,
                files_by_request,
                _error_response(400, "Invalid batch request body."),
            )

        for key in form.keys():
            if key == "@jsonPayload":
                continue
            match = _BATCH_FILE_KEY_RE.match(key)
            if match is None:
                continue

            req_index = int(match.group(1))
            field_name = match.group(2)
            for value in form.getlist(key):
                if not hasattr(value, "read"):
                    continue
                content = await value.read()
                req_files = files_by_request.setdefault(req_index, {})
                req_files.setdefault(field_name, []).append(
                    (value.filename or "file", content),
                )

        return payload, files_by_request, None

    try:
        payload = await request.json()
    except Exception:
        return None, files_by_request, _error_response(400, "Invalid JSON body.")

    return payload, files_by_request, None


def _parse_batch_target(
    batch_request: dict[str, Any],
) -> tuple[str, str, str, str | None, dict[str, str]]:
    """Parse and validate a batch subrequest target.

    Returns:
        ``(action, method, collection_id_or_name, record_id, query_params)``
    """
    method = str(batch_request.get("method", "")).upper().strip()
    raw_url = batch_request.get("url")
    if not method or not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Invalid batch request")

    parsed_url = urlsplit(raw_url)
    path = parsed_url.path
    query_params = {
        key: values[-1] if values else ""
        for key, values in parse_qs(parsed_url.query, keep_blank_values=True).items()
    }

    records_match = _BATCH_RECORDS_RE.match(path)
    if records_match:
        collection = unquote(records_match.group(1))
        if method == "POST":
            return "create", method, collection, None, query_params
        if method == "PUT":
            return "upsert", method, collection, None, query_params
        raise ValueError("Unsupported batch request method")

    record_match = _BATCH_RECORD_RE.match(path)
    if record_match:
        collection = unquote(record_match.group(1))
        record_id = unquote(record_match.group(2))
        if method == "PATCH":
            return "update", method, collection, record_id, query_params
        if method == "DELETE":
            return "delete", method, collection, record_id, query_params
        raise ValueError("Unsupported batch request method")

    raise ValueError("Unsupported batch request path")


def _build_batch_headers(request: Request, batch_headers: Any) -> Headers:
    if batch_headers is None:
        batch_headers = {}
    if not isinstance(batch_headers, dict):
        raise ValueError("Invalid batch request headers")

    merged = {str(key).lower(): str(value) for key, value in request.headers.items()}
    for key, value in batch_headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Invalid batch request headers")
        if key.lower() == "authorization":
            continue
        merged[key.lower()] = value

    # PocketBase rebuilds batch subrequests as multipart form requests.
    merged["content-type"] = "multipart/form-data"
    return Headers(headers=merged)


def _build_batch_url(request: Request, raw_url: str) -> URL:
    parsed_url = urlsplit(raw_url)
    if parsed_url.scheme and parsed_url.netloc:
        return URL(raw_url)

    base_url = str(getattr(request, "base_url", "") or "").rstrip("/")
    path = raw_url if raw_url.startswith("/") else f"/{raw_url}"
    if base_url:
        return URL(f"{base_url}{path}")
    return URL(path)


def _batch_query_suffix(raw_url: str) -> str:
    raw_query = urlsplit(raw_url).query
    return f"?{raw_query}" if raw_query else ""


def _batch_records_url(collection_name: str, query_suffix: str) -> str:
    collection = quote(collection_name, safe="")
    return f"/api/collections/{collection}/records{query_suffix}"


def _batch_record_url(collection_name: str, record_id: str, query_suffix: str) -> str:
    collection = quote(collection_name, safe="")
    record = quote(record_id, safe="")
    return f"/api/collections/{collection}/records/{record}{query_suffix}"


def _build_batch_scope(
    request: Request,
    method: str,
    raw_url: str,
    headers: Headers,
) -> dict[str, Any]:
    parsed_url = urlsplit(raw_url)
    path = parsed_url.path or "/"
    scope = dict(request.scope)
    scope["method"] = method
    scope["path"] = path
    scope["raw_path"] = path.encode("latin-1", errors="ignore")
    scope["query_string"] = parsed_url.query.encode("latin-1", errors="ignore")
    scope["headers"] = [
        (
            str(key).lower().encode("latin-1", errors="ignore"),
            str(value).encode("latin-1", errors="ignore"),
        )
        for key, value in headers.items()
    ]
    return scope


def _build_batch_request(
    request: Request,
    method: str,
    raw_url: str,
    query: dict[str, str],
    headers: Any,
) -> _BatchRequestProxy:
    return _BatchRequestProxy(
        request,
        method=method,
        raw_url=raw_url,
        query=query,
        headers=_build_batch_headers(request, headers),
    )


def _build_batch_rule_context(
    request: Request,
    auth: dict[str, Any] | None,
    method: str,
    data: dict[str, Any],
    query: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    auth_ctx, request_context = _prepare_rule_context(request, auth, data=data)
    request_context["method"] = method
    request_context["context"] = "batch"
    request_context["query"] = query
    request_context["data"] = data
    return auth_ctx, request_context


async def _apply_batch_create(
    engine: Any,
    collection: Any,
    request: Request,
    auth: dict[str, Any] | None,
    data: dict[str, Any],
    files: dict[str, list[tuple[str, bytes]]],
    query: dict[str, str],
    all_collections: list[Any],
) -> tuple[int, Any] | JSONResponse:
    if collection.name == "_superusers":
        return _error_response(
            400,
            "You cannot create _superusers records via the records API. "
            "Use the admin auth endpoints instead.",
        )

    event = RecordRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=getattr(collection, "name", ""),
        auth=auth,
        data=dict(data),
        files=dict(files),
        expand=query.get("expand"),
        fields=query.get("fields"),
        engine=engine,
    )

    async def _default_batch_create_handler(e: RecordRequestEvent) -> JSONResponse:
        if not isinstance(e.data, dict):
            return _error_response(400, "Request body must be a JSON object.")

        payload = dict(e.data)
        payload_files = dict(e.files)
        auth_ctx, request_context = _build_batch_rule_context(
            request,
            e.auth,
            "POST",
            payload,
            query,
        )
        rule_result = check_rule(collection.create_rule, auth_ctx)
        col_type = getattr(collection, "type", "base") or "base"
        managed_create_keys: set[str] = set()
        if col_type == "auth":
            managed_create_keys = {
                k for k in payload.keys() if k in _AUTH_MANAGED_CREATE_FIELDS
            }

        if rule_result is False:
            return _error_response(403, "Only superusers can perform this action.")

        try:
            record = await create_record(
                engine, collection, payload, files=payload_files
            )
        except _ValidationErrors as exc:
            return _error_response(
                400,
                _validation_message("Failed to create record.", exc.errors),
                exc.errors,
            )
        except Exception:
            return _error_response(400, "Failed to create record.", {})

        if isinstance(rule_result, str):
            matches = await check_record_rule(
                engine,
                collection,
                record["id"],
                rule_result,
                request_context,
            )
            if not matches:
                raise _AbortWithResponse(
                    _error_response(
                        400,
                        "Failed to create record.",
                        {
                            "rule": {
                                "code": "validation_rule_failed",
                                "message": "Action not allowed.",
                            }
                        },
                    )
                )

        if managed_create_keys:
            has_manage_access = await _has_manage_access_for_auth_record(
                engine,
                collection,
                record["id"],
                auth_ctx,
                request_context,
            )
            if not has_manage_access:
                raise _AbortWithResponse(
                    _error_response(
                        403,
                        "The authorized record model is not allowed to perform this action.",
                    )
                )

        if e.fields and record:
            selected_record = await get_record(
                engine,
                collection,
                record["id"],
                fields=e.fields,
                request_context=request_context,
            )
            if selected_record is not None:
                record = selected_record

        if e.expand and record:
            expanded_records = await expand_records(
                engine,
                collection,
                [record],
                e.expand,
                all_collections,
                request_context=request_context,
            )
            record = expanded_records[0] if expanded_records else record

        return JSONResponse(content=record, status_code=200)

    try:
        response = await _trigger_record_request_hook(
            request,
            HOOK_RECORD_CREATE_REQUEST,
            event,
            _default_batch_create_handler,
        )
    except _AbortWithResponse as abort:
        return abort.response
    except HTTPException as exc:
        return _http_exception_response(exc)

    return _batch_result_from_response(response)


async def _apply_batch_update(
    engine: Any,
    collection: Any,
    record_id: str,
    request: Request,
    auth: dict[str, Any] | None,
    data: dict[str, Any],
    files: dict[str, list[tuple[str, bytes]]],
    query: dict[str, str],
    all_collections: list[Any],
) -> tuple[int, Any] | JSONResponse:
    if collection.name == "_superusers":
        return _error_response(
            400,
            "You cannot update _superusers records via the records API. "
            "Use the admin auth endpoints instead.",
        )

    event = RecordRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=getattr(collection, "name", ""),
        record_id=record_id,
        auth=auth,
        data=dict(data),
        files=dict(files),
        expand=query.get("expand"),
        fields=query.get("fields"),
        engine=engine,
    )

    async def _default_batch_update_handler(e: RecordRequestEvent) -> JSONResponse:
        if not isinstance(e.data, dict):
            return _error_response(400, "Request body must be a JSON object.")

        payload = dict(e.data)
        payload_files = dict(e.files)
        target_record_id = e.record_id or record_id
        auth_ctx, request_context = _build_batch_rule_context(
            request,
            e.auth,
            "PATCH",
            payload,
            query,
        )
        rule_result = check_rule(collection.update_rule, auth_ctx)
        if rule_result is False:
            return _error_response(403, "Only superusers can perform this action.")

        if isinstance(rule_result, str):
            matches = await check_record_rule(
                engine,
                collection,
                target_record_id,
                rule_result,
                request_context,
            )
            if not matches:
                return _error_response(404, "The requested resource wasn't found.")

        col_type = getattr(collection, "type", "base") or "base"
        if col_type == "auth":
            managed_keys = {
                k for k in payload.keys() if k in _AUTH_MANAGED_UPDATE_FIELDS
            }
            if managed_keys:
                has_manage_access = await _has_manage_access_for_auth_record(
                    engine,
                    collection,
                    target_record_id,
                    auth_ctx,
                    request_context,
                )
                if not has_manage_access:
                    if not (
                        _is_self_auth_target(e.auth, collection, target_record_id)
                        and managed_keys.issubset(_AUTH_SELF_ALLOWED_UPDATE_FIELDS)
                    ):
                        return _error_response(
                            403,
                            "The authorized record model is not allowed to perform this action.",
                        )

        try:
            record = await update_record(
                engine,
                collection,
                target_record_id,
                payload,
                files=payload_files,
            )
        except _ValidationErrors as exc:
            return _error_response(
                400,
                _validation_message("Failed to update record.", exc.errors),
                exc.errors,
            )
        except Exception:
            return _error_response(400, "Failed to update record.", {})

        if record is None:
            return _error_response(404, "The requested resource wasn't found.")

        if e.fields and record:
            selected_record = await get_record(
                engine,
                collection,
                target_record_id,
                fields=e.fields,
                request_context=request_context,
            )
            if selected_record is not None:
                record = selected_record

        if e.expand and record:
            expanded_records = await expand_records(
                engine,
                collection,
                [record],
                e.expand,
                all_collections,
                request_context=request_context,
            )
            record = expanded_records[0] if expanded_records else record

        return JSONResponse(content=record, status_code=200)

    try:
        response = await _trigger_record_request_hook(
            request,
            HOOK_RECORD_UPDATE_REQUEST,
            event,
            _default_batch_update_handler,
        )
    except HTTPException as exc:
        return _http_exception_response(exc)

    return _batch_result_from_response(response)


async def _apply_batch_delete(
    engine: Any,
    collection: Any,
    record_id: str,
    request: Request,
    auth: dict[str, Any] | None,
    query: dict[str, str],
    all_collections: list[Any],
) -> tuple[int, Any] | JSONResponse:
    if collection.name == "_superusers":
        return _error_response(
            400,
            "You cannot delete _superusers records via the records API. "
            "Use the admin auth endpoints instead.",
        )

    event = RecordRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=getattr(collection, "name", ""),
        record_id=record_id,
        auth=auth,
        engine=engine,
    )

    async def _default_batch_delete_handler(e: RecordRequestEvent) -> Response:
        target_record_id = e.record_id or record_id
        auth_ctx, request_context = _build_batch_rule_context(
            request,
            e.auth,
            "DELETE",
            {},
            query,
        )
        rule_result = check_rule(collection.delete_rule, auth_ctx)
        if rule_result is False:
            return _error_response(403, "Only superusers can perform this action.")

        if isinstance(rule_result, str):
            matches = await check_record_rule(
                engine,
                collection,
                target_record_id,
                rule_result,
                request_context,
            )
            if not matches:
                return _error_response(404, "The requested resource wasn't found.")

        deleted = await delete_record(
            engine,
            collection,
            target_record_id,
            all_collections=all_collections,
        )
        if not deleted:
            return _error_response(404, "The requested resource wasn't found.")

        return Response(status_code=204)

    try:
        response = await _trigger_record_request_hook(
            request,
            HOOK_RECORD_DELETE_REQUEST,
            event,
            _default_batch_delete_handler,
        )
    except HTTPException as exc:
        return _http_exception_response(exc)

    return _batch_result_from_response(response)


async def _execute_batch_request(
    engine: Any,
    request: Request,
    auth: dict[str, Any] | None,
    batch_request: Any,
    request_files: dict[str, list[tuple[str, bytes]]],
    all_collections: list[Any],
) -> tuple[int, Any] | JSONResponse:
    if not isinstance(batch_request, dict):
        return _error_response(400, "Invalid batch request.")

    try:
        action, _method, collection_name, record_id, query = _parse_batch_target(
            batch_request
        )
    except ValueError:
        return _error_response(400, "Invalid batch request.")

    raw_url = str(batch_request.get("url"))
    try:
        subrequest = _build_batch_request(
            request,
            _method,
            raw_url,
            query,
            batch_request.get("headers"),
        )
    except ValueError:
        return _error_response(400, "Invalid batch request.")

    collection = await resolve_collection(engine, collection_name)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    body = batch_request.get("body")
    if action in {"create", "update", "upsert"}:
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return _error_response(400, "Request body must be a JSON object.")
    else:
        body = {}

    if action == "create":
        result = await _apply_batch_create(
            engine,
            collection,
            subrequest,
            auth,
            body,
            request_files,
            query,
            all_collections,
        )
        if isinstance(result, JSONResponse):
            return result
        status, response_body = result
        return status, response_body
    if action == "update" and record_id is not None:
        result = await _apply_batch_update(
            engine,
            collection,
            record_id,
            subrequest,
            auth,
            body,
            request_files,
            query,
            all_collections,
        )
        if isinstance(result, JSONResponse):
            return result
        status, response_body = result
        return status, response_body
    if action == "delete" and record_id is not None:
        result = await _apply_batch_delete(
            engine,
            collection,
            record_id,
            subrequest,
            auth,
            query,
            all_collections,
        )
        if isinstance(result, JSONResponse):
            return result
        status, response_body = result
        return status, response_body
    if action == "upsert":
        from ppbase.core.storage_safety import (
            StorageSafetyError,
            validate_record_id,
        )

        raw_upsert_id = body.get("id")
        upsert_id = ""
        if raw_upsert_id is not None and raw_upsert_id != "":
            try:
                upsert_id = validate_record_id(raw_upsert_id)
            except StorageSafetyError:
                return _error_response(
                    400,
                    "Failed to create or update record.",
                    {
                        "id": {
                            "code": "validation_invalid_record_id",
                            "message": "Must be a valid record ID.",
                        }
                    },
                )
        if upsert_id:
            existing = await get_record(engine, collection, upsert_id)
            if existing is not None:
                update_request = _build_batch_request(
                    request,
                    "PATCH",
                    _batch_record_url(
                        collection_name,
                        upsert_id,
                        _batch_query_suffix(raw_url),
                    ),
                    query,
                    batch_request.get("headers"),
                )
                result = await _apply_batch_update(
                    engine,
                    collection,
                    upsert_id,
                    update_request,
                    auth,
                    body,
                    request_files,
                    query,
                    all_collections,
                )
                if isinstance(result, JSONResponse):
                    return result
                status, response_body = result
                return status, response_body

        create_payload = dict(body)
        if upsert_id:
            create_payload["id"] = upsert_id
        create_request = _build_batch_request(
            request,
            "POST",
            _batch_records_url(collection_name, _batch_query_suffix(raw_url)),
            query,
            batch_request.get("headers"),
        )
        result = await _apply_batch_create(
            engine,
            collection,
            create_request,
            auth,
            create_payload,
            request_files,
            query,
            all_collections,
        )
        if isinstance(result, JSONResponse):
            return result
        status, response_body = result
        return status, response_body

    return _error_response(400, "Invalid batch request.")


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

    event = RecordRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        auth=auth,
        page=page,
        per_page=perPage,
        sort=sort,
        filter=filter,
        expand=expand,
        fields=fields,
        skip_total=skipTotal,
        engine=engine,
    )

    async def _default_list_handler(e: RecordRequestEvent) -> JSONResponse:
        auth_ctx, request_context = _prepare_rule_context(request, e.auth)
        rule_result = check_rule(collection.list_rule, auth_ctx)

        if rule_result is False:
            return _error_response(
                403,
                "Only superusers can perform this action.",
            )

        effective_filter = e.filter
        if isinstance(rule_result, str):
            if effective_filter:
                effective_filter = f"({rule_result}) && ({effective_filter})"
            else:
                effective_filter = rule_result

        try:
            result = await list_records(
                e.engine or engine,
                collection,
                page=e.page or 1,
                per_page=e.per_page or 30,
                sort=e.sort,
                filter_str=effective_filter,
                fields=e.fields,
                skip_total=bool(e.skip_total),
                request_context=request_context,
            )
        except ValueError as exc:
            return _error_response(400, str(exc))

        if e.expand and result["items"]:
            all_colls = await get_all_collections(e.engine or engine)
            await expand_records(
                e.engine or engine,
                collection,
                result["items"],
                e.expand,
                all_colls,
                request_context=request_context,
            )

        return JSONResponse(content=result, status_code=200)

    return await _trigger_record_request_hook(
        request,
        HOOK_RECORDS_LIST_REQUEST,
        event,
        _default_list_handler,
    )


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

    data, files, parse_error = await _parse_record_request_body(request)
    if parse_error is not None:
        return parse_error
    if data is None:
        return _error_response(400, "Request body must be a JSON object.")

    event = RecordRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        auth=auth,
        data=data,
        files=files,
        expand=expand,
        fields=fields,
        engine=engine,
    )

    async def _default_create_handler(e: RecordRequestEvent) -> JSONResponse:
        active_engine = e.engine or engine
        if not isinstance(e.data, dict):
            return _error_response(400, "Request body must be a JSON object.")

        payload = dict(e.data)
        payload_files = dict(e.files)

        auth_ctx, request_context = _prepare_rule_context(request, e.auth, data=payload)
        rule_result = check_rule(collection.create_rule, auth_ctx)
        col_type = getattr(collection, "type", "base") or "base"
        managed_create_keys: set[str] = set()
        if col_type == "auth":
            managed_create_keys = {
                key for key in payload.keys() if key in _AUTH_MANAGED_CREATE_FIELDS
            }

        if rule_result is False:
            return _error_response(
                403,
                "Only superusers can perform this action.",
            )

        try:
            record = await create_record(
                active_engine, collection, payload, files=payload_files
            )
        except _ValidationErrors as exc:
            return _error_response(
                400,
                _validation_message("Failed to create record.", exc.errors),
                exc.errors,
            )
        except Exception:
            import logging

            logging.getLogger("ppbase").exception("Record creation failed")
            return _error_response(400, "Failed to create record.", {})

        if isinstance(rule_result, str):
            matches = await check_record_rule(
                active_engine,
                collection,
                record["id"],
                rule_result,
                request_context,
            )
            if not matches:
                raise _AbortWithResponse(
                    _error_response(
                        400,
                        "Failed to create record.",
                        {
                            "rule": {
                                "code": "validation_rule_failed",
                                "message": "Action not allowed.",
                            }
                        },
                    )
                )

        if managed_create_keys:
            has_manage_access = await _has_manage_access_for_auth_record(
                active_engine,
                collection,
                record["id"],
                auth_ctx,
                request_context,
            )
            if not has_manage_access:
                raise _AbortWithResponse(
                    _error_response(
                        403,
                        "The authorized record model is not allowed to perform this action.",
                    )
                )

        if e.expand and record:
            all_colls = await get_all_collections(active_engine)
            records = await expand_records(
                active_engine,
                collection,
                [record],
                e.expand,
                all_colls,
                request_context=request_context,
            )
            record = records[0] if records else record

        return JSONResponse(content=record, status_code=200)

    async def _run_create(active_engine: _ConnectionEngineAdapter) -> Any:
        event.engine = active_engine
        return await _trigger_record_request_hook(
            request,
            HOOK_RECORD_CREATE_REQUEST,
            event,
            _default_create_handler,
        )

    try:
        return await _run_storage_transaction(engine, _run_create)
    except _AbortWithResponse as abort:
        return abort.response


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

    event = RecordRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        record_id=recordId,
        auth=auth,
        expand=expand,
        fields=fields,
        engine=engine,
    )

    async def _default_view_handler(e: RecordRequestEvent) -> JSONResponse:
        active_engine = e.engine or engine
        auth_ctx, request_context = _prepare_rule_context(request, e.auth)
        rule_result = check_rule(collection.view_rule, auth_ctx)

        if rule_result is False:
            return _error_response(
                403,
                "Only superusers can perform this action.",
            )

        record = await get_record(
            active_engine,
            collection,
            e.record_id or recordId,
            fields=e.fields,
            request_context=request_context,
        )
        if record is None:
            return _error_response(404, "The requested resource wasn't found.")

        if isinstance(rule_result, str):
            matches = await check_record_rule(
                active_engine,
                collection,
                e.record_id or recordId,
                rule_result,
                request_context,
            )
            if not matches:
                return _error_response(404, "The requested resource wasn't found.")

        if e.expand and record:
            all_colls = await get_all_collections(active_engine)
            records = await expand_records(
                active_engine,
                collection,
                [record],
                e.expand,
                all_colls,
                request_context=request_context,
            )
            record = records[0] if records else record

        return JSONResponse(content=record, status_code=200)

    return await _trigger_record_request_hook(
        request,
        HOOK_RECORD_VIEW_REQUEST,
        event,
        _default_view_handler,
    )


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

    data, files, parse_error = await _parse_record_request_body(request)
    if parse_error is not None:
        return parse_error
    if data is None:
        return _error_response(400, "Request body must be a JSON object.")

    event = RecordRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        record_id=recordId,
        auth=auth,
        data=data,
        files=files,
        expand=expand,
        fields=fields,
        engine=engine,
    )

    async def _default_update_handler(e: RecordRequestEvent) -> JSONResponse:
        active_engine = e.engine or engine
        if not isinstance(e.data, dict):
            return _error_response(400, "Request body must be a JSON object.")

        payload = dict(e.data)
        payload_files = dict(e.files)
        target_record_id = e.record_id or recordId

        auth_ctx, request_context = _prepare_rule_context(request, e.auth, data=payload)
        rule_result = check_rule(collection.update_rule, auth_ctx)

        if rule_result is False:
            return _error_response(
                403,
                "Only superusers can perform this action.",
            )

        if isinstance(rule_result, str):
            matches = await check_record_rule(
                active_engine,
                collection,
                target_record_id,
                rule_result,
                request_context,
            )
            if not matches:
                return _error_response(404, "The requested resource wasn't found.")

        col_type = getattr(collection, "type", "base") or "base"
        if col_type == "auth":
            managed_keys = {
                key for key in payload.keys() if key in _AUTH_MANAGED_UPDATE_FIELDS
            }
            if managed_keys:
                has_manage_access = await _has_manage_access_for_auth_record(
                    active_engine,
                    collection,
                    target_record_id,
                    auth_ctx,
                    request_context,
                )
                if not has_manage_access:
                    if not (
                        _is_self_auth_target(e.auth, collection, target_record_id)
                        and managed_keys.issubset(_AUTH_SELF_ALLOWED_UPDATE_FIELDS)
                    ):
                        return _error_response(
                            403,
                            "The authorized record model is not allowed to perform this action.",
                        )

        try:
            record = await update_record(
                active_engine,
                collection,
                target_record_id,
                payload,
                files=payload_files,
            )
        except _ValidationErrors as exc:
            return _error_response(
                400,
                _validation_message("Failed to update record.", exc.errors),
                exc.errors,
            )
        except Exception:
            import logging

            logging.getLogger("ppbase").exception("Record update failed")
            return _error_response(400, "Failed to update record.", {})

        if record is None:
            return _error_response(404, "The requested resource wasn't found.")

        if e.expand and record:
            all_colls = await get_all_collections(active_engine)
            records = await expand_records(
                active_engine,
                collection,
                [record],
                e.expand,
                all_colls,
                request_context=request_context,
            )
            record = records[0] if records else record

        return JSONResponse(content=record, status_code=200)

    async def _run_update(active_engine: _ConnectionEngineAdapter) -> Any:
        event.engine = active_engine
        return await _trigger_record_request_hook(
            request,
            HOOK_RECORD_UPDATE_REQUEST,
            event,
            _default_update_handler,
        )

    try:
        return await _run_storage_transaction(engine, _run_update)
    except _AbortWithResponse as abort:
        return abort.response


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

    event = RecordRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        record_id=recordId,
        auth=auth,
        engine=engine,
    )

    async def _default_delete_handler(e: RecordRequestEvent) -> JSONResponse:
        active_engine = e.engine or engine
        target_record_id = e.record_id or recordId
        auth_ctx, request_context = _prepare_rule_context(request, e.auth)
        rule_result = check_rule(collection.delete_rule, auth_ctx)

        if rule_result is False:
            return _error_response(
                403,
                "Only superusers can perform this action.",
            )

        if isinstance(rule_result, str):
            matches = await check_record_rule(
                active_engine,
                collection,
                target_record_id,
                rule_result,
                request_context,
            )
            if not matches:
                return _error_response(404, "The requested resource wasn't found.")

        all_colls = await get_all_collections(active_engine)
        deleted = await delete_record(
            active_engine,
            collection,
            target_record_id,
            all_collections=all_colls,
        )

        if not deleted:
            return _error_response(404, "The requested resource wasn't found.")

        return Response(status_code=204)

    async def _run_delete(active_engine: _ConnectionEngineAdapter) -> Any:
        event.engine = active_engine
        return await _trigger_record_request_hook(
            request,
            HOOK_RECORD_DELETE_REQUEST,
            event,
            _default_delete_handler,
        )

    try:
        return await _run_storage_transaction(engine, _run_delete)
    except _AbortWithResponse as abort:
        return abort.response


# ---------------------------------------------------------------------------
# POST /api/batch
# ---------------------------------------------------------------------------


@router.post("/api/batch")
async def api_batch_records(
    request: Request,
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """Execute multiple record actions in a single transaction."""
    engine = get_engine()

    (
        batch_enabled,
        max_requests,
        max_body_size,
        timeout_seconds,
    ) = await _get_batch_settings(engine)
    if not batch_enabled:
        return _error_response(403, "Batch requests are not allowed.")

    request_body = await request.body()
    if max_body_size > 0 and len(request_body) > max_body_size:
        return _error_response(
            400,
            f"The allowed max batch request body size is {max_body_size} bytes.",
        )

    payload, files_by_request, parse_error = await _parse_batch_payload(request)
    if parse_error is not None:
        return parse_error
    if not isinstance(payload, dict):
        return _error_response(400, "Invalid batch request body.")

    requests_payload = payload.get("requests")
    if not isinstance(requests_payload, list):
        return _error_response(400, "Invalid batch request body.")
    if len(requests_payload) > max_requests:
        return _error_response(
            400,
            f"The allowed max number of batch requests is {max_requests}.",
        )

    result_items: list[dict[str, Any]] = []
    created_file_targets: set[tuple[str, str, str]] = set()
    deferred_storage_deletes: list[
        tuple[str, str, str, tuple[str, ...]]
    ] = []
    final_file_references: set[tuple[str, str, str]] = set()
    transaction_body_finished = False
    try:
        from ppbase.services.file_storage import (
            capture_storage_writes,
            defer_storage_deletes,
            flush_deferred_storage_deletes,
            pin_storage_config,
        )

        with pin_storage_config():
            try:
                with (
                    capture_storage_writes(created_file_targets),
                    defer_storage_deletes(deferred_storage_deletes),
                ):
                    async with asyncio.timeout(timeout_seconds):
                        async with engine.begin() as conn:
                            batch_engine = _ConnectionEngineAdapter(conn)
                            all_collections = await get_all_collections(batch_engine)

                            for index, item in enumerate(requests_payload):
                                response_or_error = await _execute_batch_request(
                                    batch_engine,
                                    request,
                                    auth,
                                    item,
                                    files_by_request.get(index, {}),
                                    all_collections,
                                )
                                if isinstance(response_or_error, JSONResponse):
                                    raise _BatchRequestFailed(
                                        index, _response_json_body(response_or_error)
                                    )

                                status, body = response_or_error
                                result_items.append(
                                    {
                                        "status": status,
                                        "body": body,
                                    }
                                )
                            affected_records = _affected_storage_records(
                                created_file_targets,
                                deferred_storage_deletes,
                            )
                            if affected_records:
                                final_file_references = (
                                    await _final_batch_file_references(
                                        batch_engine,
                                        all_collections,
                                        affected_records,
                                    )
                                )
                            transaction_body_finished = True
            except BaseException as exc:
                if transaction_body_finished and isinstance(exc, Exception):
                    durable_references = await _reconcile_storage_file_references(
                        engine,
                        _affected_storage_records(
                            created_file_targets,
                            deferred_storage_deletes,
                        )
                    )
                    if durable_references is not None:
                        _cleanup_unreferenced_created_files(
                            created_file_targets,
                            durable_references,
                        )
                    elif created_file_targets:
                        logger.warning(
                            "Preserving %d new batch storage file(s) because the "
                            "database commit outcome could not be reconciled",
                            len(created_file_targets),
                        )
                    if deferred_storage_deletes:
                        logger.warning(
                            "Preserving %d deferred batch storage deletion(s) "
                            "after an ambiguous database commit",
                            len(deferred_storage_deletes),
                        )
                elif transaction_body_finished:
                    logger.warning(
                        "Preserving batch storage changes because database commit "
                        "was interrupted before its outcome could be reconciled"
                    )
                elif created_file_targets:
                    _cleanup_created_record_files(created_file_targets)
                raise

            _cleanup_unreferenced_created_files(
                created_file_targets,
                final_file_references,
            )
            cleanup_failures = flush_deferred_storage_deletes(
                deferred_storage_deletes,
                preserved_files=final_file_references,
            )
            if cleanup_failures:
                logger.warning(
                    "Skipped %d unsafe or conflicting committed batch storage cleanup(s)",
                    cleanup_failures,
                )
    except _BatchRequestFailed as exc:
        return _error_response(
            400,
            "Batch transaction failed.",
            {
                "requests": {
                    str(exc.index): {
                        "code": "batch_request_failed",
                        "message": "Batch request failed.",
                        "response": exc.response,
                    },
                },
            },
        )
    except TimeoutError:
        return _error_response(
            400,
            "Batch transaction failed.",
            {
                "timeout": {
                    "code": "batch_timeout",
                    "message": "Batch request timeout reached.",
                },
            },
        )
    return JSONResponse(content=result_items, status_code=200)
