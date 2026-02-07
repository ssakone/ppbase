# PPBase: Python Implementation Strategy

## A PocketBase-Compatible Backend-as-a-Service Built on Python + PostgreSQL

**Document Version:** 1.0
**Date:** 2026-02-06
**Status:** Architecture Design

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Python Tech Stack Selection](#2-python-tech-stack-selection)
3. [Project Structure](#3-project-structure)
4. [Key Design Decisions](#4-key-design-decisions)
5. [Database Schema Design for PostgreSQL](#5-database-schema-design-for-postgresql)
6. [Phase 1 Scope (CRUD + Admin)](#6-phase-1-scope-crud--admin)
7. [Implementation Roadmap](#7-implementation-roadmap)

---

## 1. Executive Summary

PPBase is a Python reimplementation of [PocketBase](https://pocketbase.io/) that replaces SQLite with PostgreSQL. The goal is to provide the same developer-friendly BaaS (Backend-as-a-Service) experience -- dynamic collections, auto-generated REST APIs, built-in auth, realtime subscriptions, and an admin UI -- while unlocking the scalability, concurrency, and feature richness of PostgreSQL.

### Why PPBase Exists

PocketBase is excellent for prototyping and small deployments, but it is locked to SQLite and Go. PPBase targets teams that want:

- **PostgreSQL** for production workloads (JSONB, full-text search, concurrent writes, row-level security, extensions)
- **Python ecosystem** for extensibility (data science, ML pipelines, Django/FastAPI familiarity)
- **Horizontal scalability** that SQLite cannot provide
- **Library-mode embedding** (`from ppbase import PPBase`) alongside standalone server mode

### Design Principles

1. **PocketBase API compatibility** -- clients written for PocketBase should work against PPBase with minimal changes
2. **PostgreSQL-native** -- leverage JSONB, GIN indexes, array types, full-text search, `LISTEN/NOTIFY`, and extensions rather than fighting the database
3. **Async-first** -- fully asynchronous from the HTTP layer through to database I/O
4. **Embeddable** -- usable as both a standalone server and an importable Python library
5. **Incrementally adoptable** -- start with core CRUD, layer on auth, realtime, and hooks over time

---

## 2. Python Tech Stack Selection

### 2.1 Web Framework: FastAPI

**Decision: FastAPI (built on Starlette + Uvicorn)**

| Criterion | FastAPI | Flask | Starlette (raw) |
|---|---|---|---|
| Async native | Yes | No (requires extensions) | Yes |
| OpenAPI/Swagger auto-generation | Built-in | Manual (flask-smorest) | Manual |
| Request validation | Pydantic v2 integrated | Manual | Manual |
| SSE support | Via sse-starlette | Via flask-sse | Native |
| WebSocket support | Built-in | Via flask-sock | Built-in |
| Dependency injection | Built-in | Manual | Manual |
| Ecosystem maturity | Very high (2025+) | Very high | Moderate |
| Performance | Near-Go/Node speeds | Moderate | Near-Go/Node speeds |

**Rationale:**

- FastAPI provides automatic OpenAPI schema generation, which aligns with PocketBase's self-documenting API approach.
- Native async/await means every database call, file operation, and SSE stream is non-blocking.
- Pydantic v2 integration eliminates the need for a separate validation layer.
- Starlette's `StaticFiles` mount allows serving the admin SPA directly from the same process.
- FastAPI's dependency injection system maps cleanly to PPBase's need for per-request auth context, database sessions, and collection resolution.

**Key dependencies:**

```
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
starlette>=0.41.0
```

### 2.2 ORM / Database Layer: SQLAlchemy 2.0 (Async) + asyncpg

**Decision: SQLAlchemy 2.0 async with asyncpg driver**

PPBase uses a **hybrid approach**: SQLAlchemy Core (not ORM) for dynamic collection tables, and SQLAlchemy ORM for system tables only.

**Why this split:**

- **System tables** (`_collections`, `_admins`, `_params`, `_external_auths`) have fixed, known schemas -- ORM models are appropriate and give us type safety, relationship loading, and migration support.
- **Dynamic collection tables** are created/altered at runtime based on user-defined schemas. ORM models cannot be statically defined for these. SQLAlchemy Core's `Table`, `MetaData`, `select()`, `insert()`, `update()`, and `delete()` constructs work perfectly for dynamic SQL generation without requiring a mapped class.

**asyncpg advantages:**

- Written in C/Cython, asyncpg is the fastest PostgreSQL driver for Python
- Native support for PostgreSQL types: JSONB, arrays, enums, composite types, ranges
- Built-in connection pooling (`asyncpg.create_pool()`) eliminates the need for PgBouncer in many deployments
- Prepared statement caching for repeated query patterns

**Connection pool configuration (production defaults):**

```python
pool = await asyncpg.create_pool(
    dsn=database_url,
    min_size=5,
    max_size=20,
    max_inactive_connection_lifetime=300.0,
    command_timeout=60.0,
)
```

**Key dependencies:**

```
sqlalchemy[asyncio]>=2.0.36
asyncpg>=0.30.0
```

### 2.3 Authentication: PyJWT + passlib

**Decision: PyJWT for token management, passlib[bcrypt] for password hashing**

| Component | Library | Purpose |
|---|---|---|
| JWT creation/verification | PyJWT >= 2.9.0 | HS256/RS256 token signing, claims validation |
| Password hashing | passlib[bcrypt] >= 1.7.4 | bcrypt hashing with configurable rounds |
| TOTP/2FA (Phase 2) | pyotp >= 2.9.0 | Time-based one-time passwords |
| OAuth2 (Phase 2) | authlib >= 1.3.0 | OAuth2 client for social login |

**Token strategy (PocketBase-compatible):**

PPBase issues two types of JWTs:

1. **Admin tokens** -- issued to admin users, grant full API access
2. **Record tokens** -- issued to auth collection records, scoped to that collection's API rules

Both use HS256 with a per-instance secret. Tokens contain:

```json
{
  "id": "record_or_admin_id",
  "type": "admin" | "authRecord",
  "collectionId": "collection_id (for authRecord only)",
  "exp": 1234567890,
  "iat": 1234567890
}
```

**Password hashing configuration:**

```python
from passlib.context import CryptContext

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,
)
```

### 2.4 File Storage: Local Filesystem + S3-Compatible

**Decision: Pluggable storage backend with local and S3 adapters**

```python
# Storage interface
class StorageBackend(Protocol):
    async def upload(self, key: str, data: bytes, content_type: str) -> None: ...
    async def download(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
    async def get_url(self, key: str, expires: int = 3600) -> str: ...

# Implementations
class LocalStorage(StorageBackend):
    """Stores files on the local filesystem under a configurable base path."""

class S3Storage(StorageBackend):
    """S3-compatible storage using aiobotocore (async boto3)."""
```

**File path convention (PocketBase-compatible):**

```
{collection_id}/{record_id}/{filename}
```

**Key dependencies:**

```
aiobotocore>=2.15.0   # Async S3 client (wraps botocore)
aiofiles>=24.1.0      # Async local file I/O
Pillow>=11.0.0        # Image thumbnail generation
```

### 2.5 Migration Tool: Alembic (System Tables Only)

**Decision: Alembic for system table migrations; custom migration engine for dynamic collection tables**

This is a critical architectural distinction:

- **System tables** (`_collections`, `_admins`, etc.) are managed by Alembic with autogenerate. These have fixed schemas that change only between PPBase versions.
- **Dynamic collection tables** are NOT managed by Alembic. They are created, altered, and dropped by PPBase's own `SchemaManager` service, which translates collection schema definitions into DDL statements (`CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`).

**Why not Alembic for dynamic tables:**

Alembic's autogenerate compares the SQLAlchemy MetaData to the database at code-writing time. Dynamic tables do not exist in the MetaData until runtime, making autogenerate impossible. Additionally, PocketBase-style schema changes (add field, rename field, change field type) need custom logic for data migration (e.g., converting a text field to a number field requires casting existing data).

**PPBase's own migration system for collections:**

```python
class SchemaManager:
    """Translates collection schema changes into PostgreSQL DDL."""

    async def create_collection_table(self, collection: Collection) -> None:
        """CREATE TABLE with columns derived from collection.schema fields."""

    async def update_collection_table(
        self, old: Collection, new: Collection
    ) -> None:
        """ALTER TABLE: add/drop/rename/retype columns, update indexes."""

    async def drop_collection_table(self, collection: Collection) -> None:
        """DROP TABLE CASCADE."""

    async def sync_indexes(self, collection: Collection) -> None:
        """CREATE/DROP INDEX based on collection.indexes definition."""
```

**Key dependencies:**

```
alembic>=1.14.0
```

### 2.6 Admin UI: Embedded SPA

**Decision: Pre-built SPA (Svelte or React) served as static files from FastAPI**

The admin UI is a single-page application that is:

1. **Built at release time** into static HTML/JS/CSS assets
2. **Bundled inside the Python package** under `ppbase/admin/dist/`
3. **Served by FastAPI** using Starlette's `StaticFiles` mount with `html=True` for SPA routing

```python
from fastapi.staticfiles import StaticFiles

# Mount admin SPA -- must come AFTER API routes
app.mount(
    "/_/",
    StaticFiles(
        directory=Path(__file__).parent / "admin" / "dist",
        html=True,
    ),
    name="admin",
)
```

**SPA technology choice:**

Svelte (via SvelteKit) is the recommended choice to maintain compatibility with PocketBase's existing admin UI, which is also built with Svelte. This would allow potential reuse of PocketBase's open-source admin UI components. However, React or Vue are equally viable if the team prefers them.

**Build integration:**

```
ppbase/
  admin/
    ui/               # SPA source code (Svelte/React/Vue)
    dist/             # Built static files (committed or built in CI)
    package.json
```

The `dist/` directory is included in the Python package distribution (`pyproject.toml` includes it via `package-data`).

### 2.7 Validation: Pydantic v2

**Decision: Pydantic v2 for all request/response validation and schema enforcement**

Pydantic v2 (with its Rust-powered core) provides:

- Request body validation via FastAPI integration
- Dynamic model generation for collection-specific validation
- Serialization/deserialization with JSON Schema output
- Custom validators for PocketBase field types

**Dynamic Pydantic model generation for collections:**

```python
from pydantic import create_model
from typing import Optional

def build_record_validator(collection: Collection) -> type:
    """Generate a Pydantic model from a collection's field definitions."""
    fields = {}
    for field in collection.schema:
        python_type = FIELD_TYPE_MAP[field.type]
        if field.required:
            fields[field.name] = (python_type, ...)
        else:
            fields[field.name] = (Optional[python_type], field.default)

    return create_model(
        f"{collection.name}Record",
        **fields,
    )
```

**Key dependencies:**

```
pydantic>=2.10.0
```

### 2.8 Realtime: Server-Sent Events (SSE)

**Decision: SSE via sse-starlette (Phase 2, but architecture prepared from day one)**

PocketBase uses SSE for realtime subscriptions. PPBase follows the same approach:

- Clients open an SSE connection to `GET /api/realtime`
- Server pushes events when records are created, updated, or deleted
- PostgreSQL's `LISTEN/NOTIFY` mechanism is used to propagate changes across multiple PPBase instances

**Architecture:**

```
Client (SSE) <---> FastAPI SSE endpoint <---> EventBroker <---> PostgreSQL LISTEN/NOTIFY
                                                  |
                                            In-memory pub/sub
                                            (for single-instance)
```

**Key dependencies (Phase 2):**

```
sse-starlette>=2.2.0
```

### 2.9 Full Dependency Summary

```toml
# pyproject.toml [project.dependencies]
dependencies = [
    # Web framework
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",

    # Database
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30.0",
    "alembic>=1.14.0",

    # Validation
    "pydantic>=2.10.0",

    # Auth
    "PyJWT>=2.9.0",
    "passlib[bcrypt]>=1.7.4",

    # File handling
    "aiofiles>=24.1.0",
    "Pillow>=11.0.0",

    # Filter expression parsing
    "lark>=1.2.2",

    # Utilities
    "python-multipart>=0.0.17",  # Form/file uploads
    "python-dotenv>=1.0.1",      # .env file support
    "click>=8.1.0",              # CLI commands
    "rich>=13.9.0",              # CLI output formatting
]

[project.optional-dependencies]
s3 = ["aiobotocore>=2.15.0"]
realtime = ["sse-starlette>=2.2.0"]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "httpx>=0.27.0",  # Async test client
    "testcontainers[postgres]>=4.8.0",  # PostgreSQL in Docker for tests
    "ruff>=0.8.0",
    "mypy>=1.13.0",
]
```

---

## 3. Project Structure

```
ppbase/
├── __init__.py                 # PPBase class, version, public API
├── __main__.py                 # `python -m ppbase` entry point
├── cli.py                      # Click CLI: serve, migrate, create-admin
├── app.py                      # FastAPI app factory
├── config.py                   # Settings (Pydantic BaseSettings)
├── constants.py                # System constants, field type enums
│
├── core/                       # Core application primitives
│   ├── __init__.py
│   ├── base.py                 # PPBase main class (library entry point)
│   ├── events.py               # Event bus (before/after hooks)
│   └── exceptions.py           # Custom exception hierarchy
│
├── db/                         # Database layer
│   ├── __init__.py
│   ├── engine.py               # Async engine + connection pool setup
│   ├── session.py              # Async session factory + dependency
│   ├── schema_manager.py       # Dynamic DDL: CREATE/ALTER/DROP TABLE
│   ├── query_builder.py        # Dynamic SELECT/INSERT/UPDATE/DELETE
│   ├── migrations/             # Alembic migrations (system tables only)
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/
│   └── seed.py                 # Initial data seeding
│
├── models/                     # SQLAlchemy ORM models (system tables)
│   ├── __init__.py
│   ├── collection.py           # _collections table model
│   ├── admin.py                # _admins table model
│   ├── param.py                # _params table model (KV settings)
│   └── external_auth.py        # _external_auths table model
│
├── schema/                     # Collection field types + validation
│   ├── __init__.py
│   ├── field_types.py          # TextField, NumberField, BoolField, etc.
│   ├── field_registry.py       # Registry: type string -> Field class
│   ├── validators.py           # Field-level validation logic
│   └── collection_schema.py    # Collection schema Pydantic models
│
├── api/                        # REST API routes
│   ├── __init__.py
│   ├── router.py               # Root API router aggregation
│   ├── records.py              # /api/collections/{name}/records CRUD
│   ├── collections.py          # /api/collections CRUD (admin only)
│   ├── admins.py               # /api/admins CRUD + auth
│   ├── auth.py                 # Auth endpoints (login, register, etc.)
│   ├── files.py                # /api/files/{collection}/{record}/{file}
│   ├── settings.py             # /api/settings (admin only)
│   ├── health.py               # /api/health
│   └── realtime.py             # /api/realtime (SSE, Phase 2)
│
├── services/                   # Business logic layer
│   ├── __init__.py
│   ├── record_service.py       # Record CRUD + expand + filter
│   ├── collection_service.py   # Collection CRUD + schema sync
│   ├── admin_service.py        # Admin CRUD + auth
│   ├── auth_service.py         # JWT issuance, password verify
│   ├── file_service.py         # File upload/download/delete
│   ├── filter_parser.py        # PocketBase filter -> SQL translator
│   ├── expand_resolver.py      # Relation expansion (nested records)
│   └── settings_service.py     # System settings management
│
├── storage/                    # File storage backends
│   ├── __init__.py
│   ├── base.py                 # StorageBackend protocol
│   ├── local.py                # Local filesystem storage
│   └── s3.py                   # S3-compatible storage
│
├── middleware/                  # ASGI middleware
│   ├── __init__.py
│   ├── auth.py                 # JWT extraction + request context
│   ├── cors.py                 # CORS configuration
│   ├── activity_logger.py      # Request logging
│   └── rate_limiter.py         # Rate limiting (Phase 2)
│
├── admin/                      # Admin UI
│   ├── dist/                   # Built SPA static files
│   │   ├── index.html
│   │   ├── assets/
│   │   └── ...
│   └── ui/                     # SPA source (Svelte/React)
│       ├── src/
│       ├── package.json
│       └── vite.config.ts
│
└── utils/                      # Shared utilities
    ├── __init__.py
    ├── id_generator.py         # 15-char random ID (PocketBase compat)
    ├── datetime.py             # UTC datetime helpers
    ├── crypto.py               # Token secrets, random strings
    ├── pagination.py           # Pagination helpers
    └── types.py                # Shared type aliases
```

### 3.1 Module Responsibilities

**`core/base.py` -- The PPBase Class:**

This is the primary entry point for both library and standalone usage.

```python
class PPBase:
    """
    Main PPBase application.

    Usage as library:
        from ppbase import PPBase
        app = PPBase(database_url="postgresql+asyncpg://...", data_dir="./pb_data")
        fastapi_app = app.get_app()  # Returns the FastAPI instance
        # Or run directly:
        await app.start(host="0.0.0.0", port=8090)

    Usage as standalone:
        $ ppbase serve --http 0.0.0.0:8090 --db postgresql://...
    """

    def __init__(
        self,
        database_url: str = "postgresql+asyncpg://localhost:5433/ppbase",
        data_dir: str = "./pb_data",
        dev: bool = False,
        origins: list[str] | None = None,
    ):
        self.config = Settings(
            database_url=database_url,
            data_dir=data_dir,
            dev=dev,
            origins=origins or ["*"],
        )
        self._app: FastAPI | None = None

    def get_app(self) -> FastAPI:
        """Build and return the FastAPI application instance."""
        if self._app is None:
            self._app = create_app(self.config)
        return self._app

    async def start(self, host: str = "0.0.0.0", port: int = 8090) -> None:
        """Start the server using uvicorn."""
        import uvicorn
        app = self.get_app()
        config = uvicorn.Config(app, host=host, port=port)
        server = uvicorn.Server(config)
        await server.serve()

    def on_before_create(self, collection: str):
        """Decorator to register a before-create hook."""
        def decorator(fn):
            self._event_bus.on(f"before_create:{collection}", fn)
            return fn
        return decorator
```

**`db/schema_manager.py` -- Dynamic DDL Engine:**

Responsible for all dynamic table creation and modification.

```python
class SchemaManager:
    """
    Translates collection schema definitions into PostgreSQL DDL.

    This is the core of PPBase's dynamic collection system. It:
    1. Creates tables when collections are created
    2. Alters tables when collection fields are added/removed/modified
    3. Drops tables when collections are deleted
    4. Manages indexes (including GIN indexes for JSONB, GiST for FTS)
    """

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def create_collection_table(self, collection: CollectionModel) -> None:
        """Generate and execute CREATE TABLE for a collection."""

    async def alter_collection_table(
        self,
        old_collection: CollectionModel,
        new_collection: CollectionModel,
    ) -> None:
        """Generate ALTER TABLE statements for schema changes."""

    async def drop_collection_table(self, name: str) -> None:
        """DROP TABLE IF EXISTS CASCADE."""
```

**`services/filter_parser.py` -- Filter Expression Translator:**

Translates PocketBase filter syntax into SQL WHERE clauses.

```python
class FilterParser:
    """
    Parses PocketBase filter expressions and translates them to
    SQLAlchemy Core WHERE clauses.

    Supports:
        field = "value"
        field != "value"
        field > 100
        field ~ "pattern"     (LIKE)
        field ?~ "pattern"    (ILIKE)
        field ?= "value"      (relation contains)
        expr && expr           (AND)
        expr || expr           (OR)
        (grouped expressions)
        @request.auth.id       (request context)
        @now, @yesterday, etc. (datetime macros)
        nested.relation.field  (dot-notation traversal)
    """
```

---

## 4. Key Design Decisions

### 4.1 Dynamic Collections: The Hybrid Table Strategy

This is the most consequential architectural decision in PPBase.

**PocketBase's approach (SQLite):** Each collection is a physical SQLite table. Fields map to columns. Schema changes issue `ALTER TABLE`.

**PPBase's approach (PostgreSQL): Physical tables with a JSONB escape hatch.**

Each collection gets its own PostgreSQL table with:

- **Fixed system columns** for every table: `id`, `created`, `updated`, plus auth-specific columns for auth collections
- **Typed columns** for standard field types where PostgreSQL has efficient native types
- **A JSONB `_data` column** as an optional overflow for truly dynamic/schemaless use cases

```sql
-- Example: a "posts" collection with title (text), views (number), tags (json)
CREATE TABLE posts (
    id          VARCHAR(15) PRIMARY KEY DEFAULT ppbase_id(),
    created     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title       TEXT NOT NULL DEFAULT '',
    views       INTEGER NOT NULL DEFAULT 0,
    tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
    author      VARCHAR(15) NOT NULL DEFAULT ''   -- relation field
);

-- GIN index for tags field
CREATE INDEX idx_posts_tags ON posts USING GIN (tags);
-- B-tree for sorting/filtering
CREATE INDEX idx_posts_created ON posts (created);
```

**Why physical columns instead of pure JSONB:**

| Aspect | Physical Columns | Pure JSONB |
|---|---|---|
| Query performance | Native B-tree/GiST indexes | GIN indexes, slower for range queries |
| Type safety | PostgreSQL enforces types | Application-layer only |
| Storage efficiency | Columnar, TOAST-compressed | Document overhead per row |
| ALTER TABLE cost | Can be expensive for large tables | No ALTER needed |
| JOIN performance | Native foreign key joins | Application-layer resolution |
| Full-text search | `tsvector` columns + GiST index | GIN on JSONB text extraction |
| NULL handling | PostgreSQL-native | JSON null vs missing key ambiguity |

**Decision: Physical columns for defined schema fields.** The JSONB `_data` column exists only as a safety valve for future "schemaless mode" or for fields that genuinely need unstructured storage.

### 4.2 Collection Schema Storage

Collection definitions (the schema metadata that describes what fields a collection has) are stored in the `_collections` system table using a **JSONB `schema` column**.

```sql
CREATE TABLE _collections (
    id          VARCHAR(15) PRIMARY KEY,
    name        VARCHAR(255) NOT NULL UNIQUE,
    type        VARCHAR(10) NOT NULL DEFAULT 'base',  -- 'base', 'auth', 'view'
    schema      JSONB NOT NULL DEFAULT '[]'::jsonb,
    indexes     JSONB NOT NULL DEFAULT '[]'::jsonb,
    list_rule   TEXT,
    view_rule   TEXT,
    create_rule TEXT,
    update_rule TEXT,
    delete_rule TEXT,
    options     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Why JSONB for schema storage (not separate tables):**

1. **Atomic reads** -- Loading a collection definition is a single row fetch, not a multi-table JOIN
2. **Schema-in-schema simplicity** -- The collection schema is itself semi-structured and evolves across PPBase versions
3. **PocketBase compatibility** -- PocketBase stores schemas as JSON; this maintains API response compatibility
4. **Performance** -- The `_collections` table is small (typically < 100 rows); JSONB overhead is negligible

**Schema JSONB structure (per field):**

```json
{
  "id": "abc123def45",
  "name": "title",
  "type": "text",
  "required": true,
  "presentable": false,
  "system": false,
  "options": {
    "min": null,
    "max": 500,
    "pattern": ""
  }
}
```

### 4.3 Dynamic Records with SQLAlchemy Core

Since dynamic collection tables have schemas determined at runtime, we do NOT create SQLAlchemy ORM model classes for them. Instead, we use SQLAlchemy Core:

```python
from sqlalchemy import Table, MetaData, Column, String, Integer, select, text

class DynamicTableManager:
    """
    Manages SQLAlchemy Table objects for dynamic collection tables.
    Uses a cache to avoid repeated reflection.
    """

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self.metadata = MetaData()
        self._table_cache: dict[str, Table] = {}

    async def get_table(self, collection_name: str) -> Table:
        """
        Get or create a SQLAlchemy Table object for the given collection.
        Reflects from database on first access, then caches.
        """
        if collection_name not in self._table_cache:
            async with self.engine.connect() as conn:
                await conn.run_sync(
                    lambda sync_conn: self.metadata.reflect(
                        bind=sync_conn,
                        only=[collection_name],
                        extend_existing=True,
                    )
                )
            self._table_cache[collection_name] = self.metadata.tables[collection_name]
        return self._table_cache[collection_name]

    def invalidate(self, collection_name: str) -> None:
        """Clear cache when a collection schema changes."""
        self._table_cache.pop(collection_name, None)
        if collection_name in self.metadata.tables:
            self.metadata.remove(self.metadata.tables[collection_name])
```

**Query construction using Core:**

```python
async def find_records(
    self,
    collection: CollectionModel,
    filter_expr: str | None = None,
    sort: str | None = None,
    page: int = 1,
    per_page: int = 30,
    expand: str | None = None,
) -> PaginatedResult:
    table = await self.table_manager.get_table(collection.name)

    query = select(table)

    # Apply filter
    if filter_expr:
        where_clause = self.filter_parser.parse(filter_expr, table, request_context)
        query = query.where(where_clause)

    # Apply sort
    if sort:
        query = self._apply_sort(query, sort, table)

    # Apply pagination
    query = query.offset((page - 1) * per_page).limit(per_page)

    async with self.engine.connect() as conn:
        result = await conn.execute(query)
        rows = result.mappings().all()

    return PaginatedResult(
        items=[dict(row) for row in rows],
        page=page,
        per_page=per_page,
        total_items=total_count,
        total_pages=ceil(total_count / per_page),
    )
```

### 4.4 Library-Mode Embedding

PPBase must work as both a standalone server and an importable library:

**Standalone mode:**

```bash
# Install
pip install ppbase

# Run
ppbase serve --http 0.0.0.0:8090 --db postgresql://user:pass@localhost/ppbase
```

**Library mode:**

```python
from ppbase import PPBase

# Create instance
pb = PPBase(
    database_url="postgresql+asyncpg://user:pass@localhost/myapp",
    data_dir="./storage",
)

# Get the FastAPI app to mount in your own application
from fastapi import FastAPI

my_app = FastAPI()
my_app.mount("/pb", pb.get_app())

# Or use PPBase services directly
async def my_custom_endpoint():
    records = await pb.services.records.find("posts", filter='status="published"')
    return records
```

**Implementation approach:**

The `PPBase` class is a facade that owns:

1. A `Settings` instance (configuration)
2. A database engine (async SQLAlchemy engine)
3. A `ServiceContainer` that lazily initializes all services
4. A `FastAPI` app instance (created on demand)

```python
class PPBase:
    @property
    def services(self) -> ServiceContainer:
        if self._services is None:
            self._services = ServiceContainer(self.engine, self.config)
        return self._services

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self.config.database_url,
                pool_size=self.config.pool_size,
                max_overflow=self.config.max_overflow,
            )
        return self._engine
```

### 4.5 Filter Syntax Translation

PocketBase uses a custom filter syntax that must be translated to SQL. PPBase implements this using the **Lark** parser generator.

**Grammar definition:**

```lark
// ppbase/services/filter_grammar.lark

?start: expression

?expression: or_expr

?or_expr: and_expr ("||" and_expr)*
?and_expr: comparison ("&&" comparison)*

?comparison: operand OPERATOR operand
           | "(" expression ")"

OPERATOR: "=" | "!=" | ">" | ">=" | "<" | "<=" | "~" | "!~" | "?=" | "?!=" | "?>" | "?>=" | "?<" | "?<=" | "?~" | "?!~"

?operand: field_path
        | STRING
        | NUMBER
        | BOOLEAN
        | NULL
        | macro
        | array

field_path: IDENTIFIER ("." IDENTIFIER)*
macro: "@" IDENTIFIER ("." IDENTIFIER)*
array: "[" [operand ("," operand)*] "]"

STRING: "\"" /[^"]*/ "\""
      | "'" /[^']*/ "'"
NUMBER: /[+-]?(\d+\.?\d*|\.\d+)/
BOOLEAN: "true" | "false"
NULL: "null"
IDENTIFIER: /[a-zA-Z_][a-zA-Z0-9_]*/

%ignore /\s+/
```

**Translation examples:**

| PocketBase Filter | Generated SQL |
|---|---|
| `title = "hello"` | `WHERE title = 'hello'` |
| `views > 100 && status = "active"` | `WHERE views > 100 AND status = 'active'` |
| `title ~ "search%"` | `WHERE title LIKE 'search%'` |
| `title ?~ "search%"` | `WHERE title ILIKE 'search%'` |
| `tags ?= "python"` | `WHERE tags @> '"python"'::jsonb` |
| `author.name = "John"` | `JOIN users ON posts.author = users.id WHERE users.name = 'John'` |
| `@request.auth.id != ""` | `WHERE :auth_id != ''` (bound parameter) |
| `created >= @now` | `WHERE created >= NOW()` |

**SQL injection prevention:**

All literal values in filter expressions are converted to bound parameters (`:param_1`, `:param_2`, etc.), never interpolated into SQL strings. The Lark parser rejects any input that does not conform to the grammar, providing a strong first line of defense.

### 4.6 PostgreSQL-Specific Advantages

PPBase leverages PostgreSQL features that are unavailable in SQLite:

**4.6.1 JSONB Fields:**

PocketBase's `json` field type maps to PostgreSQL's `JSONB` column type:

```sql
-- Efficient containment queries
SELECT * FROM products WHERE metadata @> '{"color": "red"}';

-- Path-based extraction
SELECT metadata->>'color' FROM products;

-- GIN indexing for fast JSON queries
CREATE INDEX idx_products_metadata ON products USING GIN (metadata);
```

**4.6.2 Array Fields:**

PostgreSQL native arrays for multi-select and tags:

```sql
-- Native array column instead of JSON array
ALTER TABLE posts ADD COLUMN tags TEXT[] NOT NULL DEFAULT '{}';

-- Array containment
SELECT * FROM posts WHERE tags @> ARRAY['python', 'fastapi'];

-- GIN index on arrays
CREATE INDEX idx_posts_tags ON posts USING GIN (tags);
```

**4.6.3 Full-Text Search:**

Built-in full-text search eliminates the need for external search engines:

```sql
-- Auto-maintained search vector column
ALTER TABLE posts ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))
    ) STORED;

-- GiST index for fast FTS
CREATE INDEX idx_posts_fts ON posts USING GiST (search_vector);

-- Query
SELECT * FROM posts WHERE search_vector @@ plainto_tsquery('english', 'python web');
```

**4.6.4 LISTEN/NOTIFY for Realtime:**

PostgreSQL's pub/sub mechanism for cross-instance realtime event propagation:

```sql
-- When a record changes:
NOTIFY ppbase_changes, '{"collection":"posts","action":"create","record_id":"abc123"}';

-- PPBase instances listen:
LISTEN ppbase_changes;
```

```python
async def listen_for_changes(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.add_listener("ppbase_changes", on_notification)
        # Keep connection alive for notifications
        while True:
            await asyncio.sleep(1)
```

**4.6.5 Row-Level Security (Future):**

PostgreSQL RLS can enforce access rules at the database level:

```sql
-- Enable RLS
ALTER TABLE posts ENABLE ROW LEVEL SECURITY;

-- Policy: users can only see their own posts
CREATE POLICY user_posts ON posts
    FOR SELECT
    USING (author = current_setting('ppbase.current_user_id'));
```

**4.6.6 Concurrent Writes:**

Unlike SQLite's single-writer limitation, PostgreSQL supports fully concurrent reads and writes with MVCC, enabling horizontal scaling of PPBase instances.

**4.6.7 Custom Functions:**

PPBase uses a custom PostgreSQL function for PocketBase-compatible ID generation:

```sql
CREATE OR REPLACE FUNCTION ppbase_id() RETURNS VARCHAR(15) AS $$
DECLARE
    chars TEXT := 'abcdefghijklmnopqrstuvwxyz0123456789';
    result TEXT := '';
    i INTEGER;
BEGIN
    FOR i IN 1..15 LOOP
        result := result || substr(chars, floor(random() * length(chars) + 1)::int, 1);
    END LOOP;
    RETURN result;
END;
$$ LANGUAGE plpgsql;
```

---

## 5. Database Schema Design for PostgreSQL

### 5.1 System Tables

These tables are managed by Alembic and exist in every PPBase instance.

#### `_collections` -- Collection Definitions

```sql
CREATE TABLE _collections (
    id          VARCHAR(15) PRIMARY KEY DEFAULT ppbase_id(),
    name        VARCHAR(255) NOT NULL,
    type        VARCHAR(10) NOT NULL DEFAULT 'base'
                    CHECK (type IN ('base', 'auth', 'view')),
    system      BOOLEAN NOT NULL DEFAULT FALSE,
    schema      JSONB NOT NULL DEFAULT '[]'::jsonb,
    indexes     JSONB NOT NULL DEFAULT '[]'::jsonb,
    list_rule   TEXT,           -- NULL = admin only
    view_rule   TEXT,           -- NULL = admin only
    create_rule TEXT,           -- NULL = admin only
    update_rule TEXT,           -- NULL = admin only
    delete_rule TEXT,           -- NULL = admin only
    options     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_collections_name UNIQUE (name)
);

-- Index for fast lookups by name
CREATE INDEX idx_collections_name ON _collections (name);
```

**Schema JSONB field structure (array of field objects):**

```json
[
  {
    "id": "f1a2b3c4d5e",
    "name": "title",
    "type": "text",
    "system": false,
    "required": true,
    "presentable": true,
    "options": {
      "min": 1,
      "max": 500,
      "pattern": ""
    }
  },
  {
    "id": "f6g7h8i9j0k",
    "name": "content",
    "type": "editor",
    "system": false,
    "required": false,
    "presentable": false,
    "options": {
      "convertUrls": false
    }
  },
  {
    "id": "fxy987zw654",
    "name": "author",
    "type": "relation",
    "system": false,
    "required": true,
    "presentable": false,
    "options": {
      "collectionId": "users_col_id",
      "cascadeDelete": false,
      "minSelect": null,
      "maxSelect": 1,
      "displayFields": []
    }
  }
]
```

#### `_admins` -- Admin Users

```sql
CREATE TABLE _admins (
    id           VARCHAR(15) PRIMARY KEY DEFAULT ppbase_id(),
    avatar       INTEGER NOT NULL DEFAULT 0,
    email        VARCHAR(255) NOT NULL,
    token_key    VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    last_reset_sent_at TIMESTAMPTZ,
    created      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_admins_email UNIQUE (email),
    CONSTRAINT uq_admins_token_key UNIQUE (token_key)
);
```

#### `_params` -- System Settings (Key-Value Store)

```sql
CREATE TABLE _params (
    id      VARCHAR(15) PRIMARY KEY DEFAULT ppbase_id(),
    key     VARCHAR(255) NOT NULL,
    value   JSONB,
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_params_key UNIQUE (key)
);
```

Used for storing:
- Application settings (app name, URL, SMTP config)
- Secret keys for JWT signing
- Feature flags
- Installed version (for system migrations)

#### `_external_auths` -- OAuth2 Provider Links

```sql
CREATE TABLE _external_auths (
    id            VARCHAR(15) PRIMARY KEY DEFAULT ppbase_id(),
    collection_id VARCHAR(15) NOT NULL,
    record_id     VARCHAR(15) NOT NULL,
    provider      VARCHAR(100) NOT NULL,
    provider_id   VARCHAR(255) NOT NULL,
    created       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_external_auths_provider
        UNIQUE (collection_id, provider, provider_id),
    CONSTRAINT fk_external_auths_collection
        FOREIGN KEY (collection_id) REFERENCES _collections(id) ON DELETE CASCADE
);

CREATE INDEX idx_external_auths_record
    ON _external_auths (collection_id, record_id);
```

### 5.2 Dynamic Collection Tables

When a collection is created via the API, PPBase's `SchemaManager` generates a `CREATE TABLE` statement.

#### Base Collection Table Template

```sql
CREATE TABLE {collection_name} (
    -- System columns (always present)
    id      VARCHAR(15) PRIMARY KEY DEFAULT ppbase_id(),
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- User-defined columns (generated from schema)
    {column_definitions}
);

-- Default indexes
CREATE INDEX idx_{collection_name}_created ON {collection_name} (created);
```

#### Auth Collection Table Template

Auth collections extend the base with authentication-specific columns:

```sql
CREATE TABLE {collection_name} (
    -- System columns
    id              VARCHAR(15) PRIMARY KEY DEFAULT ppbase_id(),
    created         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Auth system columns
    username         VARCHAR(255) NOT NULL DEFAULT '',
    email            VARCHAR(255) NOT NULL DEFAULT '',
    email_visibility BOOLEAN NOT NULL DEFAULT FALSE,
    verified         BOOLEAN NOT NULL DEFAULT FALSE,
    token_key        VARCHAR(255) NOT NULL DEFAULT '',
    password_hash    VARCHAR(255) NOT NULL DEFAULT '',
    last_reset_sent_at      TIMESTAMPTZ,
    last_verification_sent_at TIMESTAMPTZ,

    -- User-defined columns
    {column_definitions},

    CONSTRAINT uq_{collection_name}_username UNIQUE (username),
    CONSTRAINT uq_{collection_name}_email UNIQUE (email),
    CONSTRAINT uq_{collection_name}_token_key UNIQUE (token_key)
);

CREATE INDEX idx_{collection_name}_created ON {collection_name} (created);
CREATE INDEX idx_{collection_name}_token_key ON {collection_name} (token_key);
```

#### View Collection (No Physical Table)

View collections are PostgreSQL views:

```sql
CREATE VIEW {collection_name} AS
    {user_defined_select_query};
```

### 5.3 Field Type to PostgreSQL Column Mapping

| PocketBase Field Type | PostgreSQL Column Type | Default Value | Notes |
|---|---|---|---|
| `text` | `TEXT` | `''` | |
| `editor` | `TEXT` | `''` | Rich text (HTML) |
| `number` | `DOUBLE PRECISION` | `0` | |
| `bool` | `BOOLEAN` | `FALSE` | |
| `email` | `VARCHAR(255)` | `''` | Application-level validation |
| `url` | `TEXT` | `''` | Application-level validation |
| `date` | `TIMESTAMPTZ` | `NULL` | Nullable unlike most fields |
| `select` (single) | `TEXT` | `''` | |
| `select` (multiple) | `TEXT[]` | `'{}'` | PostgreSQL array |
| `json` | `JSONB` | `'null'::jsonb` | |
| `file` (single) | `TEXT` | `''` | Stores filename |
| `file` (multiple) | `TEXT[]` | `'{}'` | Array of filenames |
| `relation` (single) | `VARCHAR(15)` | `''` | FK to target collection |
| `relation` (multiple) | `VARCHAR(15)[]` | `'{}'` | Array of FK values |
| `autodate` | `TIMESTAMPTZ` | `NOW()` | `created` and `updated` |

### 5.4 Index Strategy

PPBase creates indexes strategically based on field types and collection configuration:

**Automatic indexes:**

```sql
-- Every collection
CREATE INDEX idx_{coll}_created ON {coll} (created);

-- Auth collections
CREATE UNIQUE INDEX idx_{coll}_email ON {coll} (email) WHERE email != '';
CREATE UNIQUE INDEX idx_{coll}_username ON {coll} (username) WHERE username != '';
CREATE UNIQUE INDEX idx_{coll}_token_key ON {coll} (token_key);

-- Relation fields (single)
CREATE INDEX idx_{coll}_{field} ON {coll} ({field}) WHERE {field} != '';

-- Relation fields (multiple) -- GIN for array containment
CREATE INDEX idx_{coll}_{field} ON {coll} USING GIN ({field});

-- JSONB fields
CREATE INDEX idx_{coll}_{field} ON {coll} USING GIN ({field});

-- Select fields (multiple) -- GIN for array queries
CREATE INDEX idx_{coll}_{field} ON {coll} USING GIN ({field});
```

**User-defined indexes (via `indexes` JSONB in `_collections`):**

```json
[
  "CREATE INDEX idx_posts_title ON posts (title)",
  "CREATE UNIQUE INDEX idx_posts_slug ON posts (slug)",
  "CREATE INDEX idx_posts_fts ON posts USING GiST (search_vector)"
]
```

### 5.5 Handling Relations Between Collections

Relations are stored as VARCHAR(15) references (single) or VARCHAR(15)[] arrays (multiple).

**Single relation (maxSelect = 1):**

```sql
-- Column definition
author VARCHAR(15) NOT NULL DEFAULT ''

-- Foreign key (optional, configurable)
ALTER TABLE posts ADD CONSTRAINT fk_posts_author
    FOREIGN KEY (author) REFERENCES users(id)
    ON DELETE SET DEFAULT;
```

**Multiple relation (maxSelect > 1):**

```sql
-- Column definition
tags VARCHAR(15)[] NOT NULL DEFAULT '{}'

-- No FK constraint for arrays -- enforced at application level
-- GIN index for containment queries
CREATE INDEX idx_posts_tags ON posts USING GIN (tags);
```

**Relation expansion (the `expand` query parameter):**

When a client requests `?expand=author,tags`, PPBase:

1. Fetches the primary records
2. Collects all relation IDs from the result
3. Batch-fetches related records in a single query per relation
4. Nests the expanded records in the response

```python
# Pseudo-code for expand resolution
async def expand_records(
    records: list[dict],
    expand_fields: list[str],
    collections_cache: dict,
) -> list[dict]:
    for field_name in expand_fields:
        field_def = get_field_def(field_name)
        target_collection = collections_cache[field_def.options.collection_id]
        target_table = await table_manager.get_table(target_collection.name)

        # Collect all IDs to fetch
        all_ids = set()
        for record in records:
            value = record.get(field_name)
            if isinstance(value, list):
                all_ids.update(value)
            elif value:
                all_ids.add(value)

        # Batch fetch
        query = select(target_table).where(
            target_table.c.id.in_(list(all_ids))
        )
        related = {r["id"]: r for r in await execute(query)}

        # Attach to records
        for record in records:
            value = record.get(field_name)
            if isinstance(value, list):
                record[f"expand"] = record.get("expand", {})
                record["expand"][field_name] = [
                    related[rid] for rid in value if rid in related
                ]
            elif value and value in related:
                record["expand"] = record.get("expand", {})
                record["expand"][field_name] = related[value]

    return records
```

**Cascade delete:**

When `cascadeDelete` is enabled on a relation field, PPBase implements cascade at the application level (not via SQL FK CASCADE) to maintain compatibility with PocketBase's behavior and to trigger hooks:

```python
async def cascade_delete(record_id: str, collection: CollectionModel):
    # Find all collections with relations pointing to this collection
    for other_collection in await get_all_collections():
        for field in other_collection.schema:
            if (
                field.type == "relation"
                and field.options.collection_id == collection.id
                and field.options.cascade_delete
            ):
                # Find and delete related records
                related = await find_records_with_relation(
                    other_collection, field.name, record_id
                )
                for related_record in related:
                    await delete_record(other_collection, related_record.id)
```

---

## 6. Phase 1 Scope (CRUD + Admin)

### 6.1 Phase 1: Core CRUD + Admin API (Target: 4-6 weeks)

Phase 1 delivers the minimum viable PPBase: a working BaaS that can create collections, manage records, and authenticate admins.

#### What to Implement

**P0 -- Must Have (Week 1-3):**

1. **Project scaffolding**
   - Package structure as defined in Section 3
   - `pyproject.toml` with all dependencies
   - Configuration system (`config.py` with Pydantic BaseSettings)
   - CLI entry point (`ppbase serve`, `ppbase migrate`, `ppbase create-admin`)

2. **Database layer**
   - Async engine setup with asyncpg
   - Alembic migration infrastructure for system tables
   - System table creation (`_collections`, `_admins`, `_params`)
   - `SchemaManager` for dynamic table CREATE/ALTER/DROP
   - `DynamicTableManager` for SQLAlchemy Core table reflection
   - `ppbase_id()` function installation

3. **Collection management API**
   - `POST /api/collections` -- Create collection (creates table)
   - `GET /api/collections` -- List all collections
   - `GET /api/collections/{idOrName}` -- Get collection details
   - `PATCH /api/collections/{idOrName}` -- Update collection (alters table)
   - `DELETE /api/collections/{idOrName}` -- Delete collection (drops table)
   - `POST /api/collections/import` -- Bulk import collections

4. **Record CRUD API**
   - `GET /api/collections/{collection}/records` -- List records (paginated)
   - `GET /api/collections/{collection}/records/{id}` -- Get single record
   - `POST /api/collections/{collection}/records` -- Create record
   - `PATCH /api/collections/{collection}/records/{id}` -- Update record
   - `DELETE /api/collections/{collection}/records/{id}` -- Delete record

5. **Filtering and sorting**
   - Filter parser (Lark grammar) for PocketBase filter syntax
   - Sort parameter parsing
   - Pagination (page/perPage)

6. **Admin authentication**
   - `POST /api/admins/auth-with-password` -- Admin login
   - `POST /api/admins/auth-refresh` -- Refresh admin token
   - JWT token generation and validation
   - Auth middleware for protecting admin-only routes

7. **Admin CRUD**
   - `POST /api/admins` -- Create admin
   - `GET /api/admins` -- List admins
   - `GET /api/admins/{id}` -- Get admin
   - `PATCH /api/admins/{id}` -- Update admin
   - `DELETE /api/admins/{id}` -- Delete admin

**P1 -- Should Have (Week 3-5):**

8. **Field types and validation**
   - All field types listed in Section 5.3
   - Required/optional enforcement
   - Min/max/pattern validation for text fields
   - Min/max validation for number fields
   - Select field values enforcement
   - Relation field target validation

9. **Relation expansion**
   - `?expand=field1,field2` query parameter
   - Nested expansion up to 6 levels (`expand=author.profile.avatar`)
   - Batch fetching for performance

10. **API rules enforcement**
    - Parse and evaluate `listRule`, `viewRule`, `createRule`, `updateRule`, `deleteRule`
    - `@request.auth.*` context in rules
    - NULL rules = admin only, empty string = public

11. **Health and settings**
    - `GET /api/health` -- Server health check
    - `GET /api/settings` -- Get system settings (admin only)
    - `PATCH /api/settings` -- Update settings (admin only)

**P2 -- Nice to Have (Week 5-6):**

12. **File handling (basic)**
    - File upload on record create/update
    - File serving (`GET /api/files/{collection}/{record}/{filename}`)
    - Local filesystem storage backend
    - File field validation (maxSize, mimeTypes)

13. **Library mode**
    - `PPBase` class with `get_app()` and `start()`
    - Service container for programmatic access
    - Mount support for embedding in existing FastAPI apps

14. **Basic admin UI**
    - Static placeholder or minimal admin dashboard
    - Collection list/edit interface
    - Record browser

#### What to Defer

| Feature | Deferred To | Reason |
|---|---|---|
| Auth collection user login | Phase 2 | Requires email verification, password reset flows |
| OAuth2 providers | Phase 2 | Complex provider-specific logic |
| Realtime (SSE) | Phase 2 | Needs LISTEN/NOTIFY infrastructure |
| Hooks (before/after events) | Phase 2 | Core CRUD must be solid first |
| S3 file storage | Phase 2 | Local storage sufficient for MVP |
| Image thumbnails | Phase 2 | Pillow integration is straightforward but not critical |
| Rate limiting | Phase 2 | Not needed for development/testing |
| Logs API | Phase 2 | Useful but not essential |
| View collections | Phase 2 | SQL view creation needs careful validation |
| Backup/restore | Phase 3 | pg_dump/pg_restore can be used directly |
| Row-level security | Phase 3 | Advanced PostgreSQL feature |
| Full-text search fields | Phase 3 | GiST/GIN FTS is powerful but complex |
| Multi-tenancy | Phase 3 | Schema-per-tenant or DB-per-tenant patterns |

### 6.2 Minimum Viable Feature Set

The absolute minimum to call PPBase "working" is:

```
1. Start server with: ppbase serve --db postgresql://...
2. Create an admin account: ppbase create-admin
3. Login as admin via API
4. Create a collection (dynamic table created)
5. Add/edit/remove fields on collection (table altered)
6. CRUD records in the collection
7. Filter, sort, and paginate records
8. Expand relations between records
9. API rules restrict access based on auth state
```

This covers the core PocketBase use case: define your data model through the API, and immediately get a full REST API for it.

### 6.3 Phase 1 Test Strategy

```
tests/
├── conftest.py               # Fixtures: test database, PPBase instance
├── unit/
│   ├── test_filter_parser.py  # Filter syntax -> SQL translation
│   ├── test_id_generator.py   # ID generation
│   ├── test_field_types.py    # Field validation logic
│   └── test_schema_manager.py # DDL generation (without DB)
├── integration/
│   ├── test_collections_api.py # Collection CRUD endpoints
│   ├── test_records_api.py     # Record CRUD endpoints
│   ├── test_admin_auth.py      # Admin login/token flow
│   ├── test_api_rules.py       # Access rule enforcement
│   ├── test_relations.py       # Relation expansion
│   └── test_dynamic_tables.py  # Schema sync with real PostgreSQL
└── fixtures/
    ├── collections.json        # Sample collection definitions
    └── records.json            # Sample records
```

**Testing infrastructure:**

- `testcontainers[postgres]` spins up a real PostgreSQL instance in Docker for integration tests
- `httpx.AsyncClient` as the async test client for FastAPI
- Each test gets a clean database (DROP/CREATE schema per test module)

---

## 7. Implementation Roadmap

### Phase 1: Core CRUD + Admin (Weeks 1-6)

```
Week 1: Project setup, database layer, system tables
Week 2: SchemaManager, collection API, dynamic table creation
Week 3: Record CRUD, query builder, filter parser
Week 4: Admin auth, JWT, API rules enforcement
Week 5: Field validation, relation expansion, file basics
Week 6: Testing, bug fixes, documentation, CLI polish
```

### Phase 2: Auth + Realtime + Hooks (Weeks 7-12)

```
- Auth collection user registration and login
- Email verification and password reset
- OAuth2 providers (Google, GitHub, etc.)
- SSE realtime subscriptions
- LISTEN/NOTIFY integration
- Before/after event hooks
- S3 storage backend
- Image thumbnails
- Rate limiting
- View collections
- Admin UI (full-featured SPA)
```

### Phase 3: Advanced Features (Weeks 13-18)

```
- Full-text search integration
- Row-level security
- Backup/restore API
- Multi-tenancy support
- Custom API endpoints (user-defined)
- Plugin system
- Performance optimization and benchmarking
- Production deployment guides
```

---

## Appendix A: PocketBase API Compatibility Reference

PPBase targets compatibility with PocketBase's REST API. The following endpoints are in scope:

| Endpoint | Phase | Notes |
|---|---|---|
| `GET /api/health` | 1 | |
| `GET /api/settings` | 1 | |
| `PATCH /api/settings` | 1 | |
| `GET /api/admins` | 1 | |
| `POST /api/admins` | 1 | |
| `GET /api/admins/{id}` | 1 | |
| `PATCH /api/admins/{id}` | 1 | |
| `DELETE /api/admins/{id}` | 1 | |
| `POST /api/admins/auth-with-password` | 1 | |
| `POST /api/admins/auth-refresh` | 1 | |
| `GET /api/collections` | 1 | |
| `POST /api/collections` | 1 | |
| `GET /api/collections/{idOrName}` | 1 | |
| `PATCH /api/collections/{idOrName}` | 1 | |
| `DELETE /api/collections/{idOrName}` | 1 | |
| `PUT /api/collections/import` | 1 | |
| `GET /api/collections/{coll}/records` | 1 | |
| `POST /api/collections/{coll}/records` | 1 | |
| `GET /api/collections/{coll}/records/{id}` | 1 | |
| `PATCH /api/collections/{coll}/records/{id}` | 1 | |
| `DELETE /api/collections/{coll}/records/{id}` | 1 | |
| `GET /api/files/{coll}/{record}/{filename}` | 1 | |
| `POST /api/collections/{coll}/auth-with-password` | 2 | |
| `POST /api/collections/{coll}/auth-with-oauth2` | 2 | |
| `POST /api/collections/{coll}/auth-refresh` | 2 | |
| `POST /api/collections/{coll}/request-verification` | 2 | |
| `POST /api/collections/{coll}/confirm-verification` | 2 | |
| `POST /api/collections/{coll}/request-password-reset` | 2 | |
| `POST /api/collections/{coll}/confirm-password-reset` | 2 | |
| `POST /api/collections/{coll}/request-email-change` | 2 | |
| `POST /api/collections/{coll}/confirm-email-change` | 2 | |
| `GET /api/collections/{coll}/auth-methods` | 2 | |
| `GET /api/realtime` | 2 | SSE |
| `GET /api/logs` | 2 | |
| `GET /api/logs/{id}` | 2 | |

---

## Appendix B: Configuration Reference

```python
# ppbase/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """PPBase configuration. All values can be set via environment variables."""

    # Database
    database_url: str = "postgresql+asyncpg://localhost:5433/ppbase"
    pool_size: int = 10
    max_overflow: int = 20

    # Server
    host: str = "0.0.0.0"
    port: int = 8090
    dev: bool = False

    # Storage
    data_dir: str = "./pb_data"
    storage_backend: str = "local"  # "local" or "s3"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""

    # Auth
    jwt_secret: str = ""  # Auto-generated on first run if empty
    admin_token_duration: int = 1209600       # 14 days in seconds
    record_token_duration: int = 1209600      # 14 days in seconds

    # CORS
    origins: list[str] = ["*"]

    # Misc
    log_level: str = "INFO"
    max_request_body_size: int = 5_242_880   # 5MB

    model_config = {
        "env_prefix": "PPBASE_",
        "env_file": ".env",
    }
```

---

## Appendix C: Key SQLAlchemy Patterns

### C.1 Async Engine Lifecycle

```python
# ppbase/db/engine.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from contextlib import asynccontextmanager

_engine: AsyncEngine | None = None

async def init_engine(database_url: str, **kwargs) -> AsyncEngine:
    global _engine
    _engine = create_async_engine(
        database_url,
        pool_size=kwargs.get("pool_size", 10),
        max_overflow=kwargs.get("max_overflow", 20),
        pool_pre_ping=True,
        echo=kwargs.get("echo", False),
    )
    return _engine

async def close_engine() -> None:
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None

def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialized")
    return _engine
```

### C.2 FastAPI Lifespan Integration

```python
# ppbase/app.py
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    engine = await init_engine(app.state.config.database_url)

    # Run system migrations
    await run_system_migrations(engine)

    # Install ppbase_id() function
    await install_db_functions(engine)

    yield

    # Shutdown
    await close_engine()

def create_app(config: Settings) -> FastAPI:
    app = FastAPI(
        title="PPBase",
        lifespan=lifespan,
        docs_url=None if not config.dev else "/api/docs",
    )
    app.state.config = config

    # Register routes
    app.include_router(api_router, prefix="/api")

    # Mount admin UI
    app.mount(
        "/_/",
        StaticFiles(directory=ADMIN_DIST_DIR, html=True),
        name="admin",
    )

    return app
```

### C.3 Dynamic DDL Generation

```python
# ppbase/db/schema_manager.py (simplified)
from sqlalchemy import (
    Table, Column, MetaData, String, Float, Boolean, Text,
    TIMESTAMP, text
)
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

FIELD_TYPE_TO_COLUMN = {
    "text":     lambda f: Column(f.name, Text, nullable=False, server_default=""),
    "editor":   lambda f: Column(f.name, Text, nullable=False, server_default=""),
    "number":   lambda f: Column(f.name, Float, nullable=False, server_default=text("0")),
    "bool":     lambda f: Column(f.name, Boolean, nullable=False, server_default=text("FALSE")),
    "email":    lambda f: Column(f.name, String(255), nullable=False, server_default=""),
    "url":      lambda f: Column(f.name, Text, nullable=False, server_default=""),
    "date":     lambda f: Column(f.name, TIMESTAMP(timezone=True)),
    "json":     lambda f: Column(f.name, JSONB, nullable=False, server_default=text("'null'::jsonb")),
    "select":   lambda f: (
        Column(f.name, ARRAY(Text), nullable=False, server_default=text("'{}'"))
        if f.options.get("maxSelect", 1) > 1
        else Column(f.name, Text, nullable=False, server_default="")
    ),
    "file":     lambda f: (
        Column(f.name, ARRAY(Text), nullable=False, server_default=text("'{}'"))
        if f.options.get("maxSelect", 1) > 1
        else Column(f.name, Text, nullable=False, server_default="")
    ),
    "relation": lambda f: (
        Column(f.name, ARRAY(String(15)), nullable=False, server_default=text("'{}'"))
        if f.options.get("maxSelect", 1) > 1
        else Column(f.name, String(15), nullable=False, server_default="")
    ),
}

async def create_collection_table(
    engine: AsyncEngine,
    collection: CollectionModel,
) -> None:
    metadata = MetaData()
    columns = [
        Column("id", String(15), primary_key=True, server_default=text("ppbase_id()")),
        Column("created", TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")),
        Column("updated", TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")),
    ]

    # Add auth columns if auth collection
    if collection.type == "auth":
        columns.extend(_auth_system_columns())

    # Add user-defined columns
    for field_def in collection.schema:
        col_factory = FIELD_TYPE_TO_COLUMN.get(field_def.type)
        if col_factory:
            columns.append(col_factory(field_def))

    table = Table(collection.name, metadata, *columns)

    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
```

---

## Appendix D: Decision Log

| Decision | Choice | Alternatives Considered | Rationale |
|---|---|---|---|
| Web framework | FastAPI | Flask, Starlette, Django | Best async + OpenAPI + Pydantic integration |
| Database driver | asyncpg | psycopg3, aiopg | Fastest, best PostgreSQL type support |
| ORM for system tables | SQLAlchemy 2.0 ORM | raw SQL, Tortoise ORM | Industry standard, Alembic integration |
| Dynamic table queries | SQLAlchemy Core | raw SQL, ORM reflection | Type-safe SQL generation without static models |
| Password hashing | passlib + bcrypt | argon2-cffi | PocketBase uses bcrypt; maintain compatibility |
| JWT library | PyJWT | python-jose, authlib | Lightweight, well-maintained |
| Filter parser | Lark | pyparsing, hand-written | EBNF grammar support, transformer API |
| Schema storage | JSONB in _collections | Separate _fields table | Atomic reads, PocketBase API compat |
| Collection storage | Physical columns | Pure JSONB documents | Query performance, type safety, native indexes |
| Admin UI | Svelte SPA | React, Vue | PocketBase compatibility, bundle size |
| File storage | Local + S3 (pluggable) | Local only | Production deployments need S3 |
| Migration (system) | Alembic | hand-written SQL | Standard tool, autogenerate support |
| Migration (dynamic) | Custom SchemaManager | Alembic | Dynamic tables incompatible with autogenerate |
| Realtime | SSE (Phase 2) | WebSockets | PocketBase uses SSE; simpler protocol |
| ID format | 15-char alphanumeric | UUID, ULID, nanoid | PocketBase compatibility |

---

*This document is a living specification. It will be updated as implementation progresses and design decisions are validated.*
