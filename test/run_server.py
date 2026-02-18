#!/usr/bin/env python3
"""PPBase dev server with example routes and hooks.

Usage:
    python test/run_server.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from ppbase import pb
from ppbase.ext.events import (
    BootstrapEvent,
    ServeEvent,
    TerminateEvent,
    RecordRequestEvent,
    RecordAuthRequestEvent,
    RealtimeConnectEvent,
    RealtimeSubscribeEvent,
)

# ─────────────────────────────────────────────────────────────────────────────
# Custom routes
# ─────────────────────────────────────────────────────────────────────────────

@pb.get("/hello")
async def hello():
    """Simple health/greeting endpoint."""
    return {"message": "Hello from PPBase!", "ok": True}


@pb.get("/api/custom/status")
async def custom_status():
    """Extended status endpoint with uptime info."""
    return {
        "status": "running",
        "timestamp": time.time(),
        "version": "dev",
    }


@pb.post("/api/custom/echo")
async def echo(body: dict[str, Any]):
    """Echo back whatever JSON body you send."""
    return {"echo": body}


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle hooks
# ─────────────────────────────────────────────────────────────────────────────

@pb.on_bootstrap()
async def on_bootstrap(event: BootstrapEvent):
    """Runs once before the database is initialised."""
    print("[hook] bootstrap — settings loaded")
    await event.next()


@pb.on_serve()
async def on_serve(event: ServeEvent):
    """Runs once after the app is fully ready."""
    print("[hook] serve — server is ready ✓")
    await event.next()


@pb.on_terminate()
async def on_terminate(event: TerminateEvent):
    """Runs on graceful shutdown."""
    print("[hook] terminate — cleaning up…")
    await event.next()


# ─────────────────────────────────────────────────────────────────────────────
# Record CRUD hooks
# ─────────────────────────────────────────────────────────────────────────────

@pb.on_record_create_request("users")
async def before_user_create(event: RecordRequestEvent):
    """Intercept user creation — inject a server-side field."""
    print(f"[hook] create users — data: {event.data}")
    # Add a server-controlled field before the record is written
    event.data.setdefault("source", "api")
    result = await event.next()
    print(f"[hook] user created — id: {result.get('id') if isinstance(result, dict) else '?'}")
    return result


@pb.on_record_update_request("users")
async def before_user_update(event: RecordRequestEvent):
    """Log all user updates."""
    print(f"[hook] update users/{event.record_id} — patch: {list(event.data.keys())}")
    return await event.next()


@pb.on_record_delete_request()
async def before_any_delete(event: RecordRequestEvent):
    """Log every delete across all collections."""
    coll = event.collection.name if event.collection else event.collection_id_or_name
    print(f"[hook] delete {coll}/{event.record_id}")
    return await event.next()


@pb.on_record_view_request()
async def on_record_view(event: RecordRequestEvent):
    """Passthrough view hook — useful for audit logging."""
    result = await event.next()
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Auth hooks
# ─────────────────────────────────────────────────────────────────────────────

@pb.on_record_auth_with_password_request()
async def on_password_auth(event: RecordAuthRequestEvent):
    """Log password auth attempts (never log the password itself)."""
    coll = event.collection.name if event.collection else event.collection_id_or_name
    identity = event.body.get("identity", "?")
    print(f"[hook] auth/password — {coll} / identity={identity}")
    return await event.next()


@pb.on_record_auth_refresh_request()
async def on_auth_refresh(event: RecordAuthRequestEvent):
    """Log token refresh events."""
    coll = event.collection.name if event.collection else event.collection_id_or_name
    print(f"[hook] auth/refresh — {coll}")
    return await event.next()


# ─────────────────────────────────────────────────────────────────────────────
# Realtime hooks
# ─────────────────────────────────────────────────────────────────────────────

@pb.on_realtime_connect_request()
async def on_realtime_connect(event: RealtimeConnectEvent):
    """Log new SSE connections."""
    print(f"[hook] realtime connect — client={event.client_id}")
    return await event.next()


@pb.on_realtime_subscribe_request()
async def on_realtime_subscribe(event: RealtimeSubscribeEvent):
    """Log subscription changes."""
    print(f"[hook] realtime subscribe — client={event.client_id} topics={event.subscriptions}")
    return await event.next()


# ─────────────────────────────────────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Starting PPBase dev server…")
    print("  Admin UI : http://127.0.0.1:8090/_/")
    print("  API      : http://127.0.0.1:8090/api/")
    print("  Routes   : GET  /hello")
    print("             GET  /api/custom/status")
    print("             POST /api/custom/echo")
    print("  Ctrl+C to stop")
    pb.start(host="127.0.0.1", port=8090)
