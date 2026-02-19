# PPBase Backend Developer Guide

PPBase is a Python reimplementation of [PocketBase](https://pocketbase.io/) backed by PostgreSQL. It exposes the full PocketBase REST API and adds a Python extension system for custom routes, hooks, and middleware.

## Table of Contents

| Guide | Description |
|-------|-------------|
| [Getting Started](./getting-started.md) | Installation, first run, create-admin |
| [Production Deployment](./production.md) | Deployment checklist, scaling and hardening |
| [Custom Routes](./routes.md) | `@pb.get`, `@pb.post`, route groups, `pb.configure()`, `pb.apis` |
| [Hooks](./hooks.md) | Lifecycle & record event hooks |
| [Middleware](./middleware.md) | Global/group/route extension middleware, plus built-in API rate limiting |
| [Record Repository](./records.md) | Async CRUD helpers inside hooks |
| [Auth Guards](./auth.md) | Auth helpers, superuser checks |
| [Realtime / SSE](./realtime.md) | Subscribe hooks, message events |
| [Storage & Assets](./storage.md) | Local vs S3/R2 storage, public dir, file URLs |
| [Multi-file Projects](./multi-file.md) | Organising hooks across files |

## Architecture at a glance

```
your_app.py          ← entry-point, creates pb = FlaskLikePB(...)
│
├── pb.get("/path")                           ← registers FastAPI route
├── pb.middleware(priority=N, path="/api/*")  ← filtered global middleware
├── pb.on_record_create_request("posts")      ← binds record hook
├── pb.apis.require_auth()                    ← builtin route guard
├── pb.configure(database_url=..., ...)       ← settings override
└── pb.start()                                ← starts uvicorn

ppbase/
├── app.py            ← FastAPI app factory (create_app)
├── api/              ← built-in REST routes (collections, records, auth…)
├── services/         ← business logic (record_service, auth_service…)
├── db/               ← SQLAlchemy engine, system tables, schema_manager
└── ext/              ← extension system
    ├── flask_like_pb.py   ← FlaskLikePB facade (the `pb` object)
    │     ├── pb.apis      ← _BuiltinRouteMiddlewares (require_auth, enrich_record…)
    │     └── pb.request_store()  ← per-request key-value store dependency
    ├── registry.py        ← ExtensionRegistry, route/hook storage
    ├── events.py          ← HookEvent subclasses + RouteRequestEvent.set/get
    ├── hooks.py           ← HookChain engine
    ├── record_repository.py  ← async CRUD helpers
    └── loading.py         ← pb.load_hooks() module loader
```

## Quick start (5 minutes)

```bash
# 1. Clone and install
git clone https://github.com/yourname/ppbase.git
cd ppbase
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Start PostgreSQL
python -m ppbase db start

# 3. Create admin
python -m ppbase create-admin --email admin@example.com --password secret123

# 4. Run your app
python my_app.py
# → Admin UI at http://127.0.0.1:8090/_/
```

```python
# my_app.py — minimal app
from ppbase import pb

@pb.get("/hello")
async def hello():
    return {"message": "Hello from PPBase!"}

if __name__ == "__main__":
    pb.start(host="127.0.0.1", port=8090)
```

## Environment variables

All settings use the `PPBASE_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `PPBASE_DATABASE_URL` | `postgresql+asyncpg://ppbase:ppbase@localhost:5433/ppbase` | PostgreSQL DSN |
| `PPBASE_HOST` | `0.0.0.0` | Bind host |
| `PPBASE_PORT` | `8090` | Bind port |
| `PPBASE_DATA_DIR` | `./pb_data` | Data directory (local storage + project secret file) |
| `PPBASE_DEV` | `false` | Enable debug mode + Swagger UI |
| `PPBASE_LOG_LEVEL` | `INFO` | Uvicorn log level |
| `PPBASE_ORIGINS` | `*` | CORS allowed origins (comma-separated) |
| `PPBASE_AUTO_MIGRATE` | `true` | Auto-apply migrations on start |
| `PPBASE_PUBLIC_DIR` | _(none)_ | Optional directory served as static files at `/` |
| `PPBASE_MIGRATIONS_DIR` | `pb_migrations` | Directory where migration files live |
| `PPBASE_STORAGE_BACKEND` | `local` | `local` or `s3` |
| `PPBASE_S3_ENDPOINT` | _(none)_ | S3-compatible endpoint (AWS/Cloudflare R2/MinIO) |
| `PPBASE_S3_BUCKET` | _(none)_ | Bucket name |
| `PPBASE_S3_REGION` | _(none)_ | Region (for R2 commonly `auto`) |
| `PPBASE_S3_ACCESS_KEY` | _(none)_ | Access key / key id |
| `PPBASE_S3_SECRET_KEY` | _(none)_ | Secret key |
| `PPBASE_S3_FORCE_PATH_STYLE` | `false` | Force path-style addressing |
| `PPBASE_JWT_SECRET` | _(auto)_ | If unset, generated and persisted to `data_dir/.jwt_secret` |

Settings can also be passed to `FlaskLikePB()` directly or overridden via `pb.configure()`:

```python
from ppbase import FlaskLikePB

# Constructor kwargs
pb = FlaskLikePB(
    database_url="postgresql+asyncpg://user:pass@localhost/mydb",
    port=9000,
    dev=True,
    public_dir="./public",
)

# Or runtime override before start (useful for CLI args):
pb.configure(database_url="...", migrations_dir="./my_migrations")
pb.start()
```

## `pb` facade — full API surface

### Route registration

| Method | Description |
|--------|-------------|
| `pb.get(path, **kw)` | Register GET route |
| `pb.post(path, **kw)` | Register POST route |
| `pb.put / .patch / .delete / .options / .head` | Other HTTP verbs |
| `pb.route(path, methods=[...], **kw)` | Generic multi-method |
| `pb.group(prefix)` | Create a `RouteGroup` |

Route kwargs: `middlewares=[...]`, `unbind=["id"]`, plus any FastAPI kwargs (tags, response_model, etc.).

### Middleware

| Method | Description |
|--------|-------------|
| `@pb.middleware(priority, path, paths, methods, id, predicate)` | Global extension middleware |
| `pb.use(fn, priority, path, methods, id, predicate)` | Programmatic alias |
| `pb.unbind_middleware(id)` | Remove global middleware by ID |
| `pb.request_store()` | `Depends` — access request-local store in handlers |

### Auth dependencies (FastAPI `Depends`)

| Method | Description |
|--------|-------------|
| `pb.optional_auth()` | Returns auth payload or `None` |
| `pb.require_auth()` | Raises 401 if not authenticated |
| `pb.require_record_auth()` | Raises 401/403 if missing or not an auth-record token |
| `pb.require_admin()` | Legacy admin-token guard (`type="admin"`) |

### Built-in route middlewares (`pb.apis`)

| Method | Description |
|--------|-------------|
| `pb.apis.require_auth(*collections)` | Any auth; optionally restricted to collection(s) |
| `pb.apis.require_superuser_auth()` | Admin or `_superusers` member |
| `pb.apis.require_superuser_or_owner_auth(param)` | Superuser OR record owner via path param |
| `pb.apis.require_guest_only()` | Rejects authenticated requests |
| `await pb.apis.enrich_record(request, record, collection)` | Apply `?expand` / `?fields` to one record |
| `await pb.apis.enrich_records(request, records, collection)` | Apply to a list |
| `await pb.apis.record_auth_response(request, record, collection, ...)` | Build PB auth response `{token, record, meta}` |

### Data access

| Method | Description |
|--------|-------------|
| `pb.records(collection)` | Returns `RecordRepository` for CRUD |

### Hooks

| Method | Description |
|--------|-------------|
| `pb.on_bootstrap()` | Before DB init |
| `pb.on_serve()` | After server ready |
| `pb.on_terminate()` | On graceful shutdown |
| `pb.on_records_list_request(*colls)` | Before list |
| `pb.on_record_view_request(*colls)` | Before view |
| `pb.on_record_create_request(*colls)` | Before create |
| `pb.on_record_update_request(*colls)` | Before update |
| `pb.on_record_delete_request(*colls)` | Before delete |
| `pb.on_record_auth_with_password_request(*colls)` | Before password auth |
| `pb.on_record_request_otp_request(*colls)` | Before OTP request |
| `pb.on_record_auth_with_otp_request(*colls)` | Before OTP auth |
| `pb.on_record_auth_refresh_request(*colls)` | Before token refresh |
| `pb.on_record_auth_with_oauth2_request(*colls)` | Before OAuth2 auth |
| `pb.on_record_auth_request(*colls)` | Generic auth hook |
| `pb.on_file_token_request(*colls)` | Before file token generation |
| `pb.on_file_download_request(*colls)` | Before file download response |
| `pb.on_realtime_connect_request()` | Before SSE connect |
| `pb.on_realtime_subscribe_request()` | Before topic subscribe |
| `pb.on_realtime_message_send()` | Before SSE event sent |

All hook decorators accept `id=`, `priority=` kwargs.

### App lifecycle

| Method | Description |
|--------|-------------|
| `pb.configure(**kwargs)` | Override settings (must be before `start()`) |
| `pb.get_app()` | Build and return FastAPI ASGI app |
| `pb.start(host, port)` | Start uvicorn (blocking) |
| `pb.load_hooks("module:fn")` | Import and run a `setup(pb)` function |
