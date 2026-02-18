#!/usr/bin/env python3
"""PPBase dev server — multi-file example.

Demonstrates loading hooks and routes from separate modules using
pb.load_hooks("module.path:setup").

Layout
------
test/
├── run_server.py          ← this file (entry-point)
├── hooks/
│   ├── audit.py           ← cross-cutting audit log (all collections)
│   ├── users.py           ← users collection: inject defaults, auth logging
│   └── realtime.py        ← SSE: connection guard, topic filter, payload tag
└── routes/
    ├── custom_api.py      ← /api/custom/* group with middleware + auth helpers
    └── webhooks.py        ← /api/webhooks/* receivers

Usage
-----
    python test/run_server.py           # foreground, port 8090
    python test/run_server.py 9000      # custom port
    python test/run_server.py --publicDir ./test/public_site
    python test/run_server.py --db postgresql+asyncpg://user:pass@host:5432/mydb
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

# ── path setup ───────────────────────────────────────────────────────────────
# Allow imports of both ppbase and the test-local hooks/routes packages.

_root = Path(__file__).resolve().parent.parent
_test = Path(__file__).resolve().parent

for _p in (_root, _test):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ── import the pb facade ──────────────────────────────────────────────────────

from ppbase import pb  # noqa: E402
from fastapi import Request  # noqa: E402

# ── lifecycle hooks (registered inline — not split out) ───────────────────────

from ppbase.ext.events import BootstrapEvent, ServeEvent, TerminateEvent  # noqa: E402


@pb.on_bootstrap()
async def on_bootstrap(event: BootstrapEvent):
    print("[lifecycle] bootstrap — settings ready")
    await event.next()


@pb.on_serve()
async def on_serve(event: ServeEvent):
    print("[lifecycle] serve — server is up ✓")
    await event.next()


@pb.on_terminate()
async def on_terminate(event: TerminateEvent):
    print("[lifecycle] terminate — shutting down…")
    await event.next()


# ── global extension middleware (all custom routes) ────────────────────────────

import time  # noqa: E402
from ppbase.ext.events import RecordRequestEvent, RouteRequestEvent  # noqa: E402


@pb.middleware(priority=200)
async def global_timer(event: RouteRequestEvent):
    """Time every extension-route request and log it."""
    started = time.perf_counter()
    result = await event.next()
    elapsed = (time.perf_counter() - started) * 1000
    print(f"[global] {event.method} {event.path} → {elapsed:.1f} ms")
    return result


@pb.middleware(priority=150)
async def block_demo_ua(event: RouteRequestEvent):
    """Example: block a specific User-Agent (demo only)."""
    ua = event.headers.get("user-agent", "").lower()
    if "blockme" in ua:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=403,
            content={"status": 403, "message": "Blocked client.", "data": {}},
        )
    return await event.next()


@pb.middleware(priority=120, path="/api/custom/*", methods=["GET"])
async def custom_get_probe(event: RouteRequestEvent):
    """Run only for GET /api/custom/* extension routes."""
    print(f"[custom-get] {event.method} {event.path}")
    return await event.next()


@pb.middleware(priority=110, path="/trace", methods=["GET"])
async def trace_context(event: RouteRequestEvent):
    """Request-local store example (middleware -> handler)."""
    event.set("traceId", f"trace-{int(time.time() * 1000)}")
    event.set("startedAt", time.perf_counter())
    result = await event.next()
    started_at = float(event.get("startedAt", time.perf_counter()))
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    print(f"[trace] id={event.get('traceId')} elapsed={elapsed_ms:.1f} ms")
    return result


# ── record hooks (inline demo) ────────────────────────────────────────────────

@pb.on_record_update_request("users", priority=70)
async def users_avatar_to_grayscale(event: RecordRequestEvent):
    """Convert uploaded users.avatar images to grayscale before saving."""
    avatar_uploads = event.files.get("avatar") or []
    if not avatar_uploads:
        return await event.next()

    try:
        from PIL import Image
    except Exception:
        print("[users] Pillow not available; avatar grayscale hook skipped")
        return await event.next()

    import io

    format_by_ext = {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
        ".gif": "GIF",
        ".bmp": "BMP",
        ".tif": "TIFF",
        ".tiff": "TIFF",
    }

    converted_uploads: list[tuple[str, bytes]] = []
    for original_name, content in avatar_uploads:
        try:
            with Image.open(io.BytesIO(content)) as source:
                source.load()
                gray = source.convert("L")
                target_format = (
                    (source.format or "").upper()
                    or format_by_ext.get(Path(original_name).suffix.lower(), "PNG")
                )

                output = io.BytesIO()
                gray.save(output, format=target_format)

                target_name = original_name
                if target_format == "PNG" and Path(original_name).suffix.lower() != ".png":
                    target_name = f"{Path(original_name).stem or 'avatar'}.png"

                converted_uploads.append((target_name, output.getvalue()))
        except Exception:
            converted_uploads.append((original_name, content))

    event.files["avatar"] = converted_uploads
    print(f"[users] grayscale avatar conversion applied ({len(converted_uploads)} file(s))")
    return await event.next()


# ── simple top-level routes (registered directly) ─────────────────────────────

@pb.get("/hello")
async def hello():
    """Basic smoke-test endpoint."""
    return {"message": "Hello from PPBase!", "ok": True}


@pb.get("/hello/{name}")
async def hello_name(name: str):
    """Greet by name — demonstrates path parameters."""
    return {"message": f"Hello, {name}!", "name": name}


@pb.post("/reflect")
async def reflect(body: dict):
    """Echo the request body back — useful for debugging client payloads."""
    return {"reflected": body, "keys": list(body.keys())}


@pb.get("/trace")
async def trace_demo(store: dict = pb.request_store()):
    """Read values set by middleware from request-local store."""
    return {
        "traceId": store.get("traceId"),
        "hasTrace": "traceId" in store,
    }


@pb.get("/auth-response-preview")
async def auth_response_preview(request: Request):
    """Demo of pb.apis.record_auth_response + pb.apis.enrich_record."""
    fake_collection = SimpleNamespace(id="users_id", name="users")
    fake_record = {
        "id": "preview_user",
        "email": "demo@example.com",
        "title": "Preview Record",
        "internal": "hidden-by-fields-if-requested",
    }
    enriched = await pb.apis.enrich_record(
        request,
        fake_record,
        collection=fake_collection,
    )
    return await pb.apis.record_auth_response(
        request,
        enriched or fake_record,
        collection=fake_collection,
        token="preview.token.value",
        auth_method="preview",
        meta={"source": "run_server.py"},
    )


# ── load external modules ─────────────────────────────────────────────────────
# Order matters: cross-cutting concerns first, feature hooks second.

pb.load_hooks("hooks.audit:setup")       # runs at priority=1 — logs after every mutation
pb.load_hooks("hooks.users:setup")       # users-specific: defaults, ownership, login log
pb.load_hooks("hooks.realtime:setup")    # SSE: connect guard, topic filter, payload tag

pb.load_hooks("routes.custom_api:setup") # /api/custom/*  (with group middleware)
pb.load_hooks("routes.webhooks:setup")   # /api/webhooks/*


# ── start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pb.configure(database_url="postgresql+asyncpg://ppbase:ppbase@localhost:5433/ppbase2", migrations_dir="./test/migrations")

    print()
    print("┌─────────────────────────────────────────────────────────────┐")
    print("│  PPBase dev server  (multi-file example)                    │")
    print("├─────────────────────────────────────────────────────────────┤")
    print(f"│  Admin UI  →  http://127.0.0.1:8090/_/                       │")
    print(f"│  API       →  http://127.0.0.1:8090/api/                      │")
    print(f"│  DB        →  postgresql+asyncpg://ppbase*:*@localhost:5433/ppbase2 │")
    print(f"│  Migrations →  ./test/migrations │")
    print(f"│  Public    →  ./test/public_site │")
    print("├─────────────────────────────────────────────────────────────┤")
    print("│  Custom routes                                              │")
    print("│    GET   /hello                                             │")
    print("│    GET   /hello/:name                                       │")
    print("│    POST  /reflect                                           │")
    print("│    GET   /trace                     (middleware store demo) │")
    print("│    GET   /auth-response-preview     (apis auth/enrich demo)│")
    print("│    GET   /api/custom/status                                 │")
    print("│    GET   /api/custom/me              (optional auth)        │")
    print("│    GET   /api/custom/search?q=...    (query params)         │")
    print("│    POST  /api/custom/echo                                   │")
    print("│    GET   /api/custom/private         (auth required)        │")
    print("│    GET   /api/custom/admin           (superuser only)       │")
    print("│    GET   /api/custom/stats           (superuser only)       │")
    print("│    GET   /api/custom/health          (plaintext OK)         │")
    print("│    POST  /api/webhooks/generic                              │")
    print("│    POST  /api/webhooks/github                               │")
    print("│    POST  /api/webhooks/stripe                               │")
    print("├─────────────────────────────────────────────────────────────┤")
    print("│  Hook modules loaded                                        │")
    print("│    hooks/audit.py       ← create/update/delete/view logs   │")
    print("│    hooks/users.py       ← defaults, ownership, login log   │")
    print("│    hooks/realtime.py    ← SSE filter + payload tag         │")
    print("│    routes/custom_api.py ← group middleware + auth deps     │")
    print("│    routes/webhooks.py   ← GitHub / Stripe receivers        │")
    print("│  Global middleware                                          │")
    print("│    @pb.middleware(priority=200)  ← request timer           │")
    print("│    @pb.middleware(priority=150)  ← UA block (demo)         │")
    print("│    @pb.middleware(path='/api/custom/*', methods=['GET'])   │")
    print("│    @pb.middleware(path='/trace') + pb.request_store()      │")
    print("│    @pb.on_record_update_request('users') -> avatar gray    │")
    print("│    pb.apis.record_auth_response(request, record, ...)      │")
    print("│    pb.apis.enrich_record(request, record, ...)             │")
    print("│    route/group unbind: unbind=['middleware-id']            │")
    print("└─────────────────────────────────────────────────────────────┘")
    print()
    print("  Ctrl+C to stop")
    print()

    pb.start(host="127.0.0.1", port=8090)
