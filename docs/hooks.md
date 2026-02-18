# Hooks

Hooks let you intercept PPBase lifecycle events and record CRUD/auth operations. Every hook handler receives an `event` object and **must** call `await event.next()` to continue the chain (or return a custom response to short-circuit).

## Hook chain pattern

```
request → hook_1 → hook_2 → hook_3 → built-in handler
                                           ↓
response ←←←←←←←←←←←←←←←←←←←←←←←← result
```

Each handler wraps the call:

```python
@pb.on_record_create_request("posts")
async def my_hook(event):
    # runs BEFORE the built-in create
    event.data["author"] = "system"

    result = await event.next()   # ← calls the next handler in chain

    # runs AFTER create (record request hooks usually return JSONResponse)
    print(f"create status={getattr(result, 'status_code', 'n/a')}")
    return result
```

For record request hooks (`on_record_*_request`), `event.next()` usually returns a FastAPI `Response` object (e.g. `JSONResponse`).  
For auth hooks (`on_record_auth_*`), `event.next()` returns a PocketBase-style payload dict (`{"token": ..., "record": ..., ...}`).

To **block** the request, raise `HTTPException` or return a custom `JSONResponse` without calling `event.next()`:

```python
from fastapi import HTTPException

@pb.on_record_delete_request("posts")
async def guard_delete(event):
    if not event.has_auth():
        raise HTTPException(
            status_code=401,
            detail={"status": 401, "message": "Login required.", "data": {}},
        )
    return await event.next()
```

---

## Lifecycle hooks

### `on_bootstrap`
Runs once at startup, before the database is initialised.

```python
from ppbase.ext.events import BootstrapEvent

@pb.on_bootstrap()
async def on_start(event: BootstrapEvent):
    print(f"Starting with settings: {event.settings}")
    await event.next()
```

### `on_serve`
Runs once after all tables are created and the HTTP server is ready.

```python
from ppbase.ext.events import ServeEvent

@pb.on_serve()
async def on_ready(event: ServeEvent):
    print("Server ready!")
    await event.next()
```

### `on_terminate`
Runs on SIGINT / graceful shutdown.

```python
from ppbase.ext.events import TerminateEvent

@pb.on_terminate()
async def on_shutdown(event: TerminateEvent):
    print("Shutting down, flushing buffers…")
    await event.next()
```

---

## Record CRUD hooks

All record hooks receive a `RecordRequestEvent`. Optionally filter by collection name(s).

### `on_records_list_request`

```python
from ppbase.ext.events import RecordRequestEvent

# All collections
@pb.on_records_list_request()
async def log_lists(event: RecordRequestEvent):
    coll = event.collection.name if event.collection else event.collection_id_or_name
    print(f"LIST {coll} page={event.page} filter={event.filter!r}")
    return await event.next()

# Specific collection — filter query injection
@pb.on_records_list_request("posts")
async def only_published(event: RecordRequestEvent):
    """Only show published posts to non-admins."""
    if not event.is_superuser():
        current = event.filter or ""
        event.filter = f'status="published"' if not current else f'({current}) && status="published"'
    return await event.next()
```

### `on_record_view_request`

```python
@pb.on_record_view_request("products")
async def track_view(event: RecordRequestEvent):
    result = await event.next()
    if getattr(result, "status_code", 500) == 200:
        # fire-and-forget side effect on successful view
        import asyncio
        asyncio.create_task(event.records("metrics").create({"event": "product_view"}))
    return result
```

### `on_record_create_request`

```python
@pb.on_record_create_request("posts")
async def before_post_create(event: RecordRequestEvent):
    # inject server-side fields
    event.data.setdefault("status", "draft")
    if event.has_record_auth():
        event.data["author_id"] = event.auth_id()

    result = await event.next()
    print(f"create status={getattr(result, 'status_code', 'n/a')}")
    return result
```

### `on_record_update_request`

```python
@pb.on_record_update_request("posts")
async def before_post_update(event: RecordRequestEvent):
    # only allow the author to update their own post
    if event.has_record_auth():
        current = await event.get(
            record_id=event.record_id,
            collection="posts",
            fields="id,author_id",
        )
        if current and current.get("author_id") != event.auth_id() and not event.is_superuser():
            raise HTTPException(
                status_code=403,
                detail={"status": 403, "message": "Not allowed.", "data": {}},
            )
    return await event.next()
```

### `on_record_delete_request`

```python
@pb.on_record_delete_request()
async def audit_deletes(event: RecordRequestEvent):
    coll = event.collection.name if event.collection else event.collection_id_or_name
    actor = event.auth_id() or "anonymous"
    print(f"[audit] DELETE {coll}/{event.record_id} by {actor}")
    return await event.next()
```

---

## Auth hooks

### `on_record_auth_with_password_request`

```python
from ppbase.ext.events import RecordAuthRequestEvent

@pb.on_record_auth_with_password_request("users")
async def on_login(event: RecordAuthRequestEvent):
    identity = event.body.get("identity", "?")
    print(f"Login attempt: {identity}")
    result = await event.next()
    if result:
        print(f"Login success: {result.get('record', {}).get('id')}")
    return result
```

### `on_record_auth_refresh_request`

