# Getting Started

## Prerequisites

- Python 3.11+
- A reachable PostgreSQL 16 or 17 server
- Docker only if using the optional managed local PostgreSQL container
- Node.js 18+ (only if rebuilding the Admin UI)

## Installation

```bash
python -m venv .venv
source .venv/bin/activate        # bash / zsh
source .venv/bin/activate.fish   # fish
pip install ppbase
```

For framework development from a source checkout:

```bash
git clone https://github.com/yourname/ppbase.git
cd ppbase
python -m venv .venv
source .venv/bin/activate

# Install package + dev extras
pip install -e ".[dev]"
```

Native backup/restore needs no external PostgreSQL client binaries: it uses the
same asyncpg driver as the server, so it works identically from an editable
source checkout and an installed package.

For an operational source-clone deployment rather than framework development,
follow the [Deployment from a source clone runbook](./deployment-from-clone.md).

## Start the database

PPBase ships with an optional `db` CLI sub-command that manages a local Docker
container running PostgreSQL 17 on port **5433**:

```bash
python -m ppbase db start    # create & start (first run pulls the image)
python -m ppbase db status   # check if running
python -m ppbase db stop     # stop
python -m ppbase db restart  # restart
```

> **Custom PostgreSQL?** Set `PPBASE_DATABASE_URL` to your connection string and skip this step.

Docker is not required by PPBase itself or by native backup/restore. Configure
`PPBASE_DATABASE_URL`, then start PPBase normally.

## Create your first admin

```bash
ppbase create-admin --email admin@example.com --password secret123
```

## Run the server

```bash
# Foreground (dev)
ppbase serve

# Foreground with custom public + migrations directories
python -m ppbase serve --publicDir ./public --migrationsDir ./pb_migrations

# Custom data directory (PocketBase-compatible flag name)
python -m ppbase serve --dir ./pb_data_myproject

# Load hook modules from CLI (repeatable)
python -m ppbase serve --hooks hooks.users:setup --hooks routes.blog:setup

# Background daemon
python -m ppbase serve -d --port 8090

# Stop daemon
python -m ppbase stop

# Check status
python -m ppbase status
```

Or using the shell helper:

```bash
./ppctl.sh start
./ppctl.sh stop
./ppctl.sh status
```

Open **http://127.0.0.1:8090/_/** in your browser to access the Admin UI.

The first `serve` automatically creates `pb_data/backups`. No separate control
directory or preparatory filesystem command is required for the defaults.

When deploying directly from a source clone, build the assets once after clone
and rebuild them after Admin UI changes before restarting PPBase:

```bash
cd admin-ui
npm ci
npm run build
```

Installed packages include their packaged Admin UI assets.

### SMTP test (admin)

After configuring SMTP in Admin UI (`Settings`), you can validate delivery with:

```http
POST /api/settings/test/email
```

Body example:

```json
{
  "email": "you@example.com",
  "template": "verification",
  "collection": "users"
}
```

A successful test returns `204 No Content`.

### First startup setup URL

If no admin exists, PPBase prints a one-time setup URL:

```text
No admin account found. Create your first admin:
http://127.0.0.1:<port>/_/setup?token=...
```

The URL uses the effective runtime host/port (including `pb.start(host=..., port=...)` overrides).

### JWT secret behavior

If `PPBASE_JWT_SECRET` is not set, PPBase auto-generates a strong secret and persists it to:

```text
<data_dir>/.jwt_secret
```

This keeps issued tokens stable across restarts.

## Your first extension

Create `my_app.py` next to `ppbase/`:

```python
from ppbase import pb

@pb.get("/hello")
async def hello():
    return {"message": "Hello World!", "ok": True}

if __name__ == "__main__":
    pb.start(host="127.0.0.1", port=8090)
```

Run it:

```bash
python my_app.py
curl http://127.0.0.1:8090/hello
# {"message":"Hello World!","ok":true}
```

## Project layout (recommended)

```
my_project/
├── main.py            ← entry-point
├── pb_migrations/     ← this application's versioned migrations
├── hooks/
│   ├── users.py       ← user-related hooks
│   ├── posts.py       ← post hooks
│   └── audit.py       ← cross-cutting audit log
├── routes/
│   ├── blog.py        ← blog API routes
│   └── metrics.py     ← internal metrics
└── requirements.txt   ← includes the pip-installed ppbase package
```

