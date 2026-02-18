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

Pass `middlewares=[...]` to a route decorator. These run **after** group middleware and **before** the route handler:

```python
async def require_superuser(event: RouteRequestEvent):
    await event.load_auth()
    if not event.is_superuser():
        return JSONResponse(
            status_code=403,
            content={"status": 403, "message": "Superuser required.", "data": {}},
        )
    return await event.next()

@pb.get("/admin/dashboard", middlewares=[require_superuser])
async def admin_dashboard():
    return {"dashboard": "ok"}
```

## Auth inside middleware

`event.auth` is `None` by default. Call `await event.load_auth()` to resolve the Bearer token:

```python
@pb.middleware(priority=90)
async def auth_aware(event: RouteRequestEvent):
    await event.load_auth()

    if event.has_record_auth():
        user_id = event.auth_id()
        print(f"Authenticated user: {user_id}")
    elif event.is_superuser():
        print("Superuser request")
    else:
        print("Anonymous request")

    return await event.next()
```

## Programmatic registration (`pb.use`)

Equivalent to `@pb.middleware(...)` without the decorator syntax:

```python
async def cors_headers(event: RouteRequestEvent):
    result = await event.next()
    return result  # add headers via a response wrapper if needed

pb.use(cors_headers, priority=200)
```

## RouteRequestEvent reference

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
| `event.is_superuser()` | `bool` | Auth is admin or _superusers |
| `event.auth_id()` | `str \| None` | Record ID from auth token |
| `await event.next()` | `Any` | Continue the middleware chain |

## Execution order example

Given:

```python
@pb.middleware(priority=100)   # A — global, highest priority
@api.middleware(priority=50)   # B — group
route = @api.get("/x", middlewares=[C])  # C — route-level
```

Order for `GET /api/v1/x`:

```
A (global, priority=100)
  → B (group, priority=50)
    → C (route-level)
      → route handler
    ← C
  ← B
← A
```

## Short-circuiting

Return a response directly (without calling `event.next()`) to stop the chain:

```python
@pb.middleware(priority=200)
async def block_bots(event: RouteRequestEvent):
    ua = event.headers.get("user-agent", "").lower()
    if "scraperbot" in ua:
        return JSONResponse(
            status_code=429,
            content={"status": 429, "message": "Too many requests.", "data": {}},
        )
    return await event.next()
```
