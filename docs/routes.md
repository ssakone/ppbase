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
```

### Nested groups

```python
admin_api = pb.group("/api/admin")
reports = admin_api.group("/reports")   # prefix = /api/admin/reports

@reports.get("/daily")
async def daily_report():
    return {"report": "daily"}
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
async def profile(auth: dict[str, Any] = pb.require_record_auth()):
    return {"id": auth["id"], "type": auth["type"]}

# Legacy admin token only (`type="admin"`)
@pb.get("/admin/stats")
async def admin_stats(_: dict = pb.require_admin()):
    return {"totalUsers": 42}

# Superuser guard (admin token OR _superusers auth record)
@pb.get("/superuser/stats", middlewares=[pb.apis.require_superuser_auth()])
async def superuser_stats():
    return {"ok": True}
```

`pb.require_admin()` matches legacy admin tokens only.  
For PocketBase-style superuser guard, prefer `middlewares=[pb.apis.require_superuser_auth()]`.

## Multipart file uploads in custom routes

You can receive files in custom routes with FastAPI, then write them via `RecordRepository`:

```python
from fastapi import File, Form, UploadFile

@pb.post("/api/custom/avatar")
async def upload_avatar(
    user_id: str = Form(...),
    avatar: UploadFile = File(...),
):
    content = await avatar.read()
    updated = await pb.records("users").update(
        user_id,
        data={},
        files={"avatar": [(avatar.filename or "avatar.png", content)]},
    )
    return updated
```

Returned records keep PB-compatible file names. Build public URL as:

```text
/api/files/{collection}/{recordId}/{filename}
```

## Built-in auth route middlewares (`pb.apis`)

`pb.apis` provides declarative auth guards as route-level middleware — no need to write the guard logic manually:

```python
# Any authenticated user
@api.get("/private", middlewares=[pb.apis.require_auth()])
async def private(auth = pb.optional_auth()):
    return {"id": auth["id"] if auth else None}

# Restricted to a specific collection
@api.get("/users-only", middlewares=[pb.apis.require_auth("users")])
async def users_only():
    return {"ok": True}

# Superuser only
@api.get("/admin", middlewares=[pb.apis.require_superuser_auth()])
async def admin():
    return {"admin": True}

# Superuser OR the record's owner (path param name defaults to "id")
@api.get("/users/{id}", middlewares=[pb.apis.require_superuser_or_owner_auth("id")])
async def get_user(id: str):
    return await pb.records("users").get(id)

# Guest only — reject authenticated requests
@api.post("/register", middlewares=[pb.apis.require_guest_only()])
async def register(body: dict):
    return {"registered": True}
```

## Record enrichment & auth responses (`pb.apis`)

### `pb.apis.enrich_record` / `pb.apis.enrich_records`

Apply the `?expand=` and `?fields=` query parameters from the current request to a record or list of records:

```python
from fastapi import Request

@pb.get("/posts/{id}")
async def get_post(id: str, request: Request):
    record = await pb.records("posts").get(id)
    # Respects ?expand=author&fields=id,title,author
    enriched = await pb.apis.enrich_record(request, record, collection="posts")
    return enriched

@pb.get("/posts")
async def list_posts(request: Request):
    result = await pb.records("posts").list(sort="-created")
    items = await pb.apis.enrich_records(request, result["items"], collection="posts")
    return {**result, "items": items}
```

Signature:

```python
await pb.apis.enrich_record(
    request,          # FastAPI Request
    record,           # dict | None
    collection=...,   # CollectionRecord | str | None (resolved automatically from record if omitted)
    default_expand=None,  # fallback expand string if ?expand= is not in the request
)

await pb.apis.enrich_records(
    request,
    records,          # list[dict]
    collection=...,
    default_expand=None,
)
```

### `pb.apis.record_auth_response`

Build a PocketBase-compatible `{token, record, meta}` auth response — handles token generation, record enrichment, and triggers the generic `on_record_auth_request` hook:

```python
@pb.post("/custom-login")
async def custom_login(body: dict, request: Request):
    # Validate credentials your way…
    user = await pb.records("users").list(
        filter=f'email="{body["email"]}"', per_page=1
    )
    if not user["items"]:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail={"status": 400, "message": "Invalid credentials.", "data": {}})

    record = user["items"][0]

    # Builds token automatically if `token` is omitted
    return await pb.apis.record_auth_response(
        request,
        record,
        collection="users",       # required: collection name or object
        auth_method="custom",     # logged in hooks / response meta
        meta={"provider": "sso"}, # optional extra fields in response
        # token="override.jwt.here",  # supply your own token if needed
    )
```

Response shape (matches PocketBase):
```json
{
  "token": "eyJ...",
  "record": { "id": "...", "email": "...", ... },
  "meta": { "provider": "sso" }
}
```

---

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

## `pb.configure()` — override settings at runtime

Call `pb.configure()` before `pb.start()` or `pb.get_app()` to change any setting programmatically. This is useful when settings depend on CLI args or environment detection at runtime:

```python
import sys
from ppbase import pb

if __name__ == "__main__":
    db_url = next((a for a in sys.argv if a.startswith("--db=")), None)
    if db_url:
        pb.configure(database_url=db_url.split("=", 1)[1])

    pb.configure(migrations_dir="./migrations", public_dir="./public")
    pb.start()
```

`configure()` raises `RuntimeError` if called after `get_app()` or `start()`.

## `pb.route()` — generic method registration

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
def setup(pb):
    from fastapi import HTTPException

    blog = pb.group("/api/blog")

    @blog.get("/posts")
    async def list_posts():
        return await pb.records("posts").list(sort="-created")

    @blog.get("/posts/{slug}")
    async def get_post_by_slug(slug: str):
        results = await pb.records("posts").list(
            filter=f'slug="{slug}"', per_page=1,
        )
        items = results.get("items", [])
        if not items:
            raise HTTPException(status_code=404, detail={"status": 404, "message": "Post not found.", "data": {}})
        return items[0]
```

Then in your entry-point:

```python
pb.load_hooks("routes.blog:setup")
```
