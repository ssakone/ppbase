# Realtime / SSE

PPBase implements real-time updates using **Server-Sent Events (SSE)** with PostgreSQL `LISTEN/NOTIFY` for broadcasting.

## How it works

```
Client → GET /api/realtime        ← receives clientId in PB_CONNECT event
Client → POST /api/realtime       ← subscribe to topics
DB     → NOTIFY record_changes    ← triggered by create/update/delete
Server → SSE event                ← forwarded to subscribed clients
```

## Subscription topics

| Format | Example | Meaning |
|--------|---------|---------|
| `collectionName/*` | `posts/*` | All events in the `posts` collection |
| `collectionName/recordId` | `posts/abc123` | Events for a specific record |

## Subscription semantics

- `POST /api/realtime` **replaces** the full subscription set for the client (not incremental add).
- To clear all subscriptions, send an empty list: `{"clientId":"...", "subscriptions":[]}`.
- Realtime auth is session-consistent: if a later subscribe request uses a different auth identity than the first one, PPBase returns `403`.

## Topic options (`options`)

Each topic can carry optional query/header context via URL query string:

```text
posts/*?options={"query":{"expand":"author","fields":"id,title,author"},"headers":{"x-role":"staff"}}
```

In raw HTTP clients, URL-encode the JSON value of `options`.

These options are parsed server-side and applied per subscription during event filtering/enrichment.

## Client SDK (JavaScript)

```javascript
import PocketBase from "pocketbase";   // or use raw EventSource

const pb = new PocketBase("http://127.0.0.1:8090");

// Subscribe to all posts
await pb.collection("posts").subscribe("*", (e) => {
    console.log(e.action, e.record);
});

// Subscribe to a specific post
await pb.collection("posts").subscribe("record_id_123", (e) => {
    console.log("Post updated:", e.record);
});

// Unsubscribe
await pb.collection("posts").unsubscribe("*");
```

## Raw SSE (without SDK)

```javascript
const es = new EventSource("http://127.0.0.1:8090/api/realtime");

es.addEventListener("PB_CONNECT", (e) => {
    const { clientId } = JSON.parse(e.data);

    // subscribe
    fetch("http://127.0.0.1:8090/api/realtime", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            clientId,
            subscriptions: ["posts/*"],
        }),
    });
});

es.addEventListener("posts/*", (e) => {
    const { action, record } = JSON.parse(e.data);
    console.log(action, record);
});
```

---

## Realtime hooks

### `on_realtime_connect_request`

Fires when a client establishes the SSE connection. Useful for auth-gating realtime access:

```python
from ppbase.ext.events import RealtimeConnectEvent

@pb.on_realtime_connect_request()
async def guard_realtime(event: RealtimeConnectEvent):
    """Only authenticated users may use realtime."""
    if event.auth is None:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=401,
            detail={"status": 401, "message": "Login required for realtime.", "data": {}},
        )
    print(f"Realtime connect: client={event.client_id} user={event.auth.get('id')}")
    return await event.next()
```

### `on_realtime_subscribe_request`

Fires when a client calls `POST /api/realtime` to update their subscriptions. You can inspect or filter the requested topics:

```python
from ppbase.ext.events import RealtimeSubscribeEvent

@pb.on_realtime_subscribe_request()
async def filter_subscriptions(event: RealtimeSubscribeEvent):
    """Remove subscriptions to restricted collections for non-admins."""
    RESTRICTED = {"audit_logs", "_superusers"}

    if not event.is_superuser():
        event.subscriptions = [
            s for s in event.subscriptions
            if not any(s.startswith(r) for r in RESTRICTED)
        ]

    print(f"Subscribe: client={event.client_id} topics={event.subscriptions}")
    return await event.next()
```

### `on_realtime_message_send`

Fires for each SSE event before it is sent to the client. Useful for data enrichment or filtering:

```python
from ppbase.ext.events import RealtimeMessageSendEvent

@pb.on_realtime_message_send()
async def enrich_realtime(event: RealtimeMessageSendEvent):
    """Add computed fields to realtime payloads."""
    record = event.data.get("record", {})
    if record:
        record["_realtime"] = True   # tag realtime records
    return await event.next()


@pb.on_realtime_message_send()
async def filter_sensitive(event: RealtimeMessageSendEvent):
    """Strip sensitive fields from realtime payloads."""
    record = event.data.get("record", {})
    for field in ("password_hash", "token_key", "email"):
        record.pop(field, None)
    return await event.next()
```

---

## Triggering custom realtime events

PPBase fires `NOTIFY record_changes` automatically on create/update/delete. For custom events, send a NOTIFY directly:

```python
import json
from ppbase.db.engine import get_engine

async def notify_custom(collection: str, record_id: str, action: str):
    payload = json.dumps({
        "collection": collection,
        "record_id": record_id,
        "action": action,
    })
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(
            __import__("sqlalchemy").text(f"NOTIFY record_changes, :payload"),
            {"payload": payload},
        )
        await conn.commit()
```

Call from a route or hook:

```python
@pb.post("/posts/{post_id}/publish")
async def publish_post(post_id: str):
    await pb.records("posts").update(post_id, {"status": "published"})
    await notify_custom("posts", post_id, "update")
    return {"published": True}
```

---

## RealtimeConnectEvent reference

| Attribute | Type | Description |
|-----------|------|-------------|
| `event.client_id` | `str \| None` | Auto-generated client UUID |
| `event.auth` | `dict \| None` | Decoded JWT if Authorization header present |
| `event.subscription_manager` | `SubscriptionManager` | In-memory session store |

## RealtimeSubscribeEvent reference

| Attribute | Type | Description |
|-----------|------|-------------|
| `event.client_id` | `str` | Client UUID |
| `event.subscriptions` | `list[str]` | Topic strings — **mutable** |
| `event.parsed_subscriptions` | `list[RealtimeSubscription]` | Parsed objects |
| `event.authorization` | `str \| None` | Raw Authorization header from subscribe request |
| `event.auth` | `dict \| None` | Decoded auth from `authorization` |

## RealtimeMessageSendEvent reference

| Attribute | Type | Description |
|-----------|------|-------------|
| `event.client_id` | `str` | Target client UUID |
| `event.topic` | `str` | Subscription topic (e.g. `posts/*`) |
| `event.data` | `dict` | SSE data (`{"action": "...", "record": {...}}`) — **mutable** |
| `event.subscription` | `RealtimeSubscription` | Parsed subscription object |
