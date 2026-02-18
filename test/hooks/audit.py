"""Cross-cutting audit log hook module.

Registers create / update / delete hooks for ALL collections and writes
a structured log line to stdout.  In a real app you'd persist to an
audit_logs collection or send to a log aggregator.

Loaded by run_server.py via:
    pb.load_hooks("hooks.audit:setup")
"""

from __future__ import annotations


def setup(pb) -> None:
    """Register audit hooks on the pb facade."""
    from ppbase.ext.events import RecordRequestEvent

    # ── helpers ──────────────────────────────────────────────────────────────

    def _coll(event: RecordRequestEvent) -> str:
        coll = event.collection
        return coll.name if coll else event.collection_id_or_name

    def _actor(event: RecordRequestEvent) -> str:
        if event.is_superuser():
            return f"superuser:{event.auth_id()}"
        if event.has_record_auth():
            return f"{event.auth_collection_name()}:{event.auth_id()}"
        return "anonymous"

    # ── hooks ─────────────────────────────────────────────────────────────────

    @pb.on_record_create_request(priority=1)
    async def _audit_create(event: RecordRequestEvent):
        result = await event.next()
        rec_id = result.get("id", "?") if isinstance(result, dict) else "?"
        print(f"[audit] CREATE {_coll(event)}/{rec_id} by {_actor(event)}")
        return result

    @pb.on_record_update_request(priority=1)
    async def _audit_update(event: RecordRequestEvent):
        fields_changed = sorted(event.data.keys())
        result = await event.next()
        print(
            f"[audit] UPDATE {_coll(event)}/{event.record_id} "
            f"fields={fields_changed} by {_actor(event)}"
        )
        return result

    @pb.on_record_delete_request(priority=1)
    async def _audit_delete(event: RecordRequestEvent):
        result = await event.next()
        print(f"[audit] DELETE {_coll(event)}/{event.record_id} by {_actor(event)}")
        return result

    @pb.on_record_view_request(priority=1)
    async def _audit_view(event: RecordRequestEvent):
        result = await event.next()
        rec_id = result.get("id", "?") if isinstance(result, dict) else "?"
        print(f"[audit] VIEW   {_coll(event)}/{rec_id} by {_actor(event)}")
        return result
