"""Files API routes.

Endpoints:
    POST /api/files/token
    GET  /api/files/{collectionIdOrName}/{recordId}/{filename}
    HEAD /api/files/{collectionIdOrName}/{recordId}/{filename}

Downloads follow PocketBase's ``apis/file.go`` ``download`` and
``tools/filesystem`` ``Serve``: content type from the ``.attrs`` side-car,
``Content-Disposition``, thumbnails, security and cache headers.  The rules
live in :mod:`ppbase.storage.serving` and :mod:`ppbase.storage.thumbs`; see
``docs/file-serving.md``.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, get_settings, require_auth, resolve_collection
from ppbase.core.storage_safety import StorageSafetyError
from ppbase.db.engine import get_engine
from ppbase.db.system_tables import CollectionRecord, SuperuserRecord
from ppbase.ext.events import FileDownloadRequestEvent, FileTokenRequestEvent
from ppbase.models.field_multiplicity import select_or_file_is_multiple
from ppbase.ext.registry import (
    HOOK_FILE_DOWNLOAD_REQUEST,
    HOOK_FILE_TOKEN_REQUEST,
    get_extension_registry,
)
from ppbase.services.file_tokens import (
    create_file_token_for_auth as _create_file_token_for_auth,
    get_superusers_collection as _get_superusers_collection,
    verify_file_token as _verify_file_token,
)
from ppbase.services.file_storage import (
    StorageFileStream,
    open_file_stream,
    pin_storage_config,
    write_local_storage_variant_bytes,
)
from ppbase.services.record_service import check_record_rule
from ppbase.services.rule_engine import check_rule
from ppbase.services.write_barrier import (
    WriteBarrierLease,
    mutation_write_barrier_on_connection,
)
from ppbase.storage.local_variants import (
    OversizedFileAttributes,
    open_local_stream,
    read_local_attrs,
)
from ppbase.storage.serving import (
    ATTRS_SUFFIX,
    CACHE_CONTROL,
    CONTENT_SECURITY_POLICY,
    FILE_SECURITY_HEADERS,
    FORCE_ATTACHMENT_PARAM,
    THUMB_IMAGE_CONTENT_TYPES,
    InvalidFileAttributes,
    content_disposition,
    disposition_type,
    encode_attrs,
    force_attachment,
    latin1_header_value,
    parse_attrs,
    response_content_type,
    thumb_names,
    thumb_size_allowed,
)
from ppbase.storage.thumbs import create_thumb_bytes

router = APIRouter()
_QUERY_SOURCE_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+("(?P<qtable>[^"]+)"|(?P<table>[A-Za-z_][A-Za-z0-9_]*))'
    r'(?:\s+(?:AS\s+)?("(?P<qalias>[^"]+)"|(?P<alias>[A-Za-z_][A-Za-z0-9_]*)))?',
    re.IGNORECASE,
)
_SQL_KEYWORDS = {
    "and",
    "cross",
    "full",
    "group",
    "having",
    "inner",
    "join",
    "left",
    "limit",
    "on",
    "order",
    "right",
    "union",
    "where",
}

#: Concurrent thumbnail generations and their maximum wait, like
#: ``bindFileApi`` (v0.22.34 ``apis/file.go`` l.33-35: ``NumCPU() + 2`` and
#: 60 seconds; v0.40.6 l.27-41 keeps these defaults, overridable there by
#: ``PB_THUMBS_MAX_WORKERS``/``PB_THUMBS_MAX_WAIT``).
_THUMB_GENERATION_MAX_WAIT_SECONDS = 60.0
_thumb_generation_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()

#: Returned by :func:`_resolve_thumbnail` on a lock-free cache miss.
_THUMBNAIL_CACHE_MISS = object()


class _ThumbnailGenerationRequiresBarrier(Exception):
    """Restart a cache-miss request after acquiring the shared barrier."""


@dataclass
class _ServedThumbnail:
    stream: StorageFileStream
    served_name: str
    content_type: str


def _not_found_error() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "status": 404,
            "message": "The requested resource wasn't found.",
            "data": {},
        },
    )


def _file_token_error() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "status": 400,
            "message": "Failed to generate file token.",
            "data": {},
        },
    )


def _first_query_value(request: Request, name: str) -> str | None:
    """Go's ``url.Values.Get``: the first value of a repeated parameter."""
    params = request.query_params
    getlist = getattr(params, "getlist", None)
    if callable(getlist):
        values = getlist(name)
        return str(values[0]) if values else None
    value = params.get(name)
    return None if value is None else str(value)


