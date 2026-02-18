# Multi-file Projects

As your application grows, you'll want to split hooks and routes across multiple modules. PPBase supports this via `pb.load_hooks()`.

## The `load_hooks` pattern

Each module exposes a `setup(pb)` function that receives the `pb` facade and registers its own hooks and routes:

```python
# hooks/users.py
def setup(pb):
    from ppbase.ext.events import RecordRequestEvent

    @pb.on_record_create_request("users")
    async def on_user_create(event: RecordRequestEvent):
        event.data.setdefault("role", "member")
        return await event.next()

    @pb.on_record_delete_request("users")
    async def on_user_delete(event: RecordRequestEvent):
        user_id = event.record_id
        # delete related data
        await event.records("sessions").list(filter=f'user_id="{user_id}"')
        return await event.next()
```

```python
# main.py
from ppbase import pb

pb.load_hooks("hooks.users:setup")
pb.load_hooks("hooks.posts:setup")
pb.load_hooks("hooks.audit:setup")
pb.load_hooks("routes.blog:setup")

if __name__ == "__main__":
    pb.start()
```

The `load_hooks` argument uses `"module.path:function_name"` syntax — any importable module path works.

---

## Recommended project layout

```
my_project/
├── main.py                 ← entry-point
├── hooks/
│   ├── __init__.py
│   ├── users.py            ← user CRUD hooks
│   ├── posts.py            ← post CRUD hooks
│   ├── comments.py         ← comment hooks
│   └── audit.py            ← cross-cutting audit log
├── routes/
│   ├── __init__.py
│   ├── blog.py             ← public blog API
│   ├── metrics.py          ← internal metrics
│   └── webhooks.py         ← third-party webhooks
├── services/
│   ├── __init__.py
│   ├── email.py            ← email sending helper
│   └── slugify.py          ← slug generation util
└── ppbase/                 ← ppbase package
```

---

## Example: complete multi-file app

### `main.py`

```python
from ppbase import pb

# Load all modules
pb.load_hooks("hooks.audit:setup")     # audit last (lowest priority assumed inside)
pb.load_hooks("hooks.users:setup")
pb.load_hooks("hooks.posts:setup")
pb.load_hooks("routes.blog:setup")
pb.load_hooks("routes.metrics:setup")
pb.load_hooks("routes.webhooks:setup")

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    pb.start(host="127.0.0.1", port=port)
```

---

### `hooks/audit.py`

```python
"""Cross-cutting audit log — writes every mutation to an audit_logs collection."""

def setup(pb):
    from ppbase.ext.events import RecordRequestEvent
    import datetime

    async def _log(event: RecordRequestEvent, action: str):
        coll = event.collection.name if event.collection else event.collection_id_or_name
        try:
            await event.records("audit_logs").create({
                "collection": coll,
                "record_id": event.record_id or "",
                "action": action,
                "actor_id": event.auth_id() or "",
                "actor_type": event.auth_type() or "anonymous",
                "timestamp": datetime.datetime.utcnow().isoformat(),
            })
        except Exception as exc:
            print(f"[audit] failed to write log: {exc}")

    @pb.on_record_create_request(priority=1)
    async def audit_create(event: RecordRequestEvent):
        result = await event.next()
        await _log(event, "create")
        return result

    @pb.on_record_update_request(priority=1)
    async def audit_update(event: RecordRequestEvent):
        result = await event.next()
        await _log(event, "update")
        return result

    @pb.on_record_delete_request(priority=1)
    async def audit_delete(event: RecordRequestEvent):
        result = await event.next()
        await _log(event, "delete")
        return result
```

---

### `hooks/posts.py`

```python
"""Post collection hooks: slug generation, publish workflow, view counter."""

def setup(pb):
    from ppbase.ext.events import RecordRequestEvent

    @pb.on_record_create_request("posts", priority=50)
    async def post_before_create(event: RecordRequestEvent):
        # Auto-generate slug from title
        title = str(event.data.get("title", ""))
        if title and not event.data.get("slug"):
            import re, time
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            event.data["slug"] = f"{slug}-{int(time.time())}"

        # Set author from auth token
        if event.has_record_auth() and not event.data.get("author_id"):
            event.data["author_id"] = event.auth_id()

        # Default to draft
        event.data.setdefault("status", "draft")
        return await event.next()

    @pb.on_record_update_request("posts", priority=50)
    async def post_before_update(event: RecordRequestEvent):
        # Only allow author or superuser to update
        if not event.is_superuser():
            post = await event.records("posts").get(event.record_id, fields="id,author_id")
            if post and post.get("author_id") != event.auth_id():
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=403,
                    detail={"status": 403, "message": "Not allowed.", "data": {}},
                )
        return await event.next()

    @pb.on_record_view_request("posts", priority=10)
    async def increment_views(event: RecordRequestEvent):
        result = await event.next()
        if result and isinstance(result, dict):
            import asyncio
            views = int(result.get("views", 0)) + 1
            asyncio.create_task(
                event.records("posts").update(result["id"], {"views": views})
            )
        return result
```

