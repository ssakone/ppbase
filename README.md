# PPBase

A Python reimplementation of [PocketBase](https://pocketbase.io/) backed by PostgreSQL.

PPBase gives you an instant REST API with dynamic collections, admin authentication, a built-in admin dashboard, and PocketBase-compatible endpoints -- all running on PostgreSQL instead of SQLite.

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Docker (for PostgreSQL)

### 2. Install

```bash
git clone <repo-url> ppbase && cd ppbase
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Start PostgreSQL

```bash
python -m ppbase db start
```

This creates a Docker container (`ppbase-pg`) running PostgreSQL 17 on port 5433.

### 4. Start the server

```bash
python -m ppbase serve
```

The server starts at **http://localhost:8090**. Admin UI is at **http://localhost:8090/_/**.

### 5. Create an admin account

```bash
python -m ppbase create-admin --email admin@example.com --password yourpassword
```

## CLI Reference

```bash
# Server
python -m ppbase serve              # foreground
python -m ppbase serve -d           # daemon (background)
python -m ppbase stop               # stop daemon
python -m ppbase restart            # restart daemon
python -m ppbase status             # check if running

# Database
python -m ppbase db start           # start PostgreSQL container
python -m ppbase db stop            # stop container
python -m ppbase db restart         # restart container
python -m ppbase db status          # check container status

# Admin
python -m ppbase create-admin --email <email> --password <pass>
```

A shell script (`ppctl.sh`) is also available:

```bash
./ppctl.sh start | stop | restart | status
./ppctl.sh db-start | db-stop | db-restart | db-status
```

## API Endpoints

PPBase implements the PocketBase REST API. All endpoints are under `/api/`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/admins/auth-with-password` | Admin login |
| `GET` | `/api/collections` | List collections |
| `POST` | `/api/collections` | Create collection |
| `GET` | `/api/collections/:id` | Get collection |
| `PATCH` | `/api/collections/:id` | Update collection |
| `DELETE` | `/api/collections/:id` | Delete collection |
| `GET` | `/api/collections/:col/records` | List records |
| `POST` | `/api/collections/:col/records` | Create record |
| `GET` | `/api/collections/:col/records/:id` | Get record |
| `PATCH` | `/api/collections/:col/records/:id` | Update record |
| `DELETE` | `/api/collections/:col/records/:id` | Delete record |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/settings` | Get settings |
| `PATCH` | `/api/settings` | Update settings |

### Filtering & Sorting

Records support PocketBase filter syntax:

```
GET /api/collections/posts/records?filter=title~"hello" && views>5&sort=-created&page=1&perPage=20
```

### Expand Relations

```
GET /api/collections/posts/records?expand=author,category
```

## Collection Types

- **Base** -- Standard data collections with custom fields
- **Auth** -- Collections with built-in email/password authentication fields
- **View** -- Read-only collections backed by a SQL SELECT query

## Field Types

PPBase supports 14 field types, each mapped to a physical PostgreSQL column:

| Type | PostgreSQL | Notes |
|------|-----------|-------|
| `text` | `TEXT` | Min/max length, regex pattern |
| `number` | `DOUBLE PRECISION` / `INTEGER` | Min/max, integer-only option |
| `bool` | `BOOLEAN` | |
| `email` | `VARCHAR(255)` | Domain allowlist/blocklist |
| `url` | `TEXT` | Domain allowlist/blocklist |
| `date` | `TIMESTAMPTZ` | Min/max date |
| `select` | `TEXT` / `TEXT[]` | Predefined values, single or multi |
| `file` | `TEXT` / `TEXT[]` | Max size, MIME types, single or multi |
| `relation` | `VARCHAR(15)` / `VARCHAR(15)[]` | Links to another collection |
| `json` | `JSONB` | |
| `editor` | `TEXT` | Rich text / HTML |
| `autodate` | `TIMESTAMPTZ` | Auto-set on create/update |
| `password` | `TEXT` | Stored as bcrypt hash |
| `geo_point` | `JSONB` | `{lon, lat}` |

## Admin Dashboard

The built-in admin UI at `/_/` provides:

- Collection management (create, edit, delete)
- Schema editor with full field options
- Record CRUD with field-type-aware forms
- SQL editor with syntax highlighting and autocomplete for view collections
- View collection support (read-only SQL queries)

## Architecture

```
HTTP Request -> FastAPI
  -> api/router.py -> api/{endpoint}.py
    -> services/{service}.py (business logic)
      -> db/system_tables.py (ORM for _collections, _admins)
      -> db/schema_manager.py (DDL for dynamic tables)
      -> sqlalchemy.text() (parameterized SQL for records)
```

- **Hybrid SQLAlchemy**: ORM for system tables, Core for dynamic collection tables
- **Physical columns**: Each field type maps to a real PostgreSQL column (not JSONB)
- **Filter parser**: Lark EBNF grammar translates PocketBase filter syntax to parameterized SQL
- **No migration system**: `_collections` table is the source of truth; DDL is applied directly

## Configuration

All settings use the `PPBASE_` environment variable prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `PPBASE_DATABASE_URL` | `postgresql+asyncpg://ppbase:ppbase@localhost:5433/ppbase` | PostgreSQL connection |
| `PPBASE_PORT` | `8090` | Server port |
| `PPBASE_HOST` | `0.0.0.0` | Bind address |

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run a specific test
pytest tests/test_specific.py::test_name -v
```

## Flask-like Extension API

PPBase can be used as a Python-extensible app with global decorators.

### Single-file usage

```python
from ppbase import pb


@pb.get("/hello/{name}")
async def hello(name: str):
    return {"message": f"Hello {name}"}


@pb.on_record_create_request("posts")
async def normalize_post(e):
    if "title" in e.data and "slug" not in e.data:
        e.data["slug"] = str(e.data["title"]).strip().lower().replace(" ", "-")
    return await e.next()


pb.start(host="127.0.0.1", port=8090)
```

### Access records and current user in custom code

Use repository-style helpers in routes and hooks:

```python
from ppbase import pb

@pb.get("/api/me")
async def me(auth: dict = pb.require_record_auth()):
    user = await pb.records(auth["collectionName"]).get(auth["id"])
    return {"user": user}


@pb.on_record_update_request("users")
async def before_user_update(e):
    e.require_auth_record()  # raises 401/403 with PocketBase-style body
    e.require_same_auth_record(e.record_id or "")
    if not e.is_superuser():
        e.data.setdefault("updatedByHook", True)
    current = await e.get_current_user(fields="id,email")
    if current:
        e.data.setdefault("updatedBy", current["id"])  # mutate payload before default handler
    return await e.next()
```

### Multi-file usage

- Side-effects style: import modules that register decorators on `pb`.
- Register function style: expose `register(pb)` and call it manually.

```python
from ppbase import pb
import my_hooks_side_effects
from my_hooks_register import register

register(pb)
pb.start()
```

### CLI hooks loading

You can load hook modules when starting the server:

```bash
python -m ppbase serve --hooks myapp.hooks:register
python -m ppbase serve --hooks myapp.hooks:register --hooks myapp.more_hooks:setup
```

The hook target format is strict: `module:function`. The function receives `pb`.

## Tech Stack

- **FastAPI** -- async web framework
- **SQLAlchemy 2.0** -- async ORM + Core
- **asyncpg** -- PostgreSQL async driver
- **Pydantic** -- request/response validation
- **PyJWT** -- admin authentication tokens
- **Lark** -- PocketBase filter syntax parser
- **PostgreSQL 17** -- database (via Docker)

## License

MIT