def _guess_media_type(filename: str) -> str:
    media_type, _encoding = mimetypes.guess_type(filename)
    if media_type:
        return media_type
    return "application/octet-stream"


def _fallback_content_type(filename: str) -> str:
    """PPBase's extension-based type for a file without ``.attrs``.

    Unchanged from earlier releases: the guessed type, with Starlette's
    ``; charset=utf-8`` for ``text/*`` that responses used to add.
    """
    media_type = _guess_media_type(filename)
    if media_type.startswith("text/") and "charset=" not in media_type.lower():
        media_type += "; charset=utf-8"
    return media_type


def _original_content_type(
    opened: StorageFileStream,
    collection_id: str,
    record_id: str,
    filename: str,
) -> str:
    """The real content type of a stored file (``Serve``'s ``ContentType``).

    Raises :class:`InvalidFileAttributes` for an undecodable side-car, which
    PocketBase answers with a 404.
    """
    if opened.backend != "local" or opened.config is None:
        return _fallback_content_type(filename)
    try:
        raw = read_local_attrs(opened.config, collection_id, record_id, filename)
    except OversizedFileAttributes as exc:
        raise InvalidFileAttributes(str(exc)) from exc
    if raw is None:
        return _fallback_content_type(filename)
    return parse_attrs(raw).content_type


def _thumb_generation_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _thumb_generation_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore((os.cpu_count() or 1) + 2)
        _thumb_generation_semaphores[loop] = semaphore
    return semaphore


def _open_existing_thumbnail(
    storage_config: Any,
    collection_id: str,
    record_id: str,
    thumb_dir: str,
    thumb_name: str,
    original_content_type: str,
) -> _ServedThumbnail | None:
    """Open a stored thumbnail and its side-car, ``None`` when missing.

    A thumbnail whose side-car cannot be decoded does not exist for
    ``fileblob``'s ``Exists``: PocketBase generates it again.
    """
    stream = open_local_stream(
        storage_config,
        collection_id,
        record_id,
        thumb_name,
        directory_name=thumb_dir,
    )
    if stream is None:
        return None
    try:
        raw = read_local_attrs(
            storage_config,
            collection_id,
            record_id,
            thumb_name,
            directory_name=thumb_dir,
        )
        attrs = parse_attrs(raw) if raw is not None else None
    except (InvalidFileAttributes, OversizedFileAttributes):
        stream.close()
        return None
    except BaseException:
        stream.close()
        raise
    # A thumbnail without side-car (one generated by an earlier PPBase) has
    # the type of its original, which is what PocketBase records for it.
    content_type = attrs.content_type if attrs is not None else original_content_type
    return _ServedThumbnail(stream, thumb_name, content_type)


def _read_original_bytes(
    storage_config: Any,
    collection_id: str,
    record_id: str,
    filename: str,
) -> bytes | None:
    opened = open_local_stream(storage_config, collection_id, record_id, filename)
    if opened is None:
        return None
    try:
        return bytes(opened.stream.read())
    finally:
        opened.close()


async def _create_thumbnail(
    collection_id: str,
    record_id: str,
    filename: str,
    thumb_dir: str,
    thumb_name: str,
    thumb_size: str,
    original_content_type: str,
    storage_config: Any,
    lease: WriteBarrierLease,
) -> bool:
    """Generate and publish one thumbnail like ``CreateThumb``.

    The side-car is written first, like ``fileblob``'s writer, with the
    original's content type (v0.22.34 ``filesystem.go`` l.501-503).  Any
    failure, including a write failure or the 60 s wait, means "serve the
    original" (v0.22.34 ``apis/file.go`` l.179-190).
    """
    semaphore = _thumb_generation_semaphore()
    try:
        await asyncio.wait_for(
            semaphore.acquire(), timeout=_THUMB_GENERATION_MAX_WAIT_SECONDS
        )
    except TimeoutError:
        return False
    try:
        try:
            source = await asyncio.to_thread(
                _read_original_bytes,
                storage_config,
                collection_id,
                record_id,
                filename,
            )
        except (OSError, StorageSafetyError):
            return False
        if source is None:
            return False
        generated = await asyncio.to_thread(create_thumb_bytes, source, thumb_size)
        if generated is None:
            return False
        try:
            write_local_storage_variant_bytes(
                collection_id,
                record_id,
                thumb_dir,
                f"{thumb_name}{ATTRS_SUFFIX}",
                encode_attrs(generated, original_content_type),
                lease=lease,
                config=storage_config,
            )
            write_local_storage_variant_bytes(
                collection_id,
                record_id,
                thumb_dir,
                thumb_name,
                generated,
                lease=lease,
                config=storage_config,
            )
        except (OSError, StorageSafetyError):
            return False
        return True
    finally:
        semaphore.release()


