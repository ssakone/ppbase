# Custom Routes

PPBase lets you add arbitrary HTTP routes that sit alongside the built-in PocketBase API. Routes are registered on the `pb` facade and mounted into FastAPI at startup.

## Basic routes

```python
from ppbase import pb

@pb.get("/ping")
async def ping():
    return {"pong": True}

@pb.post("/echo")
async def echo(body: dict):
    return {"echo": body}

@pb.put("/items/{item_id}")
async def replace_item(item_id: str, body: dict):
    return {"id": item_id, "data": body}

@pb.patch("/items/{item_id}")
async def patch_item(item_id: str, body: dict):
    return {"id": item_id, "patch": body}

@pb.delete("/items/{item_id}")
async def delete_item(item_id: str):
    from fastapi.responses import Response
    return Response(status_code=204)
```

All decorators accept the same kwargs as FastAPI's `@router.get(...)` — response models, tags, dependencies, etc.

## Path parameters & query parameters

```python
from fastapi import Query

@pb.get("/posts/{slug}")
async def get_post(slug: str, lang: str = Query("en")):
    return {"slug": slug, "lang": lang}
```

## Request object

Use FastAPI's `Request` directly when you need headers, body streaming, etc.:

```python
from fastapi import Request

@pb.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")
    # verify signature…
    return {"received": True}
```

## Route groups

Group related routes under a shared prefix. Groups also support shared middleware (see [Middleware](./middleware.md)):

```python
api = pb.group("/api/v1")

@api.get("/status")
async def status():
    return {"ok": True}

@api.post("/items")
async def create_item(body: dict):
    return {"created": body}

@api.get("/items/{id}")
async def get_item(id: str):
    return {"id": id}
```

### Nested groups

```python
admin_api = pb.group("/api/admin")
reports = admin_api.group("/reports")   # prefix = /api/admin/reports

@reports.get("/daily")
async def daily_report():
    return {"report": "daily"}

@reports.get("/weekly")
async def weekly_report():
    return {"report": "weekly"}
```

## Auth dependencies

PPBase provides ready-made FastAPI `Depends` helpers:

```python
from typing import Any

# Optional — returns auth payload or None
@pb.get("/me")
async def me(auth: dict[str, Any] | None = pb.optional_auth()):
    if not auth:
        return {"user": None}
    return {"user": auth}

# Required auth record (user or superuser)
@pb.get("/profile")
async def profile(auth: dict[str, Any] = pb.require_auth()):
    return {"id": auth["id"], "type": auth["type"]}

# Superuser / admin only
@pb.get("/admin/stats")
async def admin_stats(_: dict = pb.require_admin()):
    return {"totalUsers": 42}
```

## JSON responses & errors

```python
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response

@pb.get("/resource/{id}")
async def get_resource(id: str):
    resource = await fetch_resource(id)
    if resource is None:
        raise HTTPException(
            status_code=404,
            detail={"status": 404, "message": "Not found.", "data": {}},
        )
    return resource

@pb.delete("/resource/{id}")
async def del_resource(id: str):
    await remove_resource(id)
    return Response(status_code=204)   # always use Response, not JSONResponse, for 204
```

## Registering via `pb.route()`

The `route()` method is the underlying primitive and accepts a `methods` list:

```python
@pb.route("/multi", methods=["GET", "HEAD"])
async def multi():
    return {"ok": True}
```

## Registering routes from a function (for `load_hooks`)

When splitting into multiple files, wrap registrations in a `setup(pb)` function:

```python
# routes/blog.py
from typing import Any

def setup(pb):

    blog = pb.group("/api/blog")

    @blog.get("/posts")
    async def list_posts():
        records = await pb.records("posts").list(sort="-created")
        return records

    @blog.get("/posts/{slug}")
    async def get_post(slug: str):
        results = await pb.records("posts").list(
            filter=f'slug="{slug}"',
            per_page=1,
        )
        items = results.get("items", [])
        if not items:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail={"status": 404, "message": "Post not found.", "data": {}})
        return items[0]
```

Then in your entry-point:

```python
pb.load_hooks("routes.blog:setup")
```
