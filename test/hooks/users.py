"""User collection hook module.

Demonstrates:
  - Field injection on create
  - Ownership guard on update
  - Cascade cleanup on delete
  - Login / refresh logging

Loaded via:
    pb.load_hooks("hooks.users:setup")
"""

from __future__ import annotations

import datetime


def setup(pb) -> None:
    from ppbase.ext.events import RecordAuthRequestEvent, RecordRequestEvent

    # ── CREATE ────────────────────────────────────────────────────────────────

    @pb.on_record_create_request("users", priority=50)
    async def user_before_create(event: RecordRequestEvent):
        """Inject server-side defaults before writing to DB."""
        event.data.setdefault("role", "member")
        event.data.setdefault("active", True)
        event.data.setdefault(
            "joined_at",
            datetime.datetime.utcnow().isoformat() + "Z",
        )
        result = await event.next()
        print(f"[users] created user {result.get('email')} ({result.get('id')})")
        return result

    # ── UPDATE ────────────────────────────────────────────────────────────────

    @pb.on_record_update_request("users", priority=50)
    async def user_before_update(event: RecordRequestEvent):
        """Prevent regular users from promoting their own role."""
        if "role" in event.data and not event.is_superuser():
            # strip the role field — only superusers may change it
            del event.data["role"]
        return await event.next()

    # ── DELETE ────────────────────────────────────────────────────────────────

    @pb.on_record_delete_request("users", priority=50)
    async def user_before_delete(event: RecordRequestEvent):
        """Log before deletion. Could also cascade-delete related records."""
        print(f"[users] deleting user {event.record_id}")
        return await event.next()

    # ── AUTH ──────────────────────────────────────────────────────────────────

    @pb.on_record_auth_with_password_request("users")
    async def user_login(event: RecordAuthRequestEvent):
        identity = event.body.get("identity", "?")
        print(f"[users] login — identity={identity}")
        result = await event.next()
        if result and isinstance(result, dict):
            rec = result.get("record", {})
            print(f"[users] login OK — id={rec.get('id')} email={rec.get('email')}")
        return result

    @pb.on_record_auth_refresh_request("users")
    async def user_token_refresh(event: RecordAuthRequestEvent):
        print("[users] token refresh")
        return await event.next()