async def _resolve_thumbnail(
    collection_id: str,
    record_id: str,
    filename: str,
    thumb_size: str,
    original_content_type: str,
    storage_config: Any,
    lease: WriteBarrierLease | None,
) -> _ServedThumbnail | object | None:
    """Return the thumbnail to serve, ``None`` to serve the original.

    Without a write lease a missing thumbnail is reported as
    :data:`_THUMBNAIL_CACHE_MISS` so that the request restarts under the
    shared barrier before generating it.
    """
    thumb_dir, thumb_name = thumb_names(thumb_size, filename)
    try:
        existing = await asyncio.to_thread(
            _open_existing_thumbnail,
            storage_config,
            collection_id,
            record_id,
            thumb_dir,
            thumb_name,
            original_content_type,
        )
    except (OSError, StorageSafetyError):
        # An unsafe variant path (a symlink...) is never followed nor
        # replaced: the original is served.
        return None
    if existing is not None:
        return existing
    if lease is None:
        return _THUMBNAIL_CACHE_MISS
    created = await _create_thumbnail(
        collection_id,
        record_id,
        filename,
        thumb_dir,
        thumb_name,
        thumb_size,
        original_content_type,
        storage_config,
        lease,
    )
    if not created:
        return None
    try:
        reopened = await asyncio.to_thread(
            _open_existing_thumbnail,
            storage_config,
            collection_id,
            record_id,
            thumb_dir,
            thumb_name,
            original_content_type,
        )
    except (OSError, StorageSafetyError) as exc:
        raise _not_found_error() from exc
    if reopened is None:
        # PocketBase serves the thumbnail it has just written; when that
        # read fails ``Serve`` returns an error and the request is a 404.
        raise _not_found_error()
    return reopened


async def _stream_storage_file(
    opened: StorageFileStream,
    *,
    start: int = 0,
    length: int | None = None,
    chunk_size: int = 64 * 1024,
) -> AsyncIterator[bytes]:
    """Yield bounded chunks and always close the underlying file/object body."""
    try:
        if start > 0:
            seek = getattr(opened.stream, "seek", None)
            if callable(seek):
                try:
                    await asyncio.to_thread(seek, start)
                    start = 0
                except (OSError, TypeError):
                    pass
            while start > 0:
                skipped = await asyncio.to_thread(
                    opened.stream.read,
                    min(chunk_size, start),
                )
                if not skipped:
                    return
                start -= len(skipped)

        remaining = length
        while True:
            if remaining is not None and remaining <= 0:
                break
            read_size = (
                chunk_size
                if remaining is None
                else min(chunk_size, remaining)
            )
            chunk = await asyncio.to_thread(opened.stream.read, read_size)
            if not chunk:
                break
            payload = bytes(chunk)
            if remaining is not None:
                remaining -= len(payload)
            yield payload
    finally:
        opened.close()


def _parse_single_byte_range(
    raw_header: str,
    total_size: int,
) -> tuple[int, int]:
    """Parse one RFC 7233 byte range and return inclusive bounds."""
    if total_size <= 0:
        raise ValueError("Range is not satisfiable.")
    units, separator, value = raw_header.partition("=")
    if separator != "=" or units.strip().lower() != "bytes" or "," in value:
        raise ValueError("Malformed Range header.")
    start_raw, dash, end_raw = value.strip().partition("-")
    if dash != "-":
        raise ValueError("Malformed Range header.")
    if not start_raw:
        if not end_raw.isdigit():
            raise ValueError("Malformed Range header.")
        suffix_length = int(end_raw)
        if suffix_length <= 0:
            raise ValueError("Range is not satisfiable.")
        start = max(total_size - suffix_length, 0)
        return start, total_size - 1
    if not start_raw.isdigit() or (end_raw and not end_raw.isdigit()):
        raise ValueError("Malformed Range header.")
    start = int(start_raw)
    if start >= total_size:
        raise ValueError("Range is not satisfiable.")
    end = int(end_raw) if end_raw else total_size - 1
    end = min(end, total_size - 1)
    if start > end:
        raise ValueError("Range is not satisfiable.")
    return start, end


