# Middleware

Extension middleware intercepts requests to **custom routes** (routes registered via `pb.get()`, `pb.group()`, etc.). It does **not** intercept built-in PocketBase API calls — use record hooks for those.

## Three levels of middleware

| Level | Registration | Scope |
|-------|-------------|-------|
| Global | `@pb.middleware(priority=N)` | All extension routes |
| Group | `@group.middleware(priority=N)` | Routes in that group |
| Route | `@pb.get("/path", middlewares=[fn])` | That route only |

Middleware handlers receive a `RouteRequestEvent` and **must** call `await event.next()` to pass control to the next handler.

---

## Global middleware

Runs for every request on custom extension routes:

```python
import time
from ppbase.ext.events import RouteRequestEvent

@pb.middleware(priority=100)
async def request_timer(event: RouteRequestEvent):
    start = time.perf_counter()
    result = await event.next()
    ms = (time.perf_counter() - start) * 1000
    print(f"[timer] {event.method} {event.path} → {ms:.1f} ms")
    return result
```

## Path & method filtering

Restrict a global middleware to specific paths or HTTP methods — it won't run for anything that doesn't match:

```python
# Only runs for GET /api/custom/*
@pb.middleware(priority=120, path="/api/custom/*", methods=["GET"])
async def custom_get_probe(event: RouteRequestEvent):
    print(f"[probe] {event.method} {event.path}")
    return await event.next()

# Multiple paths
@pb.middleware(priority=90, paths=["/api/v1/*", "/api/v2/*"], methods=["POST", "PUT", "PATCH"])
async def mutation_guard(event: RouteRequestEvent):
    await event.load_auth()
    if not event.has_auth():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"status": 401, "message": "Login required.", "data": {}})
    return await event.next()

# Custom predicate function
@pb.middleware(priority=80, predicate=lambda e: e.path.startswith("/api/") and "internal" in e.path)
async def internal_guard(event: RouteRequestEvent):
    return await event.next()
```

Glob patterns: `*` matches a single segment, `**` matches any number of segments (e.g. `/api/**`).

## Group middleware

Applies only to routes registered in a given `RouteGroup`:

```python
from fastapi.responses import JSONResponse

api = pb.group("/api/v1")

@api.middleware(priority=50)
async def require_api_key(event: RouteRequestEvent):
    key = event.headers.get("x-api-key", "")
    if key != "my-secret-key":
        return JSONResponse(
            status_code=401,
            content={"status": 401, "message": "Invalid API key.", "data": {}},
        )
    return await event.next()

@api.get("/protected")
async def protected():
    return {"secret": "data"}
```

## Route-level middleware

Pass `middlewares=[...]` to a route decorator. These run **after** group middleware:

```python
async def check_feature_flag(event: RouteRequestEvent):
    if not BETA_ENABLED:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"status": 404, "message": "Not found.", "data": {}})
    return await event.next()

@pb.get("/beta/feature", middlewares=[check_feature_flag])
async def beta_feature():
    return {"beta": True}
```

## Built-in auth middlewares (`pb.apis`)

`pb.apis` provides ready-made route-level middlewares that mirror PocketBase's auth guards. Pass them in `middlewares=[...]`:

```python
# Require any authenticated user
@api.get("/private", middlewares=[pb.apis.require_auth()])
async def private(auth = pb.optional_auth()):
    return {"id": auth["id"] if auth else None}

# Require auth from a specific collection only
@api.get("/users-only", middlewares=[pb.apis.require_auth("users")])
async def users_only():
    return {"ok": True}

# Require superuser / admin
@api.get("/admin", middlewares=[pb.apis.require_superuser_auth()])
async def admin_panel():
    return {"admin": True}

# Superuser OR the record's owner (path param must be the user's ID)
@api.get("/users/{id}/settings", middlewares=[pb.apis.require_superuser_or_owner_auth("id")])
async def user_settings(id: str):
    return {"userId": id}

# Guest-only (reject authenticated requests)
@api.post("/register", middlewares=[pb.apis.require_guest_only()])
async def register(body: dict):
    return {"registered": True}
```

| Helper | Raises | Description |
|--------|--------|-------------|
| `pb.apis.require_auth(*collections)` | 401 / 403 | Any auth; optionally restrict to collection name(s) |
| `pb.apis.require_superuser_auth()` | 401 / 403 | Admin or `_superusers` member |
| `pb.apis.require_superuser_or_owner_auth(owner_id_param)` | 401 / 403 | Superuser or the record owner (path param) |
| `pb.apis.require_guest_only()` | 403 | Reject requests with any auth |

---

## Request-local store

Middleware can pass data to downstream handlers and route functions via a per-request key-value store. This avoids re-computing expensive operations (e.g. loading auth once, using it everywhere).

### Writing from middleware