```python
# main.py
from ppbase import pb

# load modules — each receives the `pb` facade and registers its hooks/routes
pb.load_hooks("hooks.users:setup")
pb.load_hooks("hooks.posts:setup")
pb.load_hooks("hooks.audit:setup")
pb.load_hooks("routes.blog:setup")
pb.load_hooks("routes.metrics:setup")

if __name__ == "__main__":
    pb.start()
```

```python
# hooks/users.py
def setup(pb):
    from ppbase.ext.events import RecordRequestEvent

    @pb.on_record_create_request("users")
    async def on_user_create(event: RecordRequestEvent):
        event.data.setdefault("role", "member")
        return await event.next()
```

See [Multi-file Projects](./multi-file.md) for the full pattern.

## Runtime configuration with `pb.configure()`

Override any setting after importing `pb` but before calling `pb.start()` or `pb.get_app()`. Useful for reading CLI arguments or environment-specific values:

```python
import sys
from ppbase import pb

# Override database and migration directory from CLI
pb.configure(
    database_url="postgresql+asyncpg://myuser:mypass@localhost/mydb",
    data_dir="./pb_data_myproject",
    migrations_dir="./migrations",
    public_dir="./public_site",   # served at /
    # Optional S3/R2 backend
    storage_backend="s3",
    s3_endpoint="https://<accountid>.r2.cloudflarestorage.com",
    s3_bucket="my-bucket",
    s3_region="auto",
    s3_access_key="...",
    s3_secret_key="...",
)

if __name__ == "__main__":
    pb.start(host="0.0.0.0", port=8090)
```

`pb.configure()` raises `RuntimeError` if called after the app is materialised.

## Migrations

PPBase bootstraps its internal schema and the default `users` collection, then
applies pending Python files from `migrations_dir` before it starts accepting
traffic. Each file is committed independently with its `_migrations` history
row, and PostgreSQL advisory locking prevents duplicate application when two
instances start together. Dashboard/API producers acquire the same lock before
publishing a generated migration and committing their schema change.

```bash
python -m ppbase migrate create add_posts
python -m ppbase migrate status
python -m ppbase migrate up
python -m ppbase migrate down 1
python -m ppbase migrate snapshot
```

`migrate status` is read-only. On a blank database it reports every local file
as pending without creating PPBase tables or the default users collection.

Snapshots replay in PocketBase-style extend mode and topologically order
dependent PostgreSQL views. Because PPBase records a snapshot as an already
applied baseline on its source database, the generated `down()` intentionally
does nothing: it cannot know which collections or data existed before the
snapshot. Use PostgreSQL backups for data recovery.

The migration directory belongs to the consuming application. Commit your
backend's `pb_migrations/` (or custom directory) to that backend's repository
so deployments receive and apply the same history. PPBase's own source checkout
ignores its root `pb_migrations/` because that directory is only local runtime
data for framework development; it is not bundled in the PyPI package.

PocketBase uses “automigrate” for migration-file generation after Dashboard
collection changes. PPBase now exposes the two concerns separately:

- `apply_migrations_on_start`: apply pending files during startup;
- `generate_migrations`: generate files for Dashboard/API collection changes.

For backwards compatibility, either unset option inherits the legacy
`auto_migrate` value.

Generated files and PostgreSQL cannot share one physical transaction. PPBase
publishes the file first as durable intent while holding the shared migration
lock, then commits DDL, metadata, and history together. After a crash, an
uncommitted file can remain pending and be applied on the next run. A lost
COMMIT acknowledgement is checked against `_migrations` only after reacquiring
that same runner lock; unverifiable outcomes preserve the file and require
`migrate status` before retrying. Disabling `generate_migrations` suppresses
only the file: physical collection-schema mutations still take the lock.

## Verify the app boots

```bash
python -c "from ppbase.app import create_app; app = create_app(); print('OK')"
```

For production rollout guidance, see [Production Deployment](./production.md)
and the [Deployment from a source clone runbook](./deployment-from-clone.md).
