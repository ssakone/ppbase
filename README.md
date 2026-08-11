# PPBase

A Python reimplementation of [PocketBase](https://pocketbase.io/) backed by PostgreSQL.

PPBase gives you an instant REST API with dynamic collections, admin authentication, a built-in admin dashboard, and PocketBase-compatible endpoints -- all running on PostgreSQL instead of SQLite.

## Quick Start

### 1. Prerequisites

- Python 3.11+
- A reachable PostgreSQL 16 or 17 server
- Docker is optional and used only by the bundled local PostgreSQL helper and tests
- Node.js 18+ only when building the Admin UI from a source clone

### 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install ppbase
```

For framework development from a source clone:

```bash
git clone <repo-url> ppbase && cd ppbase
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Backup and restore are native — they use the same asyncpg driver as the server
and need no external PostgreSQL client binaries — so they work identically from
an installed package and an editable source checkout.

When running directly from a source clone, build the Admin UI once after clone
and again after frontend changes so `/_/` serves the current Dashboard:

```bash
cd admin-ui
npm ci
npm run build
cd ..
```

### 3. Connect PostgreSQL

```bash
python -m ppbase db start
```

This optional convenience command creates a Docker container (`ppbase-pg`)
running PostgreSQL 17 on port 5433. PPBase itself, including native
backup/restore, does not depend on Docker; set `PPBASE_DATABASE_URL` to use an
existing PostgreSQL server instead.

Native backup and destructive restore both use `PPBASE_DATABASE_URL`. Restore
verifies the connected database before replacement and requires the running
`serve` process to restart. See
[Native Backup & Restore](docs/native-backup-restore.md) for details.

The installed `ppbase` console command and `python -m ppbase` are equivalent.

### 4. Start the server

```bash
python -m ppbase serve
```

The server starts at **http://localhost:8090**. Admin UI is at **http://localhost:8090/_/**.
PPBase creates the PocketBase-compatible `pb_data/backups` archive directory
automatically. No separate backup-control directory is required.

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

# Check live backup/restore readiness
ppbase backup doctor --db "$PPBASE_DATABASE_URL" --dir "$PPBASE_DATA_DIR" --server http://127.0.0.1:8090

# Admin
python -m ppbase create-admin --email <email> --password <pass>

# Application migrations
python -m ppbase migrate status
python -m ppbase migrate up
python -m ppbase migrate down 1
python -m ppbase migrate create add_posts
python -m ppbase migrate snapshot
```

Backup and restore use the same `PPBASE_DATABASE_URL` connection as `serve`.
`backup doctor` reports `backupReady` and `restoreReady` separately, and its
exit code tracks backup creation readiness so a restore-only database or
restart blocker does not disable backups.

The `--db` value used by doctor and its `--dir` compatibility override must
match the database and `data_dir` used by `serve`. If PostgreSQL
references local files absent from `<data_dir>/storage`,
backup creation fails safely instead of publishing an incomplete archive.
Restore is a destructive in-place operation: after fully verifying the archive,
PPBase blocks writes, replaces the active database and local file storage with
the backup's contents, and restarts. Startup recovery then finalizes the file
inventory before applying any newer migrations. It does not merge records or
files. The restored archive also replaces `_superusers` and the project-local
JWT secret: target-only admins disappear, source admins return, and operators
should sign in again with a superuser contained in the archive. The selected
ZIP remains available under `pb_data/backups`. During a restore, PPBase keeps a
small temporary journal directly in `pb_data` and locks the existing runtime
directories; the journal is removed after recovery completes. A pre-commit
failure restores the previous files and rolls back the database transaction;
after commit, PPBase stays fenced until startup recovery has verified and
finalized the matching files.
See [Native Backup & Restore](docs/native-backup-restore.md).

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
- **PocketBase-style migrations**: native Python files update collection metadata
  and PostgreSQL DDL together
- **Atomic PostgreSQL execution**: one transaction per migration file, tracked in
  `_migrations`, with an advisory lock shared by runners and Dashboard producers

## Application-owned migrations

PPBase is the migration engine. Each application using the PyPI package owns
and versions its configured migration directory (usually `pb_migrations/`).
Those files are applied after PPBase bootstraps its internal schema and the
stable `users` collection, and before HTTP traffic, realtime listeners, or
serve hooks start.

The `pb_migrations/` directory at the root of the PPBase source repository is
intentionally ignored because it is only local framework-development runtime
data. This does **not** mean consumer projects should ignore their own
migrations; application migrations belong in the consumer project's VCS.

PostgreSQL DDL, collection metadata, and migration history are atomic inside
each file. The migration filesystem is a separate durability domain: generated
files are published as durable intent before the database commit, under the
same advisory lock. A crash may therefore leave a pending file that the next
runner applies. After a COMMIT error PPBase reacquires the runner lock before
checking `_migrations` or deleting rolled-back intent, and preserves the file
when the outcome cannot be proven; it does not claim a distributed PostgreSQL +
filesystem transaction. Schema mutations take the lock even when Dashboard
migration-file generation is disabled.

`migrate snapshot` follows PocketBase's extend-mode idea: it updates existing
collections, creates missing ones, and orders dependent views. Its `down()` is
intentionally a safe no-op because the pre-snapshot state is unknowable; a
schema snapshot is not a database/data backup. `migrate status` is read-only and
does not bootstrap a blank database.

## Configuration

All settings use the `PPBASE_` environment variable prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `PPBASE_DATABASE_URL` | `postgresql+asyncpg://ppbase:ppbase@localhost:5433/ppbase` | PostgreSQL connection |
| `PPBASE_PORT` | `8090` | Server port |
| `PPBASE_HOST` | `0.0.0.0` | Bind address |
| `PPBASE_DATA_DIR` | `./pb_data` | Local files, project secret and backup ZIPs under `backups/` |
| `PPBASE_MIGRATIONS_DIR` | `./pb_migrations` | Consumer application's migration directory |
| `PPBASE_AUTO_MIGRATE` | `true` | Legacy fallback for startup application and file generation |
| `PPBASE_APPLY_MIGRATIONS_ON_START` | inherits legacy setting | Apply pending files before serving traffic |
| `PPBASE_GENERATE_MIGRATIONS` | inherits legacy setting | Generate files after Dashboard collection changes |
| `PPBASE_MIGRATION_LOCK_TIMEOUT` | `30` | Seconds to wait for another instance's migration lock |
| `PPBASE_BACKUP_MAX_UPLOAD_BYTES` | `21474836480` | Maximum native backup ZIP upload size (20 GiB) |

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
- **PostgreSQL** -- database; Docker is optional for local development

## License

MIT
