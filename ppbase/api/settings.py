"""Settings API routes.

Endpoints:
    GET   /api/settings   -> read settings (admin only)
    PATCH /api/settings   -> update settings (admin only)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, require_admin
from ppbase.core.id_generator import generate_id
from ppbase.db.system_tables import ParamRecord

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
    "trustedProxy": {
        "headers": [],
        "useLeftmostIP": False,
    },
}


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
    return merged
