"""Files API routes.

Endpoints:
    POST /api/files/token
    GET  /api/files/{collectionIdOrName}/{recordId}/{filename}
"""

from __future__ import annotations

import asyncio
import os
import mimetypes
import re
from io import BytesIO
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, get_settings, require_auth, resolve_collection
from ppbase.core.storage_safety import StorageSafetyError
from ppbase.db.engine import get_engine
from ppbase.db.system_tables import CollectionRecord, SuperuserRecord
from ppbase.ext.events import FileDownloadRequestEvent, FileTokenRequestEvent
from ppbase.ext.registry import (
    HOOK_FILE_DOWNLOAD_REQUEST,
    HOOK_FILE_TOKEN_REQUEST,
    get_extension_registry,
)
from ppbase.services.auth_service import create_token, get_collection_token_config
from ppbase.services.file_storage import (
    StorageFileStream,
    open_file_stream,
    pin_storage_config,
    read_local_storage_variant_bytes,
    write_local_storage_variant_bytes,
)
from ppbase.services.record_service import check_record_rule
from ppbase.services.rule_engine import check_rule
from ppbase.services.write_barrier import (
    WriteBarrierLease,
    mutation_write_barrier_on_connection,
)

router = APIRouter()
_THUMB_OPTION_PATTERN = re.compile(r"^(\d+)x(\d+)([tbf]?)$")
_SUPPORTED_THUMB_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
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


class _ThumbnailGenerationRequiresBarrier(Exception):
    """Restart a cache-miss request after acquiring the shared barrier."""


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


def _parse_download_option(request: Request) -> tuple[bool, str | None]:
    """Parse ``download`` query parameter.

    Returns:
        ``(force_download, custom_filename)``
    """
    raw = request.query_params.get("download")
    if raw is None:
        return False, None

    value = str(raw).strip()
    if value.lower() in {"0", "f", "false", "no", "off"}:
        return False, None
    if value == "" or value.lower() in {"1", "t", "true", "yes", "on"}:
        return True, None

    # Accept a custom filename as non-empty value.
    custom_filename = os.path.basename(value)
    if not custom_filename:
        return True, None
    return True, custom_filename


def _parse_thumb_option(request: Request) -> str | None:
    """Parse and normalize the optional ``thumb`` query parameter."""
    raw = request.query_params.get("thumb")
    if raw is None:
        return None

    value = str(raw).strip().lower()
    if not value:
        return None
    return value


def _guess_media_type(filename: str) -> str:
    media_type, _encoding = mimetypes.guess_type(filename)
    if media_type:
        return media_type
    return "application/octet-stream"


def _attachment_content_disposition(filename: str) -> str:
    """Build an injection-safe RFC 5987 attachment header value."""
    cleaned = "".join(
        "_" if ord(char) < 32 or ord(char) == 127 else char
        for char in os.path.basename(filename)
    ).strip()
    if not cleaned:
        cleaned = "download"
    ascii_name = cleaned.encode("ascii", "ignore").decode("ascii") or "download"
    ascii_name = ascii_name.replace("\\", "_").replace('"', "'")
    encoded_name = quote(cleaned, safe="")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{encoded_name}"
    )


def _normalize_thumb_options(options: dict[str, Any]) -> set[str]:
    """Return normalized configured thumb size presets for a file field."""
    raw = options.get("thumbs")
    if isinstance(raw, str):
        candidate = raw.strip().lower()
        return {candidate} if candidate else set()
    if isinstance(raw, list):
        values: set[str] = set()
        for item in raw:
            candidate = str(item or "").strip().lower()
            if candidate:
                values.add(candidate)
        return values
    return set()


def _is_supported_thumb_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in _SUPPORTED_THUMB_IMAGE_EXTENSIONS


