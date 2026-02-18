"""Custom API routes module.

Demonstrates:
  - Route groups
  - Group-level middleware
  - Builtin PocketBase-like route middlewares (`pb.apis.require_*`)
  - Auth dependencies (optional)
  - Repository usage inside routes
  - Query parameters
  - File-like response

Loaded via:
    pb.load_hooks("routes.custom_api:setup")
"""

from __future__ import annotations

import time
from typing import Any


def setup(pb) -> None:
    from fastapi import Query
    from fastapi.responses import PlainTextResponse
    from ppbase.ext.events import RouteRequestEvent

    # ── group middleware ──────────────────────────────────────────────────────

    api = pb.group("/api/custom")

    @api.middleware(priority=10)
    async def log_custom_api(event: RouteRequestEvent):
        start = time.perf_counter()
        result = await event.next()
        ms = (time.perf_counter() - start) * 1000
        print(f"[custom] {event.method} {event.path} ({ms:.1f} ms)")
        return result

    # ── public routes ─────────────────────────────────────────────────────────

    @api.get("/status")
    async def status():
        return {
            "status": "running",
            "timestamp": time.time(),
            "version": "dev",
        }

    @api.post("/echo")
    async def echo(body: dict[str, Any]):
        return {"echo": body}

    @api.get("/health")
    async def health():
        return PlainTextResponse("OK")

    # ── search route (query params) ───────────────────────────────────────────

    @api.get("/search")
    async def search(
        q: str = Query("", description="Search term"),
        collection: str = Query("users", description="Collection to search"),
        page: int = Query(1, ge=1),
        per_page: int = Query(10, ge=1, le=100),
    ):
        if not q:
            return {"items": [], "query": q}

        results = await pb.records(collection).list(
            page=page,
            per_page=per_page,
            sort="-created",
            fields="id,created",
        )
        return {"query": q, "collection": collection, **results}

    # ── auth-aware "me" endpoint ──────────────────────────────────────────────

    @api.get("/me")
    async def me(auth: dict[str, Any] | None = pb.optional_auth()):
        if not auth or auth.get("type") != "authRecord":
            return {"authenticated": False, "user": None}

        user = await pb.records(
            auth.get("collectionName") or auth.get("collectionId", "users")
        ).get(auth["id"], fields="id,email,verified")

        return {"authenticated": True, "user": user}

    # ── protected endpoints ────────────────────────────────────────────────────

    @api.get("/private", middlewares=[pb.apis.require_auth()])
    async def private(auth: dict[str, Any] | None = pb.optional_auth()):
        return {
            "ok": True,
            "type": auth.get("type") if auth else None,
            "id": auth.get("id") if auth else None,
        }

    @api.get("/admin", middlewares=[pb.apis.require_superuser_auth()])
    async def admin_endpoint(auth: dict[str, Any] | None = pb.optional_auth()):
        return {
            "message": "Welcome, superuser!",
            "id": auth.get("id") if auth else None,
        }

    # ── stats endpoint (admin only) ────────────────────────────────────────────

    @api.get("/stats", middlewares=[pb.apis.require_superuser_auth()])
    async def stats():
        from ppbase.db.engine import get_engine
        from ppbase.services.record_service import get_all_collections

        engine = get_engine()
        collections = await get_all_collections(engine)
        return {
            "collections": len(collections),
            "names": [c.name for c in collections],
        }
