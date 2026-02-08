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
    UI knows it cannot be deleted or renamed.  The actual data lives in the
    ``_admins`` system table — this record is purely metadata so the admin UI
    can list it.
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
