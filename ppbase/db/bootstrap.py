"""Bootstrap system collections in the _collections table.

PocketBase v0.23+ exposes admins as a special ``_superusers`` auth collection
and creates several additional system collections at startup. This module
ensures all required system collection records exist and their physical
tables are created.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TypeAlias

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from ppbase.core.id_generator import generate_id
from ppbase.db.schema_manager import create_collection_table
from ppbase.db.system_tables import CollectionRecord
from ppbase.services.auth_service import generate_default_auth_options

logger = logging.getLogger(__name__)

DatabaseBind: TypeAlias = AsyncEngine | AsyncConnection | AsyncSession


# ---------------------------------------------------------------------------
# Helper: check if a physical table exists
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _bind_connection(
    bind: DatabaseBind,
    *,
    transactional: bool = False,
) -> AsyncIterator[AsyncConnection]:
    """Yield a connection without nesting a supplied transaction.

    Sessions and connections are borrowed from their caller and are therefore
    never committed or closed here. The engine fallback preserves the public
    helpers' standalone behavior for existing callers.
    """
    if isinstance(bind, AsyncSession):
        yield await bind.connection()
        return

    if isinstance(bind, AsyncConnection):
        yield bind
        return

    context = bind.begin() if transactional else bind.connect()
    async with context as connection:
        yield connection


async def _table_exists(bind: DatabaseBind, table_name: str) -> bool:
    """Check whether a table exists in the public schema."""
    async with _bind_connection(bind) as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :name"
            ),
            {"name": table_name},
        )
        return result.first() is not None


async def _column_exists(
    bind: DatabaseBind,
    table_name: str,
    column_name: str,
) -> bool:
    """Check whether a column exists in the public schema."""
    async with _bind_connection(bind) as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = :table_name "
                "AND column_name = :column_name"
            ),
            {"table_name": table_name, "column_name": column_name},
        )
        return result.first() is not None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def bootstrap_system_collections(
    session: AsyncSession,
    engine: DatabaseBind,
) -> None:
    """Ensure all system collections exist in ``_collections``.

    Called once at startup inside an active transaction. ``engine`` is retained
    for API compatibility, while all metadata checks and DDL reuse ``session``
    so the caller owns the single bootstrap transaction.
    """
    await ensure_superusers_collection(session)
    await ensure_external_auths_collection(session, engine)
    await ensure_mfas_collection(session, engine)
    await ensure_otps_collection(session, engine)
    await ensure_auth_origins_collection(session, engine)
    await ensure_users_collection(session, engine)
    await ensure_request_logs_columns(session)


# ---------------------------------------------------------------------------
# _requests schema backfill
# ---------------------------------------------------------------------------

async def ensure_request_logs_columns(engine: DatabaseBind) -> None:
    """Backfill optional payload columns for the ``_requests`` table."""
    async with _bind_connection(engine, transactional=True) as conn:
        if not await _table_exists(conn, "_requests"):
            return

        if not await _column_exists(conn, "_requests", "request_body"):
            await conn.execute(
                text(
                    'ALTER TABLE "_requests" '
                    'ADD COLUMN IF NOT EXISTS "request_body" JSONB NULL'
                )
            )
            logger.info("Added _requests.request_body column")

        if not await _column_exists(conn, "_requests", "response_body"):
            await conn.execute(
                text(
                    'ALTER TABLE "_requests" '
                    'ADD COLUMN IF NOT EXISTS "response_body" JSONB NULL'
                )
            )
            logger.info("Added _requests.response_body column")


# ---------------------------------------------------------------------------
# _superusers
# ---------------------------------------------------------------------------

async def ensure_superusers_collection(session: AsyncSession) -> None:
    """Create or update the ``_superusers`` system auth collection.

    If the record already exists but has empty options, backfill with
    default auth options (per-collection secrets).
    """
    stmt = select(CollectionRecord).where(CollectionRecord.name == "_superusers")
    existing = (await session.execute(stmt)).scalars().first()

    if existing is not None:
        # Backfill options if empty
        opts = existing.options or {}
        if not opts.get("authToken"):
            existing.options = generate_default_auth_options(is_superusers=True)
            await session.flush()
        return

    now = datetime.now(timezone.utc)
    record = CollectionRecord(
        id=generate_id(),
        name="_superusers",
        type="auth",
        system=True,
        schema=[
            {"name": "email", "type": "email", "required": True, "options": {}},
            {"name": "avatar", "type": "number", "required": False, "options": {}},
        ],
        indexes=[],
        list_rule=None,
        view_rule=None,
        create_rule=None,
        update_rule=None,
        delete_rule=None,
        options=generate_default_auth_options(is_superusers=True),
        created=now,
        updated=now,
    )
    session.add(record)
    await session.flush()


# ---------------------------------------------------------------------------
# _externalAuths
# ---------------------------------------------------------------------------

async def ensure_external_auths_collection(
    session: AsyncSession,
    engine: DatabaseBind,
) -> None:
    """Create the ``_externalAuths`` system collection.

    Handles migration from the old collection-record name
    ``_external_auths``. The physical snake_case table is an ORM system table
    still used by OAuth and must never be renamed; the dynamic camelCase table
    is created separately for the PocketBase-compatible collection metadata.
    """
    bind: DatabaseBind = session

    # Check for new name first
    stmt = select(CollectionRecord).where(CollectionRecord.name == "_externalAuths")
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        if not await _table_exists(bind, "_externalAuths"):
            await create_collection_table(bind, existing)
        return

    # Check for old name and migrate
    old_stmt = select(CollectionRecord).where(
        CollectionRecord.name == "_external_auths"
    )
    old_record = (await session.execute(old_stmt)).scalars().first()
    if old_record is not None:
        old_record.name = "_externalAuths"
        old_record.updated = datetime.now(timezone.utc)
        await session.flush()
        # ``_external_auths`` remains the ORM table used by oauth2_service.
        # Only the collection metadata name changes; create its distinct
        # PocketBase-compatible dynamic table when missing.
        if not await _table_exists(bind, "_externalAuths"):
            await create_collection_table(bind, old_record)
        logger.info("Migrated _external_auths -> _externalAuths")
        return

    now = datetime.now(timezone.utc)
    record = CollectionRecord(
        id=generate_id(),
        name="_externalAuths",
        type="base",
        system=True,
        schema=[
            {"name": "collectionRef", "type": "text", "required": True, "options": {}},
            {"name": "recordRef", "type": "text", "required": True, "options": {}},
            {"name": "provider", "type": "text", "required": True, "options": {}},
            {"name": "providerId", "type": "text", "required": True, "options": {}},
        ],
        indexes=[
            'CREATE UNIQUE INDEX IF NOT EXISTS "idx__externalAuths_crp" '
            'ON "_externalAuths" ("collectionRef", "recordRef", "provider")',
            'CREATE INDEX IF NOT EXISTS "idx__externalAuths_cr" '
            'ON "_externalAuths" ("collectionRef", "recordRef")',
        ],
        list_rule=None,
        view_rule=None,
        create_rule=None,
        update_rule=None,
        delete_rule=None,
        options={},
        created=now,
        updated=now,
    )
    session.add(record)
    await session.flush()

    if not await _table_exists(bind, "_externalAuths"):
        await create_collection_table(bind, record)


# ---------------------------------------------------------------------------
# _mfas
# ---------------------------------------------------------------------------

async def ensure_mfas_collection(
    session: AsyncSession,
    engine: DatabaseBind,
) -> None:
    """Create the ``_mfas`` system collection for multi-factor auth records."""
    bind: DatabaseBind = session
    stmt = select(CollectionRecord).where(CollectionRecord.name == "_mfas")
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        if not await _table_exists(bind, "_mfas"):
            await create_collection_table(bind, existing)
        return

    owner_rule = (
        "@request.auth.id != '' "
        "&& recordRef = @request.auth.id "
        "&& collectionRef = @request.auth.collectionId"
    )

    now = datetime.now(timezone.utc)
    record = CollectionRecord(
        id=generate_id(),
        name="_mfas",
        type="base",
        system=True,
        schema=[
            {"name": "collectionRef", "type": "text", "required": True, "options": {}},
            {"name": "recordRef", "type": "text", "required": True, "options": {}},
            {"name": "method", "type": "text", "required": True, "options": {}},
        ],
        indexes=[
            'CREATE INDEX IF NOT EXISTS "idx__mfas_cr" '
            'ON "_mfas" ("collectionRef", "recordRef")',
        ],
        list_rule=owner_rule,
        view_rule=owner_rule,
        create_rule=None,
        update_rule=None,
        delete_rule=None,
        options={},
        created=now,
        updated=now,
    )
    session.add(record)
    await session.flush()

    if not await _table_exists(bind, "_mfas"):
        await create_collection_table(bind, record)


# ---------------------------------------------------------------------------
# _otps
# ---------------------------------------------------------------------------

async def ensure_otps_collection(
    session: AsyncSession,
    engine: DatabaseBind,
) -> None:
    """Create the ``_otps`` system collection for one-time password records."""
    bind: DatabaseBind = session
    stmt = select(CollectionRecord).where(CollectionRecord.name == "_otps")
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        if not await _table_exists(bind, "_otps"):
            await create_collection_table(bind, existing)
        return

    owner_rule = (
        "@request.auth.id != '' "
        "&& recordRef = @request.auth.id "
        "&& collectionRef = @request.auth.collectionId"
    )

    now = datetime.now(timezone.utc)
    record = CollectionRecord(
        id=generate_id(),
        name="_otps",
        type="base",
        system=True,
        schema=[
            {"name": "collectionRef", "type": "text", "required": True, "options": {}},
            {"name": "recordRef", "type": "text", "required": True, "options": {}},
            {"name": "password", "type": "password", "required": False, "options": {}},
            {"name": "sentTo", "type": "text", "required": False, "options": {}},
        ],
        indexes=[
            'CREATE INDEX IF NOT EXISTS "idx__otps_cr" '
            'ON "_otps" ("collectionRef", "recordRef")',
        ],
        list_rule=owner_rule,
        view_rule=owner_rule,
        create_rule=None,
        update_rule=None,
        delete_rule=None,
        options={},
        created=now,
        updated=now,
    )
    session.add(record)
    await session.flush()

    if not await _table_exists(bind, "_otps"):
        await create_collection_table(bind, record)


# ---------------------------------------------------------------------------
# _authOrigins
# ---------------------------------------------------------------------------

async def ensure_auth_origins_collection(
    session: AsyncSession,
    engine: DatabaseBind,
) -> None:
    """Create the ``_authOrigins`` system collection for tracking auth origins."""
    bind: DatabaseBind = session
    stmt = select(CollectionRecord).where(CollectionRecord.name == "_authOrigins")
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        if not await _table_exists(bind, "_authOrigins"):
            await create_collection_table(bind, existing)
        return

    owner_rule = (
        "@request.auth.id != '' "
        "&& recordRef = @request.auth.id "
        "&& collectionRef = @request.auth.collectionId"
    )

    now = datetime.now(timezone.utc)
    record = CollectionRecord(
        id=generate_id(),
        name="_authOrigins",
        type="base",
        system=True,
        schema=[
            {"name": "collectionRef", "type": "text", "required": True, "options": {}},
            {"name": "recordRef", "type": "text", "required": True, "options": {}},
            {"name": "fingerprint", "type": "text", "required": True, "options": {}},
        ],
        indexes=[
            'CREATE UNIQUE INDEX IF NOT EXISTS "idx__authOrigins_crf" '
            'ON "_authOrigins" ("collectionRef", "recordRef", "fingerprint")',
        ],
        list_rule=owner_rule,
        view_rule=owner_rule,
        create_rule=None,
        update_rule=None,
        delete_rule=owner_rule,
        options={},
        created=now,
        updated=now,
    )
    session.add(record)
    await session.flush()

    if not await _table_exists(bind, "_authOrigins"):
        await create_collection_table(bind, record)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

async def ensure_users_collection(
    session: AsyncSession,
    engine: DatabaseBind,
) -> None:
    """Create the default ``users`` auth collection.

    This is a non-system collection (users can customize it) with a fixed ID
    so that client SDKs can rely on it.
    """
    bind: DatabaseBind = session
    stmt = select(CollectionRecord).where(CollectionRecord.name == "users")
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        if not await _table_exists(bind, "users"):
            await create_collection_table(bind, existing)
        return

    auth_opts = generate_default_auth_options(is_superusers=False)
    # Add default OAuth2 mapped fields for the users collection
    auth_opts["oauth2"]["mappedFields"] = {
        "username": "name",
        "avatarURL": "avatar",
    }

    now = datetime.now(timezone.utc)
    record = CollectionRecord(
        id="_pb_users_auth_",
        name="users",
        type="auth",
        system=False,
        schema=[
            {"name": "name", "type": "text", "required": False, "options": {"max": 255}},
            {
                "name": "avatar",
                "type": "file",
                "required": False,
                "options": {
                    "mimeTypes": [
                        "image/jpeg",
                        "image/png",
                        "image/svg+xml",
                        "image/gif",
                        "image/webp",
                    ],
                    "maxSelect": 1,
                    "maxSize": 5242880,
                },
            },
        ],
        indexes=[],
        list_rule="id = @request.auth.id",
        view_rule="id = @request.auth.id",
        create_rule="",
        update_rule="id = @request.auth.id",
        delete_rule="id = @request.auth.id",
        options=auth_opts,
        created=now,
        updated=now,
    )
    session.add(record)
    await session.flush()

    if not await _table_exists(bind, "users"):
        await create_collection_table(bind, record)