```python
@pb.on_record_auth_refresh_request()
async def on_refresh(event: RecordAuthRequestEvent):
    coll = event.collection.name if event.collection else "?"
    print(f"Token refresh: {coll}")
    return await event.next()
```

### `on_record_auth_with_oauth2_request`

```python
@pb.on_record_auth_with_oauth2_request("users")
async def on_oauth2(event: RecordAuthRequestEvent):
    provider = event.body.get("provider", "?")
    print(f"OAuth2 login via {provider}")
    return await event.next()
```

### `on_record_request_otp_request`

```python
@pb.on_record_request_otp_request("users")
async def on_request_otp(event: RecordAuthRequestEvent):
    identity = event.body.get("email") or event.body.get("identity")
    print(f"OTP request: {identity}")
    return await event.next()
```

### `on_record_auth_with_otp_request`

```python
@pb.on_record_auth_with_otp_request("users")
async def on_auth_otp(event: RecordAuthRequestEvent):
    print(f"OTP auth attempt: otpId={event.body.get('otpId')}")
    return await event.next()
```

### `on_record_auth_request` (generic success hook)

Runs after successful auth flows (`password`, `oauth2`, `otp`, `refresh`) and lets you adjust the final auth payload:

```python
@pb.on_record_auth_request("users")
async def auth_success(event: RecordAuthRequestEvent):
    result = await event.next()
    if isinstance(result, dict):
        meta = dict(result.get("meta") or {})
        meta["source"] = "hook"
        result["meta"] = meta
    return result
```

---

## File hooks

### `on_file_token_request`

```python
from ppbase.ext.events import FileTokenRequestEvent

@pb.on_file_token_request()
async def log_file_token(event: FileTokenRequestEvent):
    actor = event.auth_id() or "anonymous"
    print(f"file token request by {actor}")
    return await event.next()
```

### `on_file_download_request`

```python
from ppbase.ext.events import FileDownloadRequestEvent

@pb.on_file_download_request("users")
async def guard_download(event: FileDownloadRequestEvent):
    if not event.has_auth():
        event.require_auth()  # raises PB-style 401
    return await event.next()
```

---

## Hook priority

When multiple handlers are registered for the same hook, `priority` controls execution order. **Higher priority runs first.**

```python
@pb.on_record_create_request("posts", priority=100)
async def first(event):
    print("I run first")
    return await event.next()

@pb.on_record_create_request("posts", priority=10)
async def second(event):
    print("I run second")
    return await event.next()
```

---

## Hook IDs (deduplication)

Use `id=` to name a hook so it can only be registered once (useful when reloading modules):

```python
@pb.on_record_create_request("users", id="user-create-audit")
async def audit(event):
    ...
    return await event.next()
```

---

## `RecordRequestEvent` reference

| Attribute / Method | Type | Description |
|---|---|---|
| `event.collection` | `CollectionRecord \| None` | Resolved collection ORM record |
| `event.collection_id_or_name` | `str` | Raw collection identifier from the request |
| `event.record_id` | `str \| None` | Target record ID (view/update/delete) |
| `event.auth` | `dict \| None` | Decoded JWT payload |
| `event.data` | `dict` | Request body (create/update) — mutable |
| `event.files` | `dict[str, list[tuple[str, bytes]]]` | Multipart uploaded files (create/update) — mutable |
| `event.page` | `int \| None` | Pagination (list) |
| `event.per_page` | `int \| None` | Page size (list) |
| `event.sort` | `str \| None` | Sort expression (list) — mutable |
| `event.filter` | `str \| None` | Filter expression (list) — mutable |
| `event.expand` | `str \| None` | Expand expression — mutable |
| `event.fields` | `str \| None` | Field selector — mutable |
| `event.skip_total` | `bool \| None` | Skip total count (list) — mutable |
| `event.engine` | `AsyncEngine` | SQLAlchemy engine |
| `event.has_auth()` | `bool` | Any auth present |
| `event.has_record_auth()` | `bool` | Auth is an auth-record token |
| `event.is_superuser()` | `bool` | Auth is admin or _superusers record |
| `event.is_admin()` | `bool` | Legacy admin token (`type="admin"`) |
| `event.auth_type()` | `str \| None` | Auth type (`admin`, `authRecord`, or `None`) |
| `event.auth_id()` | `str \| None` | ID from auth token |
| `event.auth_collection_name()` | `str \| None` | Collection from auth token |
| `event.require_auth()` | `dict` | Raises 401 if no auth |
| `event.require_auth_record()` | `dict` | Raises 401/403 unless auth-record |
| `event.require_superuser()` | `dict` | Raises 403 if not superuser |
| `event.require_same_auth_record(record_id, collection=None)` | `dict` | Raises 403 unless token belongs to that record |
| `await event.get_current_user()` | `dict \| None` | Fetch current auth record from DB |
| `event.records(coll)` | `RecordRepository` | CRUD helpers for a collection |
| `await event.get(record_id=None, collection=None, fields=None)` | `dict \| None` | Get one record |
| `await event.list(...)` | `dict` | List records (paginated) |
| `await event.create(data, collection=None, files=None)` | `dict` | Create record |
| `await event.update(data, record_id=None, collection=None, files=None)` | `dict \| None` | Update record |
| `await event.delete(record_id=None, collection=None)` | `bool` | Delete record |
| `await event.next()` | `Any` | Continue the hook chain |