def _image_resample_filter(image_module: Any) -> Any:
    resampling = getattr(image_module, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return image_module.LANCZOS


def _try_generate_thumb_bytes(
    source_bytes: bytes,
    thumb_option: str,
) -> bytes | None:
    """Try to generate thumbnail bytes without path-based file access.

    Returns ``None`` if generation is unavailable or fails for any reason.
    """
    match = _THUMB_OPTION_PATTERN.match(thumb_option)
    if match is None:
        return None

    width = int(match.group(1))
    height = int(match.group(2))
    mode = match.group(3)
    if width <= 0 and height <= 0:
        return None

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except Exception:
        return None

    resample_filter = _image_resample_filter(Image)

    try:
        with Image.open(BytesIO(source_bytes)) as src:
            src.load()
            fmt = src.format
            if not fmt:
                return None
            result = src

            if width == 0 and height > 0:
                new_width = max(1, int(round(src.width * (height / max(src.height, 1)))))
                result = src.resize((new_width, height), resample_filter)
            elif height == 0 and width > 0:
                new_height = max(1, int(round(src.height * (width / max(src.width, 1)))))
                result = src.resize((width, new_height), resample_filter)
            elif width > 0 and height > 0 and mode == "f":
                result = ImageOps.contain(src, (width, height), method=resample_filter)
            elif width > 0 and height > 0:
                centering = (0.5, 0.5)
                if mode == "t":
                    centering = (0.5, 0.0)
                elif mode == "b":
                    centering = (0.5, 1.0)
                result = ImageOps.fit(
                    src,
                    (width, height),
                    method=resample_filter,
                    centering=centering,
                )

            save_kwargs: dict[str, Any] = {}
            if (fmt or "").upper() == "JPEG" and result.mode not in {"RGB", "L"}:
                result = result.convert("RGB")
                save_kwargs["quality"] = 85

            output = BytesIO()
            try:
                result.save(output, format=fmt, **save_kwargs)
                return output.getvalue()
            finally:
                if result is not src:
                    result.close()
    except (OSError, ValueError, UnidentifiedImageError):
        return None


async def _resolve_thumb_bytes(
    collection_id: str,
    record_id: str,
    filename: str,
    field_def: dict[str, Any],
    thumb_option: str | None,
    source_loader: Callable[[], bytes | None],
    storage_config: Any,
    lease: WriteBarrierLease | None,
) -> tuple[bytes, str, str] | None:
    """Return secure cached/generated thumbnail bytes and variant names."""
    if not thumb_option:
        return None
    if _THUMB_OPTION_PATTERN.match(thumb_option) is None:
        return None
    if not _is_supported_thumb_image(filename):
        return None

    options = _normalize_file_options(field_def)
    allowed_thumbs = _normalize_thumb_options(options)
    if thumb_option not in allowed_thumbs:
        return None

    thumb_dir_name = f"thumbs_{filename}"
    thumb_filename = f"{thumb_option}_{filename}"
    try:
        cached = await asyncio.to_thread(
            read_local_storage_variant_bytes,
            collection_id,
            record_id,
            thumb_dir_name,
            thumb_filename,
            config=storage_config,
        )
    except (OSError, StorageSafetyError):
        return None
    if cached is not None:
        return cached, thumb_dir_name, thumb_filename
    if lease is None:
        return None

    source_bytes = await asyncio.to_thread(source_loader)
    if source_bytes is None:
        return None
    generated = await asyncio.to_thread(
        _try_generate_thumb_bytes,
        source_bytes,
        thumb_option,
    )
    if generated is None:
        return None
    try:
        write_local_storage_variant_bytes(
            collection_id,
            record_id,
            thumb_dir_name,
            thumb_filename,
            generated,
            lease=lease,
            config=storage_config,
        )
    except (OSError, StorageSafetyError):
        # The generated bytes are still safe to serve even when caching loses
        # a race or an unsafe pre-existing variant path is rejected.
        pass
    return generated, thumb_dir_name, thumb_filename


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
            max_select = options.get("maxSelect", 1) or 1
            table_name = _quote_ident(candidate.name)
            column_name = _quote_ident(field_name)
            if max_select > 1:
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


async def _get_superusers_collection(session: AsyncSession) -> CollectionRecord | None:
    stmt = select(CollectionRecord).where(CollectionRecord.name == "_superusers")
    return (await session.execute(stmt)).scalars().first()


async def _get_auth_record_token_key(
    session: AsyncSession,
    collection: CollectionRecord,
    record_id: str,
) -> str | None:
    sql = text(
        f'SELECT "token_key" FROM "{collection.name}" WHERE "id" = :rid LIMIT 1'
    )
    result = await session.execute(sql, {"rid": record_id})
    row = result.mappings().first()
    if row is None:
        return None
    token_key = row.get("token_key")
    if token_key is None:
        return None
    return str(token_key)


async def _create_file_token_for_auth(
    session: AsyncSession,
    auth_payload: dict[str, Any],
) -> str:
    auth_type = str(auth_payload.get("type", "") or "")
    auth_id = str(auth_payload.get("id", "") or "")
    if not auth_type or not auth_id:
        raise ValueError("Invalid auth payload.")

    if auth_type == "admin":
        admin = await session.get(SuperuserRecord, auth_id)
        if admin is None:
            raise ValueError("Missing superuser.")

        superusers_collection = await _get_superusers_collection(session)
        if superusers_collection is None:
            raise ValueError("Missing _superusers collection.")

        file_secret, file_duration = get_collection_token_config(
            superusers_collection, "fileToken"
        )
        payload = {
            "id": admin.id,
            "type": "admin",
            "for": "file",
        }
        return create_token(payload, str(admin.token_key) + file_secret, file_duration)

    if auth_type == "authRecord":
        collection_id = str(auth_payload.get("collectionId", "") or "")
        if not collection_id:
            raise ValueError("Missing collectionId.")

        auth_collection = await session.get(CollectionRecord, collection_id)
        if auth_collection is None:
            raise ValueError("Missing auth collection.")

        token_key = await _get_auth_record_token_key(session, auth_collection, auth_id)
        if not token_key:
            raise ValueError("Missing auth record.")

        file_secret, file_duration = get_collection_token_config(
            auth_collection, "fileToken"
        )
        payload = {
            "id": auth_id,
            "type": "authRecord",
            "collectionId": auth_collection.id,
            "for": "file",
        }
        return create_token(payload, token_key + file_secret, file_duration)

    raise ValueError("Unsupported auth token type.")


async def _verify_file_token(
    session: AsyncSession,
    file_token: str,
) -> dict[str, Any] | None:
    try:
        unverified = jwt.decode(file_token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None

    if unverified.get("for") != "file":
        return None

    token_type = str(unverified.get("type", "") or "")
    token_id = str(unverified.get("id", "") or "")
    if not token_type or not token_id:
        return None

    if token_type == "admin":
        admin = await session.get(SuperuserRecord, token_id)
        if admin is None:
            return None

        superusers_collection = await _get_superusers_collection(session)
        if superusers_collection is None:
            return None

        file_secret, _ = get_collection_token_config(superusers_collection, "fileToken")
        try:
            jwt.decode(
                file_token,
                str(admin.token_key) + file_secret,
                algorithms=["HS256"],
            )
        except jwt.InvalidTokenError:
            return None
        return {
            "id": admin.id,
            "email": admin.email,
            "type": "admin",
        }

    if token_type == "authRecord":
        collection_id = str(unverified.get("collectionId", "") or "")
        if not collection_id:
            return None

        auth_collection = await session.get(CollectionRecord, collection_id)
        if auth_collection is None:
            return None

        token_key = await _get_auth_record_token_key(session, auth_collection, token_id)
        if not token_key:
            return None

        file_secret, _ = get_collection_token_config(auth_collection, "fileToken")
        try:
            jwt.decode(file_token, token_key + file_secret, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            return None
        return {
            "id": token_id,
            "type": "authRecord",
            "collectionId": auth_collection.id,
            "collectionName": auth_collection.name,
        }

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

    if auth_type == "authRecord":
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


@router.get("/{collection_id_or_name}/{record_id}/{filename}")
async def serve_file(
    collection_id_or_name: str,
    record_id: str,
    filename: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Any = Depends(get_settings),
):
    """Serve a file from local or S3-compatible storage."""
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

    thumb_option = _parse_thumb_option(request)
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
    served_file_path: Path | None = None
    if storage_backend == "local" and (storage_config is None or source_path is None):
        opened_stream.close()
        raise _not_found_error()

    source_bytes: bytes | None = None
    if source_path is not None:
        served_file_path = source_path
        try:
            thumb_result = None
            if thumb_option:
                thumb_result = await _resolve_thumb_bytes(
                    storage_collection_id,
                    storage_record_id,
                    filename,
                    field_def,
                    thumb_option,
                    lambda: bytes(opened_stream.stream.read()),
                    storage_config,
                    thumbnail_write_lease,
                )
                if thumb_result is None and thumbnail_write_lease is None:
                    raise _ThumbnailGenerationRequiresBarrier
        except BaseException as exc:
            opened_stream.close()
            if isinstance(exc, StorageSafetyError):
                raise _not_found_error() from exc
            raise
        if thumb_result is not None:
            source_bytes, thumb_dir_name, thumb_filename = thumb_result
            served_file_path = source_path.parent / thumb_dir_name / thumb_filename
            opened_stream.close()
            opened_stream = None
        elif thumb_option:
            # Thumbnail generation may have consumed the source stream before
            # falling back to the original. Reopen it from the anchored path.
            opened_stream.close()
            try:
                opened_stream = await asyncio.to_thread(
                    open_file_stream,
                    storage_collection_id,
                    storage_record_id,
                    filename,
                    config=storage_config,
                )
            except StorageSafetyError as exc:
                raise _not_found_error() from exc
            if opened_stream is None:
                raise _not_found_error()

    try:
        default_served_path = (
            str(served_file_path) if served_file_path is not None else ""
        )
        force_download, download_filename = _parse_download_option(request)
        served_name = (download_filename or filename) if force_download else filename
        event = FileDownloadRequestEvent(
            app=request.app,
            request=request,
            collection=collection,
            record=row_dict,
            file_field=field_def,
            filename=filename,
            served_path=default_served_path,
            served_name=str(served_name),
            force_download=bool(force_download),
        )
    except BaseException:
        if opened_stream is not None:
            opened_stream.close()
        raise

    async def _default_file_download(_: FileDownloadRequestEvent) -> None:
        return None

    try:
        hook_result = await _trigger_file_download_request_hooks(
            request, event, _default_file_download
        )
    except BaseException:
        if opened_stream is not None:
            opened_stream.close()
        raise
    if isinstance(hook_result, Response):
        if opened_stream is not None:
            opened_stream.close()
        return hook_result

    resolved_served_path_raw = str(event.served_path or "").strip()
    if resolved_served_path_raw and resolved_served_path_raw != default_served_path:
        # A server-side hook explicitly selected a custom path. Hook code is a
        # trusted extension boundary; default storage reads never use this path.
        if opened_stream is not None:
            opened_stream.close()
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

    effective_filename = str(event.filename or filename).strip() or filename
    if source_bytes is None:
        if opened_stream is None:  # pragma: no cover - internal invariant
            raise _not_found_error()
        headers: dict[str, str] = {"Accept-Ranges": "bytes"}
        if opened_stream.etag:
            headers["ETag"] = opened_stream.etag
        if opened_stream.last_modified:
            headers["Last-Modified"] = opened_stream.last_modified
        status_code = 200
        range_start = 0
        stream_start = 0
        range_length = opened_stream.content_length
        raw_range = str(request.headers.get("range", "") or "").strip()
        raw_if_range = str(request.headers.get("if-range", "") or "").strip()
        if raw_range and raw_if_range and raw_if_range not in {
            opened_stream.etag,
            opened_stream.last_modified,
        }:
            raw_range = ""
        if raw_range and opened_stream.content_length is not None:
            try:
                range_start, range_end = _parse_single_byte_range(
                    raw_range,
                    opened_stream.content_length,
                )
            except ValueError:
                opened_stream.close()
                return Response(
                    status_code=416,
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Range": (
                            f"bytes */{opened_stream.content_length}"
                        ),
                    },
                )
            status_code = 206
            range_length = range_end - range_start + 1
            stream_start = range_start
            headers["Content-Range"] = (
                f"bytes {range_start}-{range_end}/{opened_stream.content_length}"
            )
            if opened_stream.backend == "s3" and opened_stream.etag:
                range_etag = opened_stream.etag
                opened_stream.close()
                try:
                    opened_stream = await asyncio.to_thread(
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
                if opened_stream is None:
                    raise _not_found_error()
                stream_start = 0
        if range_length is not None:
            headers["Content-Length"] = str(range_length)
        if event.force_download:
            safe_name = os.path.basename(
                str(event.served_name or effective_filename).strip()
            ) or effective_filename
            headers["Content-Disposition"] = _attachment_content_disposition(
                safe_name
            )
        return StreamingResponse(
            _stream_storage_file(
                opened_stream,
                start=stream_start,
                length=range_length,
            ),
            status_code=status_code,
            media_type=_guess_media_type(effective_filename),
            headers=headers,
        )

    if event.force_download:
        safe_name = os.path.basename(str(event.served_name or effective_filename).strip()) or effective_filename
        return Response(
            content=source_bytes,
            media_type=_guess_media_type(effective_filename),
            headers={
                "Content-Disposition": _attachment_content_disposition(safe_name),
            },
        )

    return Response(content=source_bytes, media_type=_guess_media_type(effective_filename))
