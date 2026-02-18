# PPBase Backend Developer Guide

PPBase is a Python reimplementation of [PocketBase](https://pocketbase.io/) backed by PostgreSQL. It exposes the full PocketBase REST API and adds a Python extension system for custom routes, hooks, and middleware.

## Table of Contents

| Guide | Description |
|-------|-------------|
| [Getting Started](./getting-started.md) | Installation, first run, create-admin |
| [Custom Routes](./routes.md) | `@pb.get`, `@pb.post`, route groups |
| [Hooks](./hooks.md) | Lifecycle & record event hooks |
| [Middleware](./middleware.md) | Global, group, and route-level middleware |
| [Record Repository](./records.md) | Async CRUD helpers inside hooks |
| [Auth Guards](./auth.md) | Auth helpers, superuser checks |
| [Realtime / SSE](./realtime.md) | Subscribe hooks, message events |
| [Multi-file Projects](./multi-file.md) | Organising hooks across files |

## Architecture at a glance

```
your_app.py          ← entry-point, creates pb = FlaskLikePB(...)
│
├── pb.get("/path")           ← registers FastAPI route via ExtensionRegistry
├── pb.on_record_create_request("posts")  ← binds hook handler
├── pb.middleware(priority=100)           ← global extension middleware
└── pb.start()                            ← starts uvicorn

ppbase/
├── app.py            ← FastAPI app factory (create_app)
├── api/              ← built-in REST routes (collections, records, auth…)
├── services/         ← business logic (record_service, auth_service…)
├── db/               ← SQLAlchemy engine, system tables, schema_manager
└── ext/              ← extension system
    ├── flask_like_pb.py   ← FlaskLikePB facade (the `pb` object)
    ├── registry.py        ← ExtensionRegistry, route/hook storage
    ├── events.py          ← HookEvent subclasses
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
| `PPBASE_HOST` | `127.0.0.1` | Bind host |
| `PPBASE_PORT` | `8090` | Bind port |
| `PPBASE_DEV` | `false` | Enable debug mode + Swagger UI |
| `PPBASE_LOG_LEVEL` | `info` | Uvicorn log level |
| `PPBASE_ORIGINS` | `*` | CORS allowed origins (comma-separated) |
| `PPBASE_AUTO_MIGRATE` | `true` | Auto-apply migrations on start |

Settings can also be passed to `FlaskLikePB()` directly:

```python
pb = FlaskLikePB(
    database_url="postgresql+asyncpg://user:pass@localhost/mydb",
    port=9000,
    dev=True,
)
```