---

### `hooks/users.py`

```python
"""User collection hooks: welcome email, cleanup on delete."""

def setup(pb):
    from ppbase.ext.events import RecordRequestEvent, RecordAuthRequestEvent

    @pb.on_record_create_request("users", priority=50)
    async def user_created(event: RecordRequestEvent):
        result = await event.next()
        if result:
            email = result.get("email", "")
            print(f"[users] new user: {email}")
            # await send_welcome_email(email)
        return result

    @pb.on_record_delete_request("users", priority=50)
    async def user_deleted(event: RecordRequestEvent):
        user_id = event.record_id
        # Remove all posts by this user before deleting
        posts = await event.records("posts").list(
            filter=f'author_id="{user_id}"',
            per_page=500,
            skip_total=True,
            fields="id",
        )
        for post in posts.get("items", []):
            await event.records("posts").delete(post["id"])
        return await event.next()

    @pb.on_record_auth_with_password_request("users")
    async def on_user_login(event: RecordAuthRequestEvent):
        identity = event.body.get("identity", "")
        print(f"[users] login attempt: {identity}")
        result = await event.next()
        return result
```

---

### `routes/blog.py`

```python
"""Public blog API routes."""

def setup(pb):
    from fastapi import HTTPException

    blog = pb.group("/api/blog")

    @blog.get("/posts")
    async def list_posts(page: int = 1, per_page: int = 10, tag: str = ""):
        f = 'status="published"'
        if tag:
            f += f' && tags~"{tag}"'
        return await pb.records("posts").list(
            page=page,
            per_page=per_page,
            sort="-created",
            filter=f,
            fields="id,title,slug,excerpt,author_id,views,created",
        )

    @blog.get("/posts/{slug}")
    async def get_post_by_slug(slug: str):
        results = await pb.records("posts").list(
            filter=f'slug="{slug}" && status="published"',
            per_page=1,
        )
        items = results.get("items", [])
        if not items:
            raise HTTPException(
                status_code=404,
                detail={"status": 404, "message": "Post not found.", "data": {}},
            )
        return items[0]

    @blog.get("/tags")
    async def list_tags():
        """Return all unique tags across published posts."""
        posts = await pb.records("posts").list(
            filter='status="published"',
            per_page=500,
            skip_total=True,
            fields="tags",
        )
        tags: set[str] = set()
        for post in posts.get("items", []):
            for tag in (post.get("tags") or []):
                tags.add(str(tag))
        return {"tags": sorted(tags)}
```

---

### `routes/metrics.py`

```python
"""Internal metrics endpoints (superuser-only)."""

def setup(pb):
    import time

    _start_time = time.time()
    metrics = pb.group("/api/metrics")

    async def require_superuser(event):
        await event.load_auth()
        if not event.is_superuser():
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=403,
                content={"status": 403, "message": "Superuser required.", "data": {}},
            )
        return await event.next()

    @metrics.get("/health", middlewares=[require_superuser])
    async def health():
        uptime = int(time.time() - _start_time)
        return {"uptime_seconds": uptime, "status": "ok"}

    @metrics.get("/collections", middlewares=[require_superuser])
    async def collection_stats():
        """List all collections with approximate record count."""
        from ppbase.db.engine import get_engine
        from ppbase.services.record_service import get_all_collections

        engine = get_engine()
        collections = await get_all_collections(engine)
        return {
            "total": len(collections),
            "collections": [{"id": c.id, "name": c.name, "type": c.type} for c in collections],
        }
```

---

### `routes/webhooks.py`

```python
"""Incoming webhook handlers."""

def setup(pb):
    import hmac, hashlib

    WEBHOOK_SECRET = "your-webhook-secret"

    webhooks = pb.group("/api/webhooks")

    @webhooks.post("/github")
    async def github_webhook(request):
        from fastapi import Request
        body = await request.body()
        sig = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"status": 401, "message": "Invalid signature.", "data": {}},
            )

        event_type = request.headers.get("x-github-event", "")
        print(f"[webhook] GitHub event: {event_type}")
        return {"received": True}
```

---

## Loading order matters

Hooks registered first run with higher implicit priority if no `priority=` is set. Register cross-cutting concerns (audit, auth guards) before feature-specific hooks:

```python
# Recommended order
pb.load_hooks("hooks.audit:setup")    # cross-cutting, runs last in chain
pb.load_hooks("hooks.auth_guard:setup")  # security
pb.load_hooks("hooks.users:setup")
pb.load_hooks("hooks.posts:setup")
pb.load_hooks("routes.blog:setup")
```

## Reloading in development

`pb.load_hooks()` is designed for startup-time loading. For hot-reload during development, use `uvicorn --reload` with a factory pattern:

```python
# main.py
from ppbase import pb

pb.load_hooks("hooks.users:setup")
pb.load_hooks("routes.blog:setup")

app = pb.get_app()  # expose the ASGI app for uvicorn --reload
```

```bash
uvicorn main:app --reload --port 8090
```
