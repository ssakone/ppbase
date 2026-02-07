# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is PPBase?

PPBase is a Python reimplementation of [PocketBase](https://pocketbase.io/) using PostgreSQL instead of SQLite. It provides dynamic collections, auto-generated REST APIs, admin auth with a built-in admin UI, and aims for PocketBase API compatibility (~92%).

## Commands

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate       # bash/zsh
source .venv/bin/activate.fish  # fish
pip install -e ".[dev]"

# PostgreSQL (Docker)
python -m ppbase db start       # create & start container (port 5433)
python -m ppbase db stop        # stop container
python -m ppbase db restart     # restart container
python -m ppbase db status      # check status

# Run server
python -m ppbase serve                    # foreground
python -m ppbase serve -d --port 8090     # daemon (background)
python -m ppbase restart --port 8090      # restart daemon
python -m ppbase stop                     # stop daemon
python -m ppbase status                   # check if running

# Shell script alternative
./ppctl.sh start | stop | restart | status
./ppctl.sh db-start | db-stop | db-restart | db-status

# Create admin
python -m ppbase create-admin --email test@example.com --password 123456

# Verify app boots
python -c "from ppbase.app import create_app; app = create_app(); print('OK')"
```

All settings are configurable via `PPBASE_` env prefix (e.g., `PPBASE_DATABASE_URL`, `PPBASE_PORT`).
Default DB URL: `postgresql+asyncpg://ppbase:ppbase@localhost:5433/ppbase`

## Architecture

PPBase uses a **hybrid SQLAlchemy** approach: ORM for system tables (fixed schema), SQLAlchemy Core for dynamic collection tables (created/altered at runtime).

### Request flow

```
HTTP Request → FastAPI (app.py)
  → api/router.py → api/{endpoint}.py
    → api/deps.py (auth, session, collection resolution)
    → services/{service}.py (business logic)
      → db/engine.py (async sessions)
      → db/system_tables.py (ORM queries for _collections, _admins, _params)
      → db/schema_manager.py (DDL for dynamic tables)
      → sqlalchemy.text() (parameterized SQL for dynamic table queries)
```

### Key design decisions

- **Dynamic tables use physical PostgreSQL columns** (not JSONB), mapped from 14 PocketBase field types. Mapping lives in `db/schema_manager.py`.
- **No migration system** — dynamic collection tables are managed via direct DDL (CREATE/ALTER/DROP TABLE) in `schema_manager.py`. The `_collections` table is the source of truth. System tables use SQLAlchemy `create_all()` at startup.
- **Filter parser** (`services/filter_parser.py`) uses a Lark EBNF grammar to translate PocketBase filter syntax (`title~"test" && views>5`) to parameterized SQL. Never injects values directly.
- **Records router** is mounted at the app root (not under `/api` prefix) because its paths are `/api/collections/{coll}/records/*` — see `app.py:73`.
- **Collections and records routers** are imported with try/except in `api/router.py` for graceful degradation.
- **bcrypt is used directly** (not passlib) due to passlib/bcrypt 5.x incompatibility. See `services/auth_service.py`.
- **Error responses** use custom exception handlers in `app.py` to produce PocketBase-format `{status, message, data}` instead of FastAPI's `{detail}`.
- **Field schema format** — API accepts both flat (PocketBase v0.23+) and nested formats. `models/collection.py` normalizes flat→nested (`options` dict) on both `schema` and `fields` keys. `record_service.py` also normalizes at read time for safety.

### System tables (ORM in `db/system_tables.py`)

- `_collections` — collection definitions (schema as JSONB, rules as nullable TEXT)
- `_admins` — admin accounts (email, password_hash, token_key)
- `_params` — key-value settings store
- `_external_auths` — OAuth2 external auth records

### API rules convention

- `NULL` rule = admin-only access
- `""` (empty string) = public access
- `"expression"` = evaluate as filter (adds WHERE clause)

### Pydantic "schema" field

Collection models use a field named `schema` which shadows `BaseModel.schema()`. This is handled via `protected_namespaces=()` in model configs and warning suppression. Both `schema` and `fields` are accepted in API requests.

## Admin UI

The admin UI is a React + TypeScript SPA built with Vite, served at `/_/`. Source lives in `admin-ui/`, build output goes to `ppbase/admin/dist/`.

### Build & Dev

```bash
# Install dependencies
cd admin-ui && npm install

# Development (with HMR, proxies API to localhost:8090)
npm run dev

# Production build (outputs to ppbase/admin/dist/)
npm run build
```

### Tech stack

- **React 18** + **TypeScript** + **Vite**
- **shadcn/ui** (Radix primitives + Tailwind CSS)
- **TanStack Query** (server state, cache, mutations)
- **React Router v7** (basename `/_/`)
- **Sonner** (toast notifications)
- **Inter font** (self-hosted, no CDN)

### Structure

```
admin-ui/src/
├── api/              # Fetch client + typed endpoints (auth, collections, records, migrations, settings)
├── hooks/            # TanStack Query hooks (use-collections, use-records, etc.)
├── context/          # Auth + Sidebar React contexts
├── routes/           # Pages (login, collections, records, migrations, settings)
├── components/
│   ├── ui/           # shadcn/ui primitives (button, input, dialog, sheet, table, etc.)
│   ├── fields/       # Per-type record field inputs (text, number, bool, select, relation, etc.)
│   ├── sql-editor/   # SQL editor with syntax highlighting and autocomplete
│   └── *.tsx         # Feature components (collection-editor, record-editor, etc.)
├── lib/              # Utilities (cn, formatDate, field-types config, format-cell)
└── styles/globals.css
```

### Serving

`ppbase/app.py` mounts `/_/assets` → `ppbase/admin/dist/` (StaticFiles) and returns `index.html` for all `/_/*` routes (SPA catch-all). No changes needed to app.py for the React build.

## Reference docs

Detailed specs live in `project_docs/` (~10K lines):
- `03_api_specification.md` — all 42 PocketBase endpoints with request/response formats
- `04_data_models_and_db.md` — field types, Go source analysis
- `05_python_implementation_strategy.md` — tech stack rationale, DB schema DDL, Lark grammar
- `07_comparison_report.md` — side-by-side PPBase vs PocketBase response comparison

## Current status (v0.1.0 — Phase 1+)

**Working:** Admin CRUD+auth, Collection CRUD (dynamic DDL), Record CRUD, filter/sort/pagination, field validation (14 types), expand relations, PocketBase-compatible error format, admin UI with full collection/record management, CLI with server & DB management commands.

**Not yet implemented (Phase 2):** Auth collection user registration/login, OAuth2, SSE realtime (LISTEN/NOTIFY), hooks, S3 storage, image thumbnails.
