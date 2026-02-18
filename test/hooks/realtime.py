"""Realtime / SSE hook module.

Demonstrates:
  - Auth-gating the SSE connection
  - Filtering subscription topics
  - Enriching outgoing SSE payloads

Loaded via:
    pb.load_hooks("hooks.realtime:setup")
"""

from __future__ import annotations

# Collections regular users are NOT allowed to subscribe to
_RESTRICTED_COLLECTIONS = {"_superusers", "_admins", "audit_logs"}


def setup(pb) -> None:
    from ppbase.ext.events import (
        RealtimeConnectEvent,
        RealtimeMessageSendEvent,
        RealtimeSubscribeEvent,
    )

    @pb.on_realtime_connect_request()
    async def on_connect(event: RealtimeConnectEvent):
        """Log every new SSE connection."""
        user_id = event.auth.get("id") if event.auth else "anonymous"
        print(f"[realtime] CONNECT client={event.client_id} user={user_id}")
        return await event.next()

    @pb.on_realtime_subscribe_request()
    async def on_subscribe(event: RealtimeSubscribeEvent):
        """Remove restricted topics for non-superusers."""
        if not event.is_superuser():
            original = event.subscriptions[:]
            event.subscriptions = [
                s
                for s in event.subscriptions
                if not any(s.startswith(r) for r in _RESTRICTED_COLLECTIONS)
            ]
            removed = set(original) - set(event.subscriptions)
            if removed:
                print(f"[realtime] removed restricted topics {removed} for client={event.client_id}")

        print(f"[realtime] SUBSCRIBE client={event.client_id} topics={event.subscriptions}")
        return await event.next()

    @pb.on_realtime_message_send()
    async def on_message(event: RealtimeMessageSendEvent):
        """Tag every outgoing realtime record with _rt=True."""
        record = event.data.get("record")
        if isinstance(record, dict):
            record["_rt"] = True
        return await event.next()
