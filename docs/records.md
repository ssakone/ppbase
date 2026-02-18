# Record Repository

`RecordRepository` provides async CRUD helpers for any collection. Use it inside hooks, custom routes, and background tasks — anywhere you need to read or write records programmatically.

## Getting a repository

```python
# From the pb facade (outside a request context)
repo = pb.records("posts")

# From inside a hook event
repo = event.records("posts")

# From inside a route handler
@pb.get("/blog/featured")
async def featured():
    repo = pb.records("posts")
    return await repo.list(filter='featured=true', sort="-created", per_page=5)
```

---

## `list` — paginated query

```python
result = await pb.records("posts").list(
    page=1,
    per_page=20,
    sort="-created",           # "-" prefix = descending
    filter='status="published" && views>100',
    fields="id,title,slug,created",  # optional field projection
    skip_total=False,          # set True to skip COUNT query
)
# {
#   "page": 1, "perPage": 20, "totalItems": 42, "totalPages": 3,
#   "items": [{...}, ...]
# }
```

### Filter syntax (PocketBase compatible)

```
title~"python"             # contains (case-insensitive)
title="Exact"              # equals
views>100                  # comparison: > < >= <= !=
status="pub" && views>0   # AND
status="draft" || views=0  # OR
!(status="archived")       # NOT
created>="2024-01-01"     # date comparison
author_id=@request.auth.id # request context macro (list/view rules only)
```

---

## `get` — single record by ID

```python
post = await pb.records("posts").get("record_id_here")
if post is None:
    raise HTTPException(status_code=404, ...)

# with field projection
post = await pb.records("posts").get("abc123", fields="id,title,body")
```

---

## `create` — insert a new record

```python
record = await pb.records("posts").create({
    "title": "Hello PPBase",
    "body": "Content here…",
    "status": "published",
    "views": 0,
})
print(record["id"])
```

### With file upload

```python
with open("cover.jpg", "rb") as f:
    data = f.read()

record = await pb.records("posts").create(
    {"title": "With cover"},
    files={"cover": [("cover.jpg", data)]},
)
```

---

## `update` — partial update

```python
updated = await pb.records("posts").update(
    "record_id_here",
    {"views": 42, "status": "archived"},
)
```

---

## `delete` — remove a record

```python
deleted = await pb.records("posts").delete("record_id_here")
# returns True if deleted, False if not found
```

---

## Patterns inside hooks

For record request hooks (`on_record_*_request`), `event.next()` usually returns a FastAPI `Response` object (not a plain dict).  
Use `event.records(...)` for DB reads/writes, and treat the returned response as transport output.

### Enrich response after create

```python
@pb.on_record_create_request("orders")
async def enrich_order(event):
    result = await event.next()
    if getattr(result, "status_code", 500) >= 400:
        return result

    # create a related notification
    await event.records("notifications").create({
        "user_id": event.data.get("user_id", ""),
        "message": "Order confirmed",
        "read": False,
    })
    return result
```

### Cross-collection lookup

```python
@pb.on_record_view_request("products")
async def attach_reviews(event):
    result = await event.next()
    if getattr(result, "status_code", 500) >= 400:
        return result

    # Example side effect on successful view request
    reviews = await event.records("reviews").list(
        filter=f'product_id="{event.record_id}"',
        sort="-created",
        per_page=5,
        fields="id,rating,body,created",
    )
    print(f"product {event.record_id} has {len(reviews.get('items', []))} recent review(s)")
    return result
```

### Soft-delete pattern

```python
@pb.on_record_delete_request("posts")
async def soft_delete_posts(event):
    """Replace hard delete with soft delete (set deleted_at field)."""
    record_id = event.record_id
    await event.records("posts").update(record_id, {
        "deleted_at": "2024-01-01T00:00:00Z",   # use datetime.utcnow().isoformat()
        "status": "archived",
    })
    # Return 204 without calling event.next() — skip actual delete
    from fastapi.responses import Response
    return Response(status_code=204)
```

### Cascade delete

```python
@pb.on_record_delete_request("authors")
async def cascade_author_posts(event):
    # delete all posts before deleting the author
    author_id = event.record_id
    posts = await event.records("posts").list(
        filter=f'author_id="{author_id}"',
        per_page=500,
        skip_total=True,
        fields="id",
    )
    for post in posts.get("items", []):
        await event.records("posts").delete(post["id"])

    return await event.next()
```

---

## `RecordRepository` API reference

| Method | Signature | Returns |
|--------|-----------|---------|
| `list` | `(page, per_page, sort, filter, fields, skip_total, request_context)` | `dict` (PocketBase paginated result) |
| `get` | `(record_id, *, fields)` | `dict \| None` |
| `create` | `(data, *, files)` | `dict` (created record) |
| `update` | `(record_id, data, *, files)` | `dict \| None` |
| `delete` | `(record_id)` | `bool` |
| `resolve_collection` | `()` | `CollectionRecord` (raises if not found) |