```python
import time
from ppbase.ext.events import RouteRequestEvent

@pb.middleware(priority=110, path="/trace")
async def trace_context(event: RouteRequestEvent):
    event.set("traceId", f"trace-{int(time.time() * 1000)}")
    event.set("startedAt", time.perf_counter())

    result = await event.next()

    elapsed = (time.perf_counter() - float(event.get("startedAt", 0))) * 1000
    print(f"[trace] id={event.get('traceId')} elapsed={elapsed:.1f} ms")
    return result
```

### Reading from route handler (`pb.request_store()`)

```python
@pb.get("/trace")
async def trace_demo(store: dict = pb.request_store()):
    return {
        "traceId": store.get("traceId"),
        "hasTrace": "traceId" in store,
    }
```

### Store API on `RouteRequestEvent`

| Method | Description |
|--------|-------------|
| `event.set(key, value)` | Write a value to the store; returns `self` (chainable) |
| `event.get(key, default=None)` | Read a value (returns `default` if missing) |
| `event.has(key)` | Check if key is present |
| `event.remove(key)` | Remove a key |

The store is tied to `request.state` so it is shared across all middlewares and the handler within the same HTTP request.

---

## Unbinding middleware

Sometimes a child group or specific route needs to opt out of a middleware registered at a higher level. Use `unbind=` or `group.unbind()`.

### Unbind from a specific route

```python
@pb.middleware(priority=100, id="global-auth")
async def global_auth(event: RouteRequestEvent):
    await event.load_auth()
    if not event.has_auth():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"status": 401, "message": "Login required.", "data": {}})
    return await event.next()

# This route runs without global-auth
@pb.get("/public/ping", unbind=["global-auth"])
async def ping():
    return {"pong": True}
```

### Unbind from a group

```python
public = pb.group("/public").unbind("global-auth")

@public.get("/status")    # skips global-auth
async def status():
    return {"ok": True}
```

### Remove a global middleware entirely

```python
pb.unbind_middleware("global-auth")   # removes from registry
```

---

## Auth inside middleware

`event.auth` is `None` by default. Call `await event.load_auth()` to resolve the Bearer token:

```python
@pb.middleware(priority=90)
async def auth_aware(event: RouteRequestEvent):
    await event.load_auth()

    if event.is_superuser():
        print("Superuser request")
    elif event.has_record_auth():
        print(f"User: {event.auth_id()} ({event.auth_collection_name()})")
    else:
        print("Anonymous request")

    return await event.next()
```

## Programmatic registration (`pb.use`)

Equivalent to `@pb.middleware(...)` without the decorator syntax:

```python
async def cors_headers(event: RouteRequestEvent):
    return await event.next()

pb.use(cors_headers, priority=200, path="/api/*")
```

---

## Built-in API rate limiting

PPBase also ships a core middleware (`RateLimitMiddleware`) for built-in API endpoints (`/api/**`).

- Controlled from `settings.rateLimiting` (or legacy `settings.rateLimits`).
- Skips `/api/health` and `/api/realtime`.
- Returns PocketBase-style `429` on limit breach.

Response headers:

- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`
- `Retry-After` (only when blocked)

---

## `RouteRequestEvent` reference

| Property / Method | Type | Description |
|---|---|---|
| `event.request` | `Request` | Raw Starlette request |
| `event.path` | `str` | URL path (`/api/custom/foo`) |
| `event.method` | `str` | HTTP method (`GET`, `POST`, …) |
| `event.headers` | `dict[str, str]` | Request headers |
| `event.query_params` | `dict[str, str]` | URL query parameters |
| `event.path_params` | `dict[str, Any]` | Path parameters |
| `event.auth` | `dict \| None` | Decoded auth payload (after `load_auth()`) |
| `await event.load_auth()` | `dict \| None` | Resolve Bearer token once, cache it |
| `event.has_auth()` | `bool` | Auth is loaded and not None |
| `event.is_superuser()` | `bool` | Auth is admin or `_superusers` |
| `event.auth_id()` | `str \| None` | Record ID from auth token |
| `event.set(key, value)` | `self` | Write to request-local store |
| `event.get(key, default)` | `Any` | Read from request-local store |
| `event.has(key)` | `bool` | Check key in store |
| `event.remove(key)` | — | Remove key from store |
| `await event.next()` | `Any` | Continue the middleware chain |

## Execution order example

Given:

```python
@pb.middleware(priority=200, id="timer")       # A — global
@pb.middleware(priority=120, path="/api/v1/*") # B — global, path-filtered
@api.middleware(priority=50)                   # C — group
route = @api.get("/x", middlewares=[D])        # D — route-level
```

Order for `GET /api/v1/x`:

```
A (global priority=200)
  → B (global path-filtered, priority=120, matches /api/v1/x)
    → C (group priority=50)
      → D (route-level)
        → route handler
      ← D
    ← C
  ← B
← A
```

For `GET /api/v1/x` with `unbind=["timer"]` on the route, A is skipped entirely.
