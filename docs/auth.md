# Auth Guards

PPBase supports two types of authenticated principals:

| Type | `auth["type"]` | Description |
|------|---------------|-------------|
| Admin | `"admin"` | Legacy admin token (`/api/admins/auth-with-password`) |
| Auth record | `"authRecord"` | Per-collection auth token (users, _superusers, custom) |

> In PocketBase v0.23+ admins are stored in `_superusers`. PPBase follows this convention — `is_superuser()` returns `True` for both `"admin"` tokens and `_superusers` auth records.

---

## Auth in routes

### FastAPI dependency injection

```python
from typing import Any

# Route requires no auth but reads it if present
@pb.get("/public")
async def public(auth: dict[str, Any] | None = pb.optional_auth()):
    if auth:
        return {"hello": auth["id"], "type": auth["type"]}
    return {"hello": "anonymous"}

# Route requires any valid auth (admin or record)
@pb.get("/dashboard")
async def dashboard(auth: dict[str, Any] = pb.require_auth()):
    return {"user_id": auth["id"]}

# Route requires an auth-record (not just admin)
@pb.get("/profile")
async def profile(auth: dict[str, Any] = pb.require_record_auth()):
    return {"collection": auth["collectionName"], "id": auth["id"]}

# Legacy admin token only
@pb.get("/admin/panel")
async def admin_panel(_: dict = pb.require_admin()):
    return {"admin": True}
```

`pb.require_admin()` checks only `auth["type"] == "admin"`.  
For PocketBase-like superuser access (admin token or `_superusers` auth record), use route middleware:

```python
@pb.get("/superuser/panel", middlewares=[pb.apis.require_superuser_auth()])
async def superuser_panel():
    return {"ok": True}
```

### Decoded auth payload fields

```python
{
    "id": "abc123",
    "type": "authRecord",          # or "admin"
    "collectionId": "users_coll_id",
    "collectionName": "users",
    "email": "user@example.com",
    "verified": True,
    "exp": 1700000000,
}
```

---

## Auth in hooks

All hook events expose the same auth helpers. Load auth first if it may not have been set:

```python
from ppbase.ext.events import RouteRequestEvent

@pb.middleware(priority=100)
async def auth_guard(event: RouteRequestEvent):
    await event.load_auth()   # resolve Bearer token

    # check type
    print(event.auth_type())         # "authRecord" | "admin" | None
    print(event.auth_id())           # record ID
    print(event.auth_collection_name())  # "users" | "_superusers" | …

    # boolean helpers
    event.has_auth()          # any auth
    event.has_record_auth()   # auth-record specifically
    event.is_superuser()      # admin or _superusers member
    event.is_admin()          # legacy admin token

    return await event.next()
```

### Guard helpers (raise HTTPException automatically)

```python
from ppbase.ext.events import RecordRequestEvent

@pb.on_record_delete_request("posts")
async def guard(event: RecordRequestEvent):
    auth = event.require_auth()         # raises 401 if no auth
    event.require_superuser()           # raises 403 if not superuser

    # Only the record's owner can delete it
    post = await event.records("posts").get(event.record_id, fields="id,author_id")
    if post and post.get("author_id") != event.auth_id():
        event.require_superuser()       # let superusers bypass

    return await event.next()
```

### Checking the requesting user is the same as the target record

```python
@pb.on_record_update_request("users")
async def self_only(event: RecordRequestEvent):
    """Users can only update their own profile."""
    if not event.is_superuser():
        event.require_same_auth_record(event.record_id)  # raises 403 if not same
    return await event.next()
```

---

## Auth collection hooks

Hook by specific collection names to apply logic only to a particular auth collection:

```python
# Only fires for /api/collections/users/auth-with-password
@pb.on_record_auth_with_password_request("users")
async def users_login(event):
    identity = event.body.get("identity", "")
    print(f"Users login: {identity}")
    return await event.next()

# Only fires for _superusers login
@pb.on_record_auth_with_password_request("_superusers")
async def su_login(event):
    print("Superuser login attempt!")
    return await event.next()

# Fires for ALL auth collections
@pb.on_record_auth_with_password_request()
async def all_logins(event):
    coll = event.collection.name if event.collection else "?"
    print(f"Login to {coll}")
    return await event.next()
```

---

## Per-collection token secrets

Each auth collection has its own signing secret stored in `_collections.options`. Tokens issued for `users` cannot be used on `_superusers` endpoints, and vice versa.

This is handled automatically by PPBase — you don't need to manage secrets manually. But if you need to read the active auth collection from a token:

```python
@pb.on_record_auth_refresh_request()
async def refresh_guard(event):
    # event.collection is the auth collection the token was issued for
    coll_name = event.collection.name if event.collection else None
    if coll_name == "_superusers":
        print("Superuser token refresh")
    return await event.next()
```

---

## Fetching the current user record

```python
@pb.get("/me")
async def me(auth: dict | None = pb.optional_auth()):
    if not auth or auth.get("type") != "authRecord":
        return {"user": None}

    user = await pb.records(auth["collectionName"]).get(
        auth["id"],
        fields="id,email,name,avatar",
    )
    return {"user": user}
```

Or from inside a hook:

```python
@pb.on_record_create_request("comments")
async def attach_author(event):
    user = await event.get_current_user(fields="id,name")
    if user:
        event.data["author_name"] = user["name"]
    return await event.next()
```
