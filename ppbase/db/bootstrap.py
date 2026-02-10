"""Bootstrap system collections in the _collections table.

PocketBase v0.23+ exposes admins as a special ``_superusers`` auth collection.
This module ensures that collection record exists at startup so the admin UI
can display it alongside user-created collections.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.core.id_generator import generate_id
from ppbase.db.system_tables import CollectionRecord


async def ensure_superusers_collection(session: AsyncSession) -> None:
    """Create the ``_superusers`` system collection if it doesn't exist yet.

    The collection is of type ``auth`` and marked ``system=True`` so that the
    UI knows it cannot be deleted or renamed. The actual superuser data lives
    in the ``_superusers`` table (created by SQLAlchemy).
    """
    stmt = select(CollectionRecord).where(CollectionRecord.name == "_superusers")
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
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
        options={},
        created=now,
        updated=now,
    )
    session.add(record)
    await session.flush()


async def ensure_external_auths_collection(session: AsyncSession) -> None:
    """Create the ``_external_auths`` system collection if it doesn't exist yet.

    The collection stores OAuth2 provider links for auth collection records.
    This is a system table marked ``system=True`` so the UI displays it in
    the system collections section.
    """
    stmt = select(CollectionRecord).where(CollectionRecord.name == "_external_auths")
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        return

    now = datetime.now(timezone.utc)
    record = CollectionRecord(
        id=generate_id(),
        name="_external_auths",
        type="base",
        system=True,
        schema=[
            {"name": "collection_id", "type": "text", "required": True, "options": {}},
            {"name": "record_id", "type": "text", "required": True, "options": {}},
            {"name": "provider", "type": "text", "required": True, "options": {}},
            {"name": "provider_id", "type": "text", "required": True, "options": {}},
        ],
        indexes=[],
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
