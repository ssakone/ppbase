"""Settings API routes.

Endpoints:
    GET   /api/settings   -> read settings (admin only)
    PATCH /api/settings   -> update settings (admin only)
    POST  /api/settings/test/email -> send test email (admin only)
"""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from typing import Any

import smtplib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, require_admin
from ppbase.core.id_generator import generate_id
from ppbase.db.system_tables import ParamRecord
from ppbase.models.field_types import _EMAIL_RE
from ppbase.services.file_storage import configure_storage_runtime_from_settings_payload

router = APIRouter()

# Default settings structure matching PocketBase
_DEFAULT_SETTINGS: dict[str, Any] = {
    "meta": {
        "appName": "PPBase",
        "appURL": "http://localhost:8090",
        "senderName": "Support",
        "senderAddress": "support@example.com",
        "hideControls": False,
    },
    "logs": {
        "maxDays": 7,
        "minLevel": 0,
        "logIP": True,
        "logAuthId": True,
    },
    "smtp": {
        "enabled": False,
        "host": "",
        "port": 587,
        "username": "",
        "password": "",
        "tls": True,
        "authMethod": "PLAIN",
        "localName": "",
    },
    "s3": {
        "enabled": False,
        "bucket": "",
        "region": "",
        "endpoint": "",
        "accessKey": "",
        "secret": "",
        "forcePathStyle": False,
    },
    "backups": {
        "cron": "",
        "cronMaxKeep": 3,
        "s3": {
            "enabled": False,
            "bucket": "",
            "region": "",
            "endpoint": "",
            "accessKey": "",
            "secret": "",
            "forcePathStyle": False,
        },
    },
    "batch": {
        "enabled": True,
        "maxRequests": 50,
        "timeout": 3,
        "maxBodySize": 0,
    },
    "rateLimits": {
        "enabled": False,
        "rules": [],
    },
    "rateLimiting": {
        "enabled": False,
        "maxRequests": 1000,
        "window": 60,
    },
    "trustedProxy": {
        "headers": [],
        "useLeftmostIP": False,
    },
}

_EMAIL_TEMPLATE_SET = frozenset({"verification", "password-reset", "email-change"})


async def _get_or_create_settings(session: AsyncSession) -> ParamRecord:
    """Fetch the settings param row, creating one with defaults if missing."""
    q = select(ParamRecord).where(ParamRecord.key == "settings")
    row = (await session.execute(q)).scalars().first()
    if row is not None:
        return row

    row = ParamRecord(
        id=generate_id(),
        key="settings",
        value=_DEFAULT_SETTINGS,
    )
    session.add(row)
    await session.flush()
    return row


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _error_response(status: int, message: str, data: dict[str, Any] | None = None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "status": status,
            "message": message,
            "data": data or {},
        },
    )


def _smtp_config_from_settings(settings_value: dict[str, Any]) -> dict[str, Any]:
    smtp_raw = settings_value.get("smtp") if isinstance(settings_value, dict) else {}
    meta_raw = settings_value.get("meta") if isinstance(settings_value, dict) else {}
    smtp = smtp_raw if isinstance(smtp_raw, dict) else {}
    meta = meta_raw if isinstance(meta_raw, dict) else {}

    host = str(smtp.get("host", "") or "").strip()
    try:
        port = int(smtp.get("port", 587) or 587)
    except Exception:
        port = 587
    if port <= 0:
        port = 587

    username = str(smtp.get("username", "") or "").strip()
    password = str(smtp.get("password", "") or "")
    tls = bool(smtp.get("tls", True))
    sender_name = str(meta.get("senderName", "") or "").strip()
    sender_address = str(meta.get("senderAddress", "") or "").strip()
    if not sender_address:
        sender_address = username or "noreply@ppbase.local"
    app_name = str(meta.get("appName", "") or "PPBase").strip() or "PPBase"

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "tls": tls,
        "sender_name": sender_name,
        "sender_address": sender_address,
        "app_name": app_name,
    }


def _apply_runtime_settings_to_app(request: Request, settings_value: dict[str, Any]) -> None:
    """Apply runtime overrides that should react immediately to settings PATCH."""
    configure_storage_runtime_from_settings_payload(settings_value)

    runtime_settings = getattr(request.app.state, "settings", None)
    if runtime_settings is None:
        return

    s3_raw = settings_value.get("s3") if isinstance(settings_value, dict) else {}
    s3 = s3_raw if isinstance(s3_raw, dict) else {}

    endpoint = str(s3.get("endpoint", "") or "").strip()
    bucket = str(s3.get("bucket", "") or "").strip()
    region = str(s3.get("region", "") or "").strip()
    access_key = str(s3.get("accessKey", "") or "").strip()
    secret_key = str(s3.get("secret", "") or "").strip()
    enabled_raw = s3.get("enabled")
    enabled = bool(enabled_raw) if enabled_raw is not None else bool(bucket and access_key and secret_key)

    setattr(runtime_settings, "storage_backend", "s3" if enabled else "local")
    setattr(runtime_settings, "s3_endpoint", endpoint)
    setattr(runtime_settings, "s3_bucket", bucket)
    setattr(runtime_settings, "s3_region", region)
    setattr(runtime_settings, "s3_access_key", access_key)
    setattr(runtime_settings, "s3_secret_key", secret_key)
    setattr(runtime_settings, "s3_force_path_style", bool(s3.get("forcePathStyle", False)))


