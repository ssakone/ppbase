"""Files API routes.

Endpoints:
    POST /api/files/token
    GET  /api/files/{collectionIdOrName}/{recordId}/{filename}
"""

from __future__ import annotations

import os
import mimetypes
import re
from pathlib import Path
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, get_settings, require_auth, resolve_collection
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
    get_storage_backend,
    get_storage_path,
    read_file_bytes,
    set_storage_settings,
)
from ppbase.services.record_service import check_record_rule
from ppbase.services.rule_engine import check_rule

router = APIRouter()
_THUMB_OPTION_PATTERN = re.compile(r"^(\d+)x(\d+)([tbf]?)$")
_SUPPORTED_THUMB_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


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


def _try_generate_thumb_file(
    source_path: Path,
    thumb_path: Path,
    thumb_option: str,
) -> bool:
    """Try to generate a thumb image in ``thumb_path``.

    Returns ``True`` on success. Returns ``False`` if generation is unavailable
    or if thumb generation fails for any reason.
    """
    match = _THUMB_OPTION_PATTERN.match(thumb_option)
    if match is None:
        return False

    width = int(match.group(1))
    height = int(match.group(2))
    mode = match.group(3)
    if width <= 0 and height <= 0:
        return False

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except Exception:
        return False

    resample_filter = _image_resample_filter(Image)

    try:
        with Image.open(source_path) as src:
            src.load()
            fmt = src.format
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

            thumb_path.parent.mkdir(parents=True, exist_ok=True)

            save_kwargs: dict[str, Any] = {}
            if (fmt or "").upper() == "JPEG" and result.mode not in {"RGB", "L"}:
                result = result.convert("RGB")
                save_kwargs["quality"] = 85

            result.save(thumb_path, format=fmt, **save_kwargs)
        return thumb_path.is_file()
    except (OSError, ValueError, UnidentifiedImageError):
        return False


def _resolve_thumb_path(
    source_path: Path,
    filename: str,
    field_def: dict[str, Any],
    thumb_option: str | None,
) -> Path | None:
    """Return an existing/generated thumb file path when possible."""
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

    thumb_dir = source_path.parent / f"thumbs_{filename}"
    thumb_path = thumb_dir / f"{thumb_option}_{filename}"
    if thumb_path.is_file():
        return thumb_path

    generated = _try_generate_thumb_file(source_path, thumb_path, thumb_option)
    if generated:
        return thumb_path
    return None


def _normalize_file_options(field_def: dict[str, Any]) -> dict[str, Any]:
    options = field_def.get("options")
    normalized: dict[str, Any] = dict(options) if isinstance(options, dict) else {}
    # Handle flat legacy schemas where file options can be top-level.
    for key in ("maxSelect", "maxSize", "mimeTypes", "thumbs", "protected"):
        if key in field_def and key not in normalized:
            normalized[key] = field_def.get(key)
    return normalized


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
    set_storage_settings(settings)
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
    storage_backend = get_storage_backend()

    source_path = get_storage_path(collection.id, record_id) / filename
    served_file_path: Path | None = None
    if storage_backend != "s3":
        if not source_path.is_file():
            raise _not_found_error()
        served_file_path = (
            _resolve_thumb_path(source_path, filename, field_def, thumb_option) or source_path
        )

    source_bytes: bytes | None = None
    if served_file_path is None:
        source_bytes = read_file_bytes(collection.id, record_id, filename)
        if source_bytes is None:
            raise _not_found_error()

    force_download, download_filename = _parse_download_option(request)
    served_name = (download_filename or filename) if force_download else filename
    event = FileDownloadRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        record=row_dict,
        file_field=field_def,
        filename=filename,
        served_path=str(served_file_path) if served_file_path is not None else "",
        served_name=str(served_name),
        force_download=bool(force_download),
    )

    async def _default_file_download(_: FileDownloadRequestEvent) -> None:
        return None

    hook_result = await _trigger_file_download_request_hooks(
        request, event, _default_file_download
    )
    if isinstance(hook_result, Response):
        return hook_result

    resolved_served_path_raw = str(event.served_path or "").strip()
    if resolved_served_path_raw:
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
        source_bytes = read_file_bytes(collection.id, record_id, effective_filename)
    if source_bytes is None:
        raise _not_found_error()

    if event.force_download:
        safe_name = os.path.basename(str(event.served_name or effective_filename).strip()) or effective_filename
        return Response(
            content=source_bytes,
            media_type=_guess_media_type(effective_filename),
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
            },
        )

    return Response(content=source_bytes, media_type=_guess_media_type(effective_filename))