def _normalize_file_options(field_def: dict[str, Any]) -> dict[str, Any]:
    options = field_def.get("options")
    normalized: dict[str, Any] = dict(options) if isinstance(options, dict) else {}
    # Handle flat legacy schemas where file options can be top-level.
    for key in ("maxSelect", "maxSize", "mimeTypes", "thumbs", "protected"):
        if key in field_def and key not in normalized:
            normalized[key] = field_def.get(key)
    return normalized


def _collection_type(collection: CollectionRecord) -> str:
    return str(getattr(collection, "type", "base") or "base").strip().lower()


def _quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _get_view_query(collection: CollectionRecord) -> str:
    options = getattr(collection, "options", None) or {}
    if not isinstance(options, dict):
        return ""
    return str(options.get("query") or options.get("viewQuery") or "").strip()


def _strip_sql_comments(query: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    return re.sub(r"--[^\n\r]*", " ", without_block)


def _extract_query_sources(query: str) -> dict[str, str]:
    sources: dict[str, str] = {}
    clean_query = _strip_sql_comments(query)

    for match in _QUERY_SOURCE_RE.finditer(clean_query):
        table = match.group("qtable") or match.group("table") or ""
        alias = match.group("qalias") or match.group("alias") or table
        if alias.lower() in _SQL_KEYWORDS:
            alias = table
        if table:
            sources[table] = table
        if alias:
            sources[alias] = table

    return sources


def _find_top_level_from_index(query: str) -> int:
    depth = 0
    quote: str | None = None
    index = 0
    upper_query = query.upper()

    while index < len(query):
        char = query[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif depth == 0 and upper_query.startswith("FROM", index):
            before = query[index - 1] if index > 0 else " "
            after_index = index + len("FROM")
            after = query[after_index] if after_index < len(query) else " "
            if (
                not (before.isalnum() or before == "_")
                and not (after.isalnum() or after == "_")
            ):
                return index
        index += 1

    return -1


def _get_view_select_clause(query: str) -> str:
    stripped = query.strip()
    if not stripped.upper().startswith("SELECT"):
        return ""
    from_index = _find_top_level_from_index(stripped)
    if from_index < 0:
        return ""
    return stripped[len("SELECT") : from_index]


def _split_select_expressions(select_sql: str) -> list[str]:
    expressions: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None

    for index, char in enumerate(select_sql):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")" and depth > 0:
            depth -= 1
            continue
        if char == "," and depth == 0:
            expressions.append(select_sql[start:index].strip())
            start = index + 1

    tail = select_sql[start:].strip()
    if tail:
        expressions.append(tail)
    return expressions


def _unquote_ident(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def _parse_simple_column_reference(expression: str) -> tuple[str | None, str | None]:
    expr = expression.strip()
    match = re.match(
        r'^(?:(?P<alias>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\.)?'
        r'(?P<field>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)$',
        expr,
    )
    if match is None:
        return None, None
    alias = match.group("alias")
    field = match.group("field")
    return (_unquote_ident(alias) if alias else None, _unquote_ident(field))


def _view_file_source_hint(
    collection: CollectionRecord,
    file_field_name: str,
) -> tuple[str | None, str]:
    query = _get_view_query(collection)
    if not query:
        return None, file_field_name

    clean_query = _strip_sql_comments(query)
    select_sql = _get_view_select_clause(clean_query)
    if not select_sql:
        return None, file_field_name

    sources = _extract_query_sources(clean_query)

    for expression in _split_select_expressions(select_sql):
        alias_match = re.search(
            r'\s+AS\s+("(?P<qas>[^"]+)"|(?P<as>[A-Za-z_][A-Za-z0-9_]*))\s*$',
            expression,
            flags=re.IGNORECASE,
        )
        if alias_match:
            output_name = alias_match.group("qas") or alias_match.group("as") or ""
            source_expr = expression[: alias_match.start()].strip()
        else:
            source_expr = expression.strip()
            _source_alias, source_field = _parse_simple_column_reference(source_expr)
            output_name = source_field or ""

        if output_name != file_field_name:
            continue

        source_alias, source_field = _parse_simple_column_reference(source_expr)
        if not source_field:
            continue
        source_table = sources.get(source_alias or "")
        return source_table, source_field

    return None, file_field_name


def _field_contains_filename(field_value: Any, filename: str) -> bool:
    target = str(filename)
    if isinstance(field_value, list):
        return target in {str(v) for v in field_value if v is not None}
    return str(field_value or "") == target


def _find_file_field_for_filename(
    schema: list[dict[str, Any]],
    row: dict[str, Any],
    filename: str,
) -> tuple[dict[str, Any], bool] | None:
    for field_def in schema:
        if not isinstance(field_def, dict):
            continue
        if field_def.get("type") != "file":
            continue
        field_name = str(field_def.get("name", "") or "")
        if not field_name:
            continue
        if not _field_contains_filename(row.get(field_name), filename):
            continue
        options = _normalize_file_options(field_def)
        return field_def, bool(options.get("protected", False))
    return None


async def _resolve_view_file_storage_context(
    session: AsyncSession,
    collection: CollectionRecord,
    file_field_name: str,
    filename: str,
) -> tuple[str, str] | None:
    """Return ``(collection_id, record_id)`` for files projected by a view."""
    if _collection_type(collection) != "view":
        return None

    source_table, source_field_name = _view_file_source_hint(collection, file_field_name)
    query_sources = set(_extract_query_sources(_get_view_query(collection)).values())

    stmt = select(CollectionRecord).where(CollectionRecord.type != "view")
    if source_table:
        stmt = stmt.where(CollectionRecord.name == source_table)
    elif query_sources:
        stmt = stmt.where(CollectionRecord.name.in_(query_sources))

    candidates = (await session.execute(stmt)).scalars().all()
    for candidate in candidates:
        for field_def in candidate.schema or []:
            if not isinstance(field_def, dict):
                continue
            if field_def.get("type") != "file":
                continue
            field_name = str(field_def.get("name", "") or "")
            if field_name != source_field_name:
                continue

            options = _normalize_file_options(field_def)
            table_name = _quote_ident(candidate.name)
            column_name = _quote_ident(field_name)
            if select_or_file_is_multiple(options):
                query = text(
                    f"SELECT id FROM {table_name} "
                    f"WHERE :filename = ANY({column_name}) LIMIT 1"
                )
            else:
                query = text(
                    f"SELECT id FROM {table_name} "
                    f"WHERE {column_name} = :filename LIMIT 1"
                )

            try:
                result = await session.execute(query, {"filename": filename})
            except Exception:
                continue
            row = result.mappings().first()
            if row is not None:
                return candidate.id, str(row.get("id") or "")

    return None


async def _resolve_file_token_event_context(
    session: AsyncSession,
    auth_payload: dict[str, Any],
) -> tuple[CollectionRecord | None, dict[str, Any] | None]:
    """Resolve collection/record context for file token hooks."""
    auth_type = str(auth_payload.get("type", "") or "")
    auth_id = str(auth_payload.get("id", "") or "")
    if not auth_type or not auth_id:
        return None, None

    if auth_type == "admin":
        admin = await session.get(SuperuserRecord, auth_id)
        if admin is None:
            return None, None
        collection = await _get_superusers_collection(session)
        record = {
            "id": admin.id,
            "email": admin.email,
        }
        return collection, record

    if auth_type in {"auth", "authRecord"}:
        collection_id = str(auth_payload.get("collectionId", "") or "")
        if not collection_id:
            return None, None
        auth_collection = await session.get(CollectionRecord, collection_id)
        if auth_collection is None:
            return None, None
        row_result = await session.execute(
            text(f'SELECT * FROM "{auth_collection.name}" WHERE "id" = :rid LIMIT 1'),
            {"rid": auth_id},
        )
        row = row_result.mappings().first()
        return auth_collection, (dict(row) if row is not None else None)

    return None, None


async def _trigger_file_token_request_hooks(
    request: Request,
    event: FileTokenRequestEvent,
    default_handler: Any,
) -> Any:
    extensions = get_extension_registry(request.app)
    if extensions is None:
        return await default_handler(event)
    hook = extensions.hooks.get(HOOK_FILE_TOKEN_REQUEST)
    return await hook.trigger(event, default_handler)


async def _trigger_file_download_request_hooks(
    request: Request,
    event: FileDownloadRequestEvent,
    default_handler: Any,
) -> Any:
    extensions = get_extension_registry(request.app)
    if extensions is None:
        return await default_handler(event)
    hook = extensions.hooks.get(HOOK_FILE_DOWNLOAD_REQUEST)
    return await hook.trigger(event, default_handler)


def _build_rule_context(
    request: Request,
    auth_payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if auth_payload.get("type") == "admin":
        auth_ctx: dict[str, Any] | None = {
            "is_admin": True,
            "@request.auth.id": auth_payload.get("id", ""),
            "@request.auth.email": auth_payload.get("email", ""),
        }
    else:
        auth_ctx = {
            "is_admin": False,
            "@request.auth.id": auth_payload.get("id", ""),
            "@request.auth.collectionId": auth_payload.get("collectionId", ""),
            "@request.auth.collectionName": auth_payload.get("collectionName", ""),
            "@request.auth.type": auth_payload.get("type", ""),
        }

    headers_info: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        headers_info[lower] = value
        headers_info[lower.replace("-", "_")] = value

    request_context = {
        "context": "protectedFile",
        "method": request.method.upper(),
        "headers": headers_info,
        "auth": {
            "id": auth_payload.get("id", ""),
            "email": auth_payload.get("email", ""),
            "type": auth_payload.get("type", ""),
            "collectionId": auth_payload.get("collectionId", ""),
            "collectionName": auth_payload.get("collectionName", ""),
        },
        "data": {},
        "query": dict(request.query_params),
    }
    return auth_ctx, request_context


async def _check_protected_file_view_rule(
    request: Request,
    collection: CollectionRecord,
    record_id: str,
    auth_payload: dict[str, Any],
) -> bool:
    auth_ctx, request_context = _build_rule_context(request, auth_payload)
    rule_result = check_rule(collection.view_rule, auth_ctx)
    if rule_result is False:
        return False
    if rule_result is True:
        return True

    engine = get_engine()
    try:
        return await check_record_rule(
            engine,
            collection,
            record_id,
            str(rule_result),
            request_context,
        )
    except Exception:
        return False


@router.post("/token")
async def generate_file_token(
    request: Request,
    auth: dict[str, Any] = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Generate a short-lived token for protected file access."""
    token_collection, token_record = await _resolve_file_token_event_context(session, auth)
    event = FileTokenRequestEvent(
        app=request.app,
        request=request,
        collection=token_collection,
        record=token_record,
        auth=auth,
    )

    async def _default_file_token(e: FileTokenRequestEvent) -> dict[str, str]:
        try:
            token = await _create_file_token_for_auth(session, e.auth or {})
        except Exception as exc:
            raise _file_token_error() from exc
        e.token = token
        return {"token": token}

    return await _trigger_file_token_request_hooks(request, event, _default_file_token)


# Two routes, so that OpenAPI keeps one ``GET`` operation (a single route
# with both methods gives both operations the same generated id).
@router.head(
    "/{collection_id_or_name}/{record_id}/{filename}",
    include_in_schema=False,
)
@router.get("/{collection_id_or_name}/{record_id}/{filename}")
async def serve_file(
    collection_id_or_name: str,
    record_id: str,
    filename: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Any = Depends(get_settings),
):
    """Serve a file from local or S3-compatible storage.

    ``HEAD`` is served like ``GET`` without a body, as by PocketBase
    (v0.22.34 ``apis/file.go`` l.40-41; v0.40.6 ``apis/file.go`` l.45 on
    ``net/http``'s ``ServeMux``, ``tools/router/router.go`` l.74 and l.131,
    whose ``GET`` patterns also match ``HEAD``).
    """
    try:
        # Cached thumbnails and ordinary reads stay lock-free.  This first pass
        # is strictly read-only: a cache miss restarts only after leaving the
        # pinned configuration, so it can never become a writer using a backend
        # selected before a concurrent exclusive local/S3 switch.
        with pin_storage_config(settings) as storage_config:
            return await _serve_file(
                collection_id_or_name,
                record_id,
                filename,
                request,
                session,
                initial_storage_config=storage_config,
                thumbnail_write_lease=None,
            )
    except _ThumbnailGenerationRequiresBarrier:
        connection = await session.connection()
        async with mutation_write_barrier_on_connection(connection) as lease:
            # Select and pin the writable backend only after the shared lock is
            # held.  Re-run all record/source validation and reopen the source
            # beneath this current configuration; no object from the first pass
            # is reused after an intervening exclusive switch.
            with pin_storage_config(settings) as storage_config:
                return await _serve_file(
                    collection_id_or_name,
                    record_id,
                    filename,
                    request,
                    session,
                    initial_storage_config=storage_config,
                    thumbnail_write_lease=lease,
                )


async def _serve_file(
    collection_id_or_name: str,
    record_id: str,
    filename: str,
    request: Request,
    session: AsyncSession,
    *,
    initial_storage_config: Any,
    thumbnail_write_lease: WriteBarrierLease | None,
):
    collection = await resolve_collection(session, collection_id_or_name)

    row_result = await session.execute(
        text(f'SELECT * FROM "{collection.name}" WHERE "id" = :rid LIMIT 1'),
        {"rid": record_id},
    )
    row = row_result.mappings().first()
    if row is None:
        raise _not_found_error()
    row_dict = dict(row)

    matched_file_field = _find_file_field_for_filename(
        collection.schema or [], row_dict, filename
    )
    if matched_file_field is None:
        raise _not_found_error()
    field_def, is_protected = matched_file_field

    if is_protected:
        file_token = str(request.query_params.get("token", "") or "").strip()
        if not file_token:
            raise _not_found_error()
        token_auth = await _verify_file_token(session, file_token)
        if token_auth is None:
            raise _not_found_error()
        has_view_access = await _check_protected_file_view_rule(
            request, collection, record_id, token_auth
        )
        if not has_view_access:
            raise _not_found_error()

    storage_collection_id = collection.id
    storage_record_id = record_id

    view_storage_context = await _resolve_view_file_storage_context(
        session,
        collection,
        str(field_def.get("name", "") or ""),
        filename,
    )
    if view_storage_context is not None:
        storage_collection_id, storage_record_id = view_storage_context

    try:
        opened_stream = await asyncio.to_thread(
            open_file_stream,
            storage_collection_id,
            storage_record_id,
            filename,
            config=initial_storage_config,
        )
    except StorageSafetyError as exc:
        raise _not_found_error() from exc
    if opened_stream is None:
        raise _not_found_error()

    storage_backend = opened_stream.backend
    storage_config = opened_stream.config
    source_path = opened_stream.storage_path
    if storage_backend == "local" and (storage_config is None or source_path is None):
        opened_stream.close()
        raise _not_found_error()

    served_stream: StorageFileStream = opened_stream
    served_name = filename
    served_file_path: Path | None = source_path
    try:
        original_content_type = await asyncio.to_thread(
            _original_content_type,
            opened_stream,
            storage_collection_id,
            storage_record_id,
            filename,
        )
        served_content_type = original_content_type

        # v0.22.34 apis/file.go l.162-193 == v0.40.6 l.164-196.  Thumbnails
        # are only produced on the local backend (S3 serves the original).
        thumb_size = _first_query_value(request, "thumb") or ""
        if (
            thumb_size
            and storage_backend == "local"
            and thumb_size_allowed(
                thumb_size, _normalize_file_options(field_def).get("thumbs")
            )
            and original_content_type in THUMB_IMAGE_CONTENT_TYPES
        ):
            thumbnail = await _resolve_thumbnail(
                storage_collection_id,
                storage_record_id,
                filename,
                thumb_size,
                original_content_type,
                storage_config,
                thumbnail_write_lease,
            )
            if thumbnail is _THUMBNAIL_CACHE_MISS:
                raise _ThumbnailGenerationRequiresBarrier
            if isinstance(thumbnail, _ServedThumbnail):
                opened_stream.close()
                served_stream = thumbnail.stream
                served_name = thumbnail.served_name
                served_content_type = thumbnail.content_type
                served_file_path = thumbnail.stream.storage_path
    except BaseException as exc:
        served_stream.close()
        if served_stream is not opened_stream:
            opened_stream.close()
        if isinstance(exc, (StorageSafetyError, InvalidFileAttributes)):
            raise _not_found_error() from exc
        raise

    try:
        default_served_path = (
            str(served_file_path) if served_file_path is not None else ""
        )
        event = FileDownloadRequestEvent(
            app=request.app,
            request=request,
            collection=collection,
            record=row_dict,
            file_field=field_def,
            filename=filename,
            served_path=default_served_path,
            served_name=served_name,
            force_download=force_attachment(
                _first_query_value(request, FORCE_ATTACHMENT_PARAM)
            ),
        )
    except BaseException:
        served_stream.close()
        raise

    async def _default_file_download(_: FileDownloadRequestEvent) -> None:
        return None

    try:
        hook_result = await _trigger_file_download_request_hooks(
            request, event, _default_file_download
        )
    except BaseException:
        served_stream.close()
        raise
    if isinstance(hook_result, Response):
        served_stream.close()
        return hook_result

    resolved_served_path_raw = str(event.served_path or "").strip()
    if resolved_served_path_raw and resolved_served_path_raw != default_served_path:
        # A server-side hook explicitly selected a custom path. Hook code is a
        # trusted extension boundary; default storage reads never use this path.
        served_stream.close()
        resolved_served_path = Path(resolved_served_path_raw).expanduser()
        if not resolved_served_path.is_file():
            raise _not_found_error()

        if event.force_download:
            safe_name = os.path.basename(str(event.served_name or filename).strip()) or filename
            return FileResponse(
                str(resolved_served_path),
                filename=safe_name,
                content_disposition_type="attachment",
            )

        return FileResponse(str(resolved_served_path), content_disposition_type="inline")

    # Serve (v0.22.34 tools/filesystem/filesystem.go l.393-430 == v0.40.6
    # l.562-599 for everything below but the quoting of non-token names).
    disposition_name = str(event.served_name or "") or served_name
    disposition = disposition_type(
        served_content_type, forced=bool(event.force_download)
    )
    headers: dict[str, str] = {
        "Content-Disposition": latin1_header_value(
            content_disposition(disposition, disposition_name)
        ),
        "Content-Type": latin1_header_value(
            response_content_type(served_content_type, disposition_name)
        ),
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "Cache-Control": CACHE_CONTROL,
        **FILE_SECURITY_HEADERS,
        "Accept-Ranges": "bytes",
    }
    if served_stream.etag:
        headers["ETag"] = served_stream.etag
    if served_stream.last_modified:
        headers["Last-Modified"] = served_stream.last_modified
    status_code = 200
    range_start = 0
    stream_start = 0
    range_length = served_stream.content_length
    raw_range = str(request.headers.get("range", "") or "").strip()
    raw_if_range = str(request.headers.get("if-range", "") or "").strip()
    if raw_range and raw_if_range and raw_if_range not in {
        served_stream.etag,
        served_stream.last_modified,
    }:
        raw_range = ""
    if raw_range and served_stream.content_length is not None:
        try:
            range_start, range_end = _parse_single_byte_range(
                raw_range,
                served_stream.content_length,
            )
        except ValueError:
            served_stream.close()
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": (f"bytes */{served_stream.content_length}"),
                },
            )
        status_code = 206
        range_length = range_end - range_start + 1
        stream_start = range_start
        headers["Content-Range"] = (
            f"bytes {range_start}-{range_end}/{served_stream.content_length}"
        )
        if served_stream.backend == "s3" and served_stream.etag:
            # Only an original is ever served from S3.
            range_etag = served_stream.etag
            served_stream.close()
            try:
                served_stream = await asyncio.to_thread(
                    open_file_stream,
                    storage_collection_id,
                    storage_record_id,
                    filename,
                    byte_range=(range_start, range_end),
                    config=storage_config,
                    if_match=range_etag,
                )
            except StorageSafetyError as exc:
                raise _not_found_error() from exc
            if served_stream is None:
                raise _not_found_error()
            stream_start = 0
    if range_length is not None:
        headers["Content-Length"] = str(range_length)
    if str(getattr(request, "method", "GET") or "GET").upper() == "HEAD":
        served_stream.close()
        return Response(status_code=status_code, headers=headers)
    return StreamingResponse(
        _stream_storage_file(
            served_stream,
            start=stream_start,
            length=range_length,
        ),
        status_code=status_code,
        headers=headers,
    )