def _test_email_subject(template: str, app_name: str) -> str:
    normalized = template.strip().lower()
    if normalized == "verification":
        return f"{app_name} test email: verification"
    if normalized == "password-reset":
        return f"{app_name} test email: password reset"
    return f"{app_name} test email: email change"


def _test_email_body(
    *,
    template: str,
    collection: str | None,
    app_name: str,
) -> str:
    template_label = template.strip().lower()
    lines = [
        f"This is a PPBase SMTP test message for template '{template_label}'.",
        "",
        f"Application: {app_name}",
    ]
    if collection:
        lines.append(f"Collection: {collection}")
    lines.extend(
        [
            "",
            "Your SMTP configuration is valid and can deliver emails.",
        ]
    )
    return "\n".join(lines)


def _send_smtp_message(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    tls: bool,
    sender_name: str,
    sender_address: str,
    email: str,
    subject: str,
    body: str,
) -> None:
    from_value = sender_address
    if sender_name:
        from_value = f"{sender_name} <{sender_address}>"

    msg = EmailMessage()
    msg["From"] = from_value
    msg["To"] = email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=12) as server:
        server.ehlo()
        if tls:
            server.starttls()
            server.ehlo()
        if username:
            server.login(username, password)
        server.send_message(msg)


@router.get("")
async def get_settings(
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Return the current application settings."""
    row = await _get_or_create_settings(session)
    await session.commit()
    return row.value or _DEFAULT_SETTINGS


@router.patch("")
async def update_settings(
    request: Request,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Partially update the application settings."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "Invalid settings payload.",
                "data": {},
            },
        )

    row = await _get_or_create_settings(session)
    current = row.value or dict(_DEFAULT_SETTINGS)
    merged = _deep_merge(current, body)
    row.value = merged
    await session.flush()
    await session.commit()

    _apply_runtime_settings_to_app(request, merged)

    current_version = int(getattr(request.app.state, "rate_limit_settings_version", 0) or 0)
    request.app.state.rate_limit_settings_version = current_version + 1
    return merged


@router.post("/test/email", status_code=204)
async def send_test_email(
    request: Request,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Send a test email using the currently persisted SMTP settings."""
    body = await request.json()
    if not isinstance(body, dict):
        raise _error_response(
            400,
            "Failed to send the test email.",
            {
                "body": {
                    "code": "validation_invalid_value",
                    "message": "Invalid request body.",
                }
            },
        )

    email = str(body.get("email", "") or "").strip()
    template = str(body.get("template", "") or "").strip().lower()
    collection = str(body.get("collection", "") or "").strip() or None

    errors: dict[str, Any] = {}
    if not email:
        errors["email"] = {
            "code": "validation_required",
            "message": "Missing required value.",
        }
    elif not _EMAIL_RE.match(email):
        errors["email"] = {
            "code": "validation_invalid_email",
            "message": "Must be a valid email address.",
        }

    if not template:
        errors["template"] = {
            "code": "validation_required",
            "message": "Missing required value.",
        }
    elif template not in _EMAIL_TEMPLATE_SET:
        errors["template"] = {
            "code": "validation_invalid_value",
            "message": "Invalid template value.",
        }

    if errors:
        raise _error_response(400, "Failed to send the test email.", errors)

    row = await _get_or_create_settings(session)
    settings_value = row.value or dict(_DEFAULT_SETTINGS)
    smtp_config = _smtp_config_from_settings(settings_value)

    if not smtp_config["host"]:
        raise _error_response(
            400,
            "Failed to send the test email.",
            {
                "smtp": {
                    "code": "validation_required",
                    "message": "SMTP host is not configured.",
                }
            },
        )

    subject = _test_email_subject(template, smtp_config["app_name"])
    body_text = _test_email_body(
        template=template,
        collection=collection,
        app_name=smtp_config["app_name"],
    )

    try:
        await asyncio.to_thread(
            _send_smtp_message,
            host=smtp_config["host"],
            port=smtp_config["port"],
            username=smtp_config["username"],
            password=smtp_config["password"],
            tls=smtp_config["tls"],
            sender_name=smtp_config["sender_name"],
            sender_address=smtp_config["sender_address"],
            email=email,
            subject=subject,
            body=body_text,
        )
    except Exception as exc:
        raise _error_response(
            400,
            "Failed to send the test email.",
            {
                "smtp": {
                    "code": "smtp_send_failed",
                    "message": str(exc),
                }
            },
        ) from exc

    # PocketBase-compatible empty 204 response body.
    return Response(status_code=204)
