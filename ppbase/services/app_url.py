"""Base URL of every e-mailed link: the stored application URL, nothing else.

PocketBase builds the action URL of every e-mail from the application URL
setting alone: v0.22.34 ``mails/admin.go:22-26`` (admin password reset) and
``mails/record.go:175-184`` (record verification, password reset and e-mail
change templates), v0.40.6 ``mails/record.go:265-266``.  The setting is
required (v0.22.34 ``models/settings/settings.go:520``) and defaults to
``http://localhost:8090`` (v0.22.34 ``models/settings/settings.go:77``,
v0.40.6 ``core/settings_model.go:152``).

Request headers (``Host``, ``X-Forwarded-*``) are chosen by the client.  A
link built from them can point to a site of the requester, which then reads
the token from the URL fragment when the account owner clicks it, so they are
never used; this was measured on the PocketBase 0.22.34 binary, which ignores
forged ``Host`` and ``X-Forwarded-*`` headers for the admin and record links.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ppbase.db.system_tables import ParamRecord

DEFAULT_APP_URL = "http://localhost:8090"


def app_url_from_settings(settings_value: Any) -> str:
    """Return ``meta.appURL`` of a stored settings value, else the default.

    A missing settings row, a missing or empty ``appURL`` and a value that is
    not an absolute URL all give the default, as PocketBase starts from its
    default settings and rejects such a value (``validation.Required``,
    ``is.URL``).  The trailing slash is dropped so that the link paths can be
    appended, which gives the same links as PocketBase's ``rest.NormalizeUrl``.
    """
    meta = settings_value.get("meta") if isinstance(settings_value, Mapping) else None
    raw = meta.get("appURL") if isinstance(meta, Mapping) else None
    if not isinstance(raw, str):
        return DEFAULT_APP_URL
    try:
        parsed = urlsplit(raw.strip())
    except ValueError:
        return DEFAULT_APP_URL
    if not parsed.scheme or not parsed.netloc:
        return DEFAULT_APP_URL
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


async def resolve_app_url(engine: AsyncEngine) -> str:
    """Read the stored application URL (see :func:`app_url_from_settings`)."""
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        value = (
            await session.execute(select(ParamRecord.value).where(ParamRecord.key == "settings"))
        ).scalar_one_or_none()
    return app_url_from_settings(value)
