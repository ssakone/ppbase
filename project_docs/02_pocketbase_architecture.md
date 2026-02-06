# PocketBase Go Source Code Architecture - Comprehensive Analysis

> This document provides a deep analysis of PocketBase's Go source code architecture,
> intended to serve as a blueprint for replicating its functionality in Python.
> Based on the PocketBase repository at github.com/pocketbase/pocketbase (master branch).

---

## Table of Contents

1. [Project Structure](#1-project-structure)
2. [Core Models](#2-core-models)
3. [Database Layer](#3-database-layer)
4. [API Layer](#4-api-layer)
5. [Collection Schema](#5-collection-schema)
6. [Record CRUD Operations](#6-record-crud-operations)
7. [Admin/Superuser Operations](#7-adminsuperuser-operations)
8. [Authentication Flow](#8-authentication-flow)
9. [File Storage](#9-file-storage)
10. [Migrations](#10-migrations)
11. [Hook/Event System](#11-hookevent-system)
12. [Settings System](#12-settings-system)
13. [Realtime/SSE](#13-realtimesse)
14. [Key Design Patterns for Python Replication](#14-key-design-patterns-for-python-replication)

---

## 1. Project Structure

### Root Directory Layout

```
pocketbase/
|-- pocketbase.go          # Main PocketBase struct, wraps core.App + cobra CLI
|-- apis/                  # REST API handlers and middlewares
|-- cmd/                   # CLI commands (serve, superuser)
|-- core/                  # THE BACKBONE - App interface, models, DB ops, fields, hooks
|-- examples/              # Usage examples
|-- forms/                 # Form handlers (record upsert, etc.)
|-- mails/                 # Email template helpers
|-- migrations/            # Built-in database migration files
|-- plugins/               # Optional plugins (JS VM, migrate cmd, GitHub updates)
|-- tests/                 # Integration/E2E tests
|-- tools/                 # Utility packages (shared libraries)
|-- ui/                    # Embedded admin dashboard (pre-built SPA)
|-- go.mod / go.sum        # Go module definitions
```

### Package Responsibilities

| Package | Purpose |
|---------|---------|
| `core/` | The backbone. Defines the App interface, BaseApp implementation, ALL models (Record, Collection, Settings, Log, etc.), ALL database operations (Save, Delete, queries), ALL field types, hook/event system, migrations runner, record field resolver, tokens |
| `apis/` | HTTP REST API endpoint handlers, middleware stack, router setup, file serving, realtime SSE, batch API |
| `forms/` | Form validation/processing layer between API requests and core operations. Currently: `RecordUpsert`, `TestEmailSend`, `TestS3Filesystem`, `AppleClientSecretCreate` |
| `tools/` | Shared utility libraries (archive, auth providers, cron, dbutils, filesystem, hook, inflector, list, logger, mailer, osutils, picker, router, routine, search, security, store, subscriptions, template, tokenizer, types) |
| `cmd/` | CLI command definitions: `serve` (starts HTTP server), `superuser` (manage superuser accounts) |
| `mails/` | Email sending helpers for record-related operations (verification, password reset, etc.) |
| `migrations/` | Built-in SQL migration files that initialize and upgrade the database schema |
| `plugins/` | Optional plugins: `jsvm` (JavaScript VM for hooks), `migratecmd` (auto-generate migrations), `ghupdate` (GitHub auto-update) |
| `ui/` | Pre-built embedded admin dashboard SPA (served at `/_/`) |

### tools/ Sub-packages Detail

| Sub-package | Purpose |
|-------------|---------|
| `tools/archive` | ZIP archive creation/extraction |
| `tools/auth` | OAuth2 provider interfaces and implementations (Google, Facebook, GitHub, Apple, etc.) |
| `tools/cron` | Cron job scheduler |
| `tools/dbutils` | Database utility helpers (index parsing, etc.) |
| `tools/filesystem` | File system abstraction (local + S3), file upload, thumb generation |
| `tools/hook` | Generic typed hook/event system with priority-based handler chains |
| `tools/inflector` | String inflection (columnify, slugify, etc.) |
| `tools/list` | Slice/list helper utilities |
| `tools/logger` | Structured logging (writes to auxiliary DB) |
| `tools/mailer` | SMTP email sending |
| `tools/osutils` | OS-level utilities |
| `tools/picker` | Data picking/filtering from maps |
| `tools/router` | HTTP router implementation (generic, typed) |
| `tools/routine` | Goroutine helpers (fire-and-forget) |
| `tools/search` | Search provider, filter parser, sort parser for list APIs |
| `tools/security` | Cryptographic utilities (JWT, encryption, random strings, bcrypt) |
| `tools/store` | Thread-safe generic key-value in-memory store |
| `tools/subscriptions` | Realtime subscription broker and client management |
| `tools/template` | HTML template rendering |
| `tools/tokenizer` | String tokenizer for filter expressions |
| `tools/types` | Custom types (DateTime, JSONRaw, JSONArray, JSONMap) |

---

## 2. Core Models

### 2.1 Base Model Interface and Struct

**File: `core/db_model.go`**

All database-persisted models implement the `Model` interface:

```go
type Model interface {
    TableName() string
    PK() any
    LastSavedPK() any
    IsNew() bool
    MarkAsNew()
    MarkAsNotNew()
}
```

The `BaseModel` struct provides the default implementation:

```go
type BaseModel struct {
    lastSavedPK string
    Id string `db:"id" json:"id" form:"id" xml:"id"`
}
```

Key behaviors:
- `IsNew()` returns true when `lastSavedPK == ""` (no previous save)
- `MarkAsNotNew()` copies `Id` to `lastSavedPK` (called after DB insert/scan)
- `PostScan()` is called after DB row scanning (implements `dbx.PostScanner`) and calls `MarkAsNotNew()`
- IDs are 15-character lowercase alphanumeric strings by default: `[a-z0-9]{15}`

### 2.2 Record Model

**File: `core/record_model.go`**

The `Record` is the central data model. It is a **dynamic, schema-less struct** that stores field values in a generic map, governed by its parent `Collection`'s field definitions.

```go
type Record struct {
    collection       *Collection
    originalData     map[string]any          // snapshot of data at load time
    customVisibility *store.Store[string, bool]
    data             *store.Store[string, any]  // current field values
    expand           *store.Store[string, any]  // expanded relation data

    BaseModel

    exportCustomData      bool
    ignoreEmailVisibility bool
    ignoreUnchangedFields bool
}
```

Key characteristics:
- **Dynamic fields**: Field values are stored in a thread-safe `store.Store[string, any]` map, NOT as struct fields
- **Collection-bound**: Every Record has a reference to its parent `Collection` which defines the schema
- **Original data tracking**: `originalData` stores the state when loaded from DB (for dirty checking)
- **Expand support**: Related records can be expanded and stored in the `expand` store
- **DBExporter interface**: Implements `DBExport(app App) (map[string]any, error)` for custom DB serialization
- **FilesManager interface**: Implements `BaseFilesPath() string` returning `collectionId/recordId`
- **HookTagger interface**: Returns tags `[collectionId, collectionName, tableName]` for targeted hook binding

Record implements these interfaces:
- `Model` - base DB model
- `HookTagger` - for targeted event hooks
- `DBExporter` - custom DB export
- `FilesManager` - file path management

#### Record Data Access

```go
// Get returns the field value by name, processed through the field's getter
record.Get("fieldName")        // returns any

// GetRaw returns the raw unprocessed value
record.GetRaw("fieldName")    // returns any

// Set sets a field value, processed through the field's setter
record.Set("fieldName", value)

// SetIfFieldExists sets a field only if it exists in the collection schema
record.SetIfFieldExists("fieldName", value)

// Typed getters
record.GetString("name")
record.GetBool("active")
record.GetInt("count")
record.GetFloat("price")
record.GetDateTime("created")
record.GetStringSlice("tags")
```

#### Record Modifier Pattern

Fields support **modifier suffixes** for append/prepend/subtract operations:

```go
record.Set("tags+", "newTag")     // append
record.Set("+tags", "newTag")     // prepend
record.Set("tags-", "oldTag")    // subtract/remove
```

This is implemented through the `SetterFinder` interface on each field type.

### 2.3 Collection Model

**File: `core/collection_model.go`**

Collections define the schema for records. They are themselves stored in the `_collections` table.

```go
type baseCollection struct {
    BaseModel

    ListRule   *string `db:"listRule" json:"listRule"`
    ViewRule   *string `db:"viewRule" json:"viewRule"`
    CreateRule *string `db:"createRule" json:"createRule"`
    UpdateRule *string `db:"updateRule" json:"updateRule"`
    DeleteRule *string `db:"deleteRule" json:"deleteRule"`

    RawOptions types.JSONRaw `db:"options" json:"-"`

    Name    string                  `db:"name" json:"name"`
    Type    string                  `db:"type" json:"type"`
    Fields  FieldsList              `db:"fields" json:"fields"`
    Indexes types.JSONArray[string] `db:"indexes" json:"indexes"`
    Created types.DateTime          `db:"created" json:"created"`
    Updated types.DateTime          `db:"updated" json:"updated"`
    System  bool                    `db:"system" json:"system"`
}

type Collection struct {
    baseCollection
    collectionAuthOptions   // auth-specific options (embedded, only used for auth type)
    collectionViewOptions   // view-specific options (embedded, only used for view type)
}
```

#### Collection Types

| Type | Constant | Description |
|------|----------|-------------|
| `base` | `CollectionTypeBase` | Standard data collection with CRUD |
| `auth` | `CollectionTypeAuth` | Authentication-enabled collection with email, password, tokens |
| `view` | `CollectionTypeView` | Read-only SQL VIEW based collection |

#### Access Rules

Rules are **nullable string pointers** (`*string`):
- `nil` = Only superusers can access (locked)
- `""` (empty string) = Everyone can access (public)
- `"expression"` = Only records matching the filter expression can access

Each rule is a filter expression evaluated against the requesting user's context using the `RecordFieldResolver`.

#### Collection Table Name

Collections are stored in the `_collections` table. Each collection's records are stored in a table named after the collection (e.g., collection named "posts" stores records in "posts" table).

#### Collection ID Generation

Collection IDs are auto-generated using a CRC32 checksum of `type + name`:
```go
func (c *Collection) idChecksum() string {
    return "pbc_" + crc32Checksum(c.Type + c.Name)
}
```

#### Auth Collection System Fields

When a collection is of type `auth`, these system fields are automatically initialized:
- `id` - TextField (primary key, 15 chars, pattern `[a-z0-9]+`)
- `password` - PasswordField (hidden, required, min 8 chars)
- `tokenKey` - TextField (hidden, 30-60 chars, auto-generated, unique index)
- `email` - EmailField (required, unique index with COLLATE NOCASE)
- `emailVisibility` - BoolField
- `verified` - BoolField

### 2.4 Settings Model

**File: `core/settings_model.go`**

Settings implements the `Model` interface and is stored as a single row in the `_params` table with key `"settings"`.

```go
type Settings struct {
    settings
    mu    sync.RWMutex
    isNew bool
}

type settings struct {
    SMTP         SMTPConfig
    Backups      BackupsConfig
    S3           S3Config
    Meta         MetaConfig         // AppName, AppURL, SenderName, SenderAddress
    RateLimits   RateLimitsConfig
    TrustedProxy TrustedProxyConfig
    Batch        BatchConfig
    Logs         LogsConfig         // MaxDays, MinLevel, LogIP, LogAuthId
}
```

Settings are stored encrypted when an encryption environment variable is configured. The `DBExport` method handles encryption/decryption:
```go
encryptionKey := os.Getenv(app.EncryptionEnv())
if encryptionKey != "" {
    encryptVal, _ := security.Encrypt(encoded, encryptionKey)
    result["value"] = encryptVal
}
```

### 2.5 Other Models

All defined in `core/`:

| Model | File | Table | Purpose |
|-------|------|-------|---------|
| `Log` | `log_model.go` | `_logs` (aux DB) | Request activity logs |
| `ExternalAuth` | `external_auth_model.go` | `_externalAuths` | OAuth2 linked accounts |
| `MFA` | `mfa_model.go` | `_mfas` | Multi-factor auth records |
| `OTP` | `otp_model.go` | `_otps` | One-time password records |
| `AuthOrigin` | `auth_origin_model.go` | `_authOrigins` | Auth origin tracking (device fingerprints) |

---

## 3. Database Layer

### 3.1 Database Connection Architecture

**File: `core/db_connect.go`**

PocketBase uses **SQLite** as its database engine via the `modernc.org/sqlite` pure-Go driver, wrapped with `github.com/pocketbase/dbx` (a query builder).

```go
func DefaultDBConnect(dbPath string) (*dbx.DB, error) {
    pragmas := "?_pragma=busy_timeout(10000)" +
               "&_pragma=journal_mode(WAL)" +
               "&_pragma=journal_size_limit(200000000)" +
               "&_pragma=synchronous(NORMAL)" +
               "&_pragma=foreign_keys(ON)" +
               "&_pragma=temp_store(MEMORY)" +
               "&_pragma=cache_size(-32000)"

    db, err := dbx.Open("sqlite", dbPath+pragmas)
    return db, err
}
```

Key SQLite pragmas:
- **WAL mode** for concurrent reads
- **busy_timeout(10000)** - 10 second busy wait
- **synchronous(NORMAL)** - balanced durability/performance
- **foreign_keys(ON)** - enforce referential integrity
- **cache_size(-32000)** - ~32MB page cache

### 3.2 Dual Database Architecture

**File: `core/base.go`**

PocketBase uses TWO separate SQLite databases:

```go
type BaseApp struct {
    concurrentDB       dbx.Builder    // data.db - read operations
    nonconcurrentDB    dbx.Builder    // data.db - write operations
    auxConcurrentDB    dbx.Builder    // auxiliary.db - read (logs, etc.)
    auxNonconcurrentDB dbx.Builder    // auxiliary.db - write
    // ...
}
```

- **data.db** (`DataDir/data.db`): Main database for collections, records, settings, migrations
- **auxiliary.db** (`DataDir/auxiliary.db`): Secondary database for logs and other auxiliary data

Each database has two connection pools:
- **ConcurrentDB**: For read operations (SELECT), higher connection limits (120 max open, 15 idle)
- **NonconcurrentDB**: For write operations (INSERT/UPDATE/DELETE), lower limits (120 max open, 15 idle but serialized writes)

Connection pool defaults:
```go
const (
    DefaultDataMaxOpenConns int = 120
    DefaultDataMaxIdleConns int = 15
    DefaultAuxMaxOpenConns  int = 20
    DefaultAuxMaxIdleConns  int = 3
    DefaultQueryTimeout     time.Duration = 30 * time.Second
)
```

### 3.3 The Save/Delete Pattern (No DAO Layer)

**IMPORTANT**: PocketBase does NOT use a traditional DAO pattern. Instead, it has a unified `App.Save()` / `App.Delete()` approach where the `App` interface (and its `BaseApp` implementation) directly handles all persistence.

**File: `core/db.go`**

#### Save Flow

```go
func (app *BaseApp) Save(model Model) error {
    return app.save(ctx, model, true, false)  // with validation, data.db
}

func (app *BaseApp) save(ctx, model, withValidations, isForAuxDB) error {
    if model.IsNew() {
        return app.create(ctx, model, withValidations, isForAuxDB)
    }
    return app.update(ctx, model, withValidations, isForAuxDB)
}
```

#### Create Flow (detailed)

```
1. app.create(model)
2.   -> Trigger OnModelCreate hook chain
3.      -> If withValidations: app.ValidateWithContext(model)
4.         -> Trigger OnModelValidate hook chain
5.      -> Trigger OnModelCreateExecute hook chain
6.         -> model.DBExport(app) to get column data map
7.         -> db.Insert(tableName, data).Execute()
8.         -> model.MarkAsNotNew()
9.   -> On success: Trigger OnModelAfterCreateSuccess
10.  -> On failure: Trigger OnModelAfterCreateError
```

#### Update Flow (detailed)

```
1. app.update(model)
2.   -> Trigger OnModelUpdate hook chain
3.      -> If withValidations: app.ValidateWithContext(model)
4.         -> Trigger OnModelValidate hook chain
5.      -> Trigger OnModelUpdateExecute hook chain
6.         -> model.DBExport(app) to get column data map
7.         -> db.Update(tableName, data, {id: lastSavedPK}).Execute()
8.   -> On success: Trigger OnModelAfterUpdateSuccess
9.  -> On failure: Trigger OnModelAfterUpdateError
```

#### Delete Flow (detailed)

```
1. app.Delete(model)
2.   -> Trigger OnModelDelete hook chain
3.      -> Trigger OnModelDeleteExecute hook chain
4.         -> db.Delete(tableName, {id: lastSavedPK}).Execute()
5.   -> On success: Trigger OnModelAfterDeleteSuccess
6.   -> On failure: Trigger OnModelAfterDeleteError
```

#### DBExporter Interface

Models that need custom DB serialization implement:
```go
type DBExporter interface {
    DBExport(app App) (map[string]any, error)
}
```

This returns a `map[string]any` of column name to value, which is used for INSERT/UPDATE queries. Both `Record` and `Collection` implement this interface.

For `Record`, `DBExport` iterates over all collection fields and calls each field's `DriverValue(record)` method to get the database-serializable value.

### 3.4 Transaction Support

**File: `core/db_tx.go`**

```go
func (app *BaseApp) RunInTransaction(fn func(txApp App) error) error
func (app *BaseApp) AuxRunInTransaction(fn func(txApp App) error) error
```

Transaction implementation:
1. Creates a shallow clone of the current `BaseApp`
2. Replaces the DB connections with the transaction `*dbx.Tx`
3. Attaches a `TxAppInfo` to the clone with after-completion callbacks
4. Nested transactions are supported - if already in a transaction, `fn` runs on the existing transaction
5. After transaction completes (success or rollback), all registered `OnComplete` callbacks are executed

```go
type TxAppInfo struct {
    parent     *BaseApp
    afterFuncs []func(txErr error) error
    isForAuxDB bool
}
```

This is critical for the hook system: hooks like `OnModelAfterCreateSuccess` are deferred until after the transaction commits successfully.

### 3.5 Lock Retry Mechanism

**File: `core/db_retry.go`**

PocketBase implements automatic retry on SQLite `SQLITE_BUSY` / `SQLITE_LOCKED` errors:

```go
func baseLockRetry(fn func(attempt int) error, maxRetries int) error
func execLockRetry(timeout time.Duration, maxRetries int) func(q, a, op) error
```

Default max retries: 8, with exponential backoff.

### 3.6 Query Building

**File: `core/record_query.go`**

Record queries are built using `dbx.SelectQuery` with custom hooks for automatic Record hydration:

```go
func (app *BaseApp) RecordQuery(collectionModelOrIdentifier any) *dbx.SelectQuery
```

This method:
1. Resolves the collection from a model, ID, or name
2. Creates a SELECT query: `SELECT "tableName".* FROM tableName`
3. Attaches `WithOneHook` and `WithAllHook` for automatic Record construction from `NullStringMap`
4. Handles the `RecordProxy` interface for custom record types

Key query methods on `BaseApp`:
```go
FindRecordById(collection, recordId, ...filters)
FindRecordsByIds(collection, recordIds, ...filters)
FindAllRecords(collection, ...expressions)
FindFirstRecordByData(collection, key, value)
FindRecordsByFilter(collection, filter, sort, limit, offset, ...params)
FindFirstRecordByFilter(collection, filter, ...params)
CountRecords(collection, ...expressions)
FindAuthRecordByToken(token, ...validTypes)
FindAuthRecordByEmail(collection, email)
CanAccessRecord(record, requestInfo, accessRule)
```

---

## 4. API Layer

### 4.1 Router Architecture

**File: `apis/base.go`**

PocketBase uses a custom generic typed router (`tools/router`) that creates a new `core.RequestEvent` for each request:

```go
func NewRouter(app core.App) (*router.Router[*core.RequestEvent], error) {
    pbRouter := router.NewRouter(func(w http.ResponseWriter, r *http.Request) (*core.RequestEvent, router.EventCleanupFunc) {
        event := new(core.RequestEvent)
        event.Response = w
        event.Request = r
        event.App = app
        return event, nil
    })
    // ... register default middlewares and routes
}
```

### 4.2 API Route Registration

All API routes are registered under the `/api` prefix:

```go
apiGroup := pbRouter.Group("/api")
bindSettingsApi(app, apiGroup)      // /api/settings
bindCollectionApi(app, apiGroup)    // /api/collections
bindRecordCrudApi(app, apiGroup)    // /api/collections/{collection}/records
bindRecordAuthApi(app, apiGroup)    // /api/collections/{collection}/auth-*
bindLogsApi(app, apiGroup)          // /api/logs
bindBackupApi(app, apiGroup)        // /api/backups
bindCronApi(app, apiGroup)          // /api/crons
bindFileApi(app, apiGroup)          // /api/files
bindBatchApi(app, apiGroup)         // /api/batch
bindRealtimeApi(app, apiGroup)      // /api/realtime
bindHealthApi(app, apiGroup)        // /api/health
```

Admin dashboard is served at `/_/{path...}` using the embedded SPA.

### 4.3 Complete API Endpoints

#### Record CRUD (`apis/record_crud.go`)
```
GET    /api/collections/{collection}/records       -> recordsList
GET    /api/collections/{collection}/records/{id}  -> recordView
POST   /api/collections/{collection}/records       -> recordCreate
PATCH  /api/collections/{collection}/records/{id}  -> recordUpdate
DELETE /api/collections/{collection}/records/{id}  -> recordDelete
```

#### Record Auth (`apis/record_auth.go`)
```
GET    /api/collections/{collection}/auth-methods               -> recordAuthMethods
POST   /api/collections/{collection}/auth-refresh               -> recordAuthRefresh
POST   /api/collections/{collection}/auth-with-password          -> recordAuthWithPassword
POST   /api/collections/{collection}/auth-with-oauth2            -> recordAuthWithOAuth2
POST   /api/collections/{collection}/request-otp                -> recordRequestOTP
POST   /api/collections/{collection}/auth-with-otp              -> recordAuthWithOTP
POST   /api/collections/{collection}/request-password-reset      -> recordRequestPasswordReset
POST   /api/collections/{collection}/confirm-password-reset      -> recordConfirmPasswordReset
POST   /api/collections/{collection}/request-verification        -> recordRequestVerification
POST   /api/collections/{collection}/confirm-verification        -> recordConfirmVerification
POST   /api/collections/{collection}/request-email-change        -> recordRequestEmailChange
POST   /api/collections/{collection}/confirm-email-change        -> recordConfirmEmailChange
POST   /api/collections/{collection}/impersonate/{id}            -> recordAuthImpersonate
GET    /api/oauth2-redirect                                      -> oauth2SubscriptionRedirect
POST   /api/oauth2-redirect                                      -> oauth2SubscriptionRedirect
```

#### Files (`apis/file.go`)
```
POST   /api/files/token                                 -> fileToken
GET    /api/files/{collection}/{recordId}/{filename}     -> download
```

#### Collections, Settings, Logs, Backups, Cron, Health, Batch, Realtime
```
GET/POST/PATCH/DELETE /api/collections/...
GET/PATCH            /api/settings
GET                  /api/logs
GET/POST/DELETE      /api/backups/...
GET                  /api/crons
GET                  /api/health
POST                 /api/batch
GET/POST             /api/realtime
```

### 4.4 Middleware Stack

**File: `apis/middlewares.go`**

Default middleware registered in order (by priority, lower = earlier):

| Priority | ID | Middleware | Purpose |
|----------|----|-----------|---------|
| -99999 | `pbWWWRedirect` | WWW Redirect | Redirects www to non-www |
| RateLimit-40 | `pbActivityLogger` | Activity Logger | Logs requests to auxiliary DB |
| RateLimit-30 | `pbPanicRecover` | Panic Recovery | Catches panics, returns 500 |
| RateLimit-20 | `pbLoadAuthToken` | Auth Token Loader | Extracts JWT from Authorization header |
| RateLimit-10 | `pbSecurityHeaders` | Security Headers | X-XSS-Protection, X-Content-Type-Options, X-Frame-Options |
| 0 | `pbRateLimit` | Rate Limiter | Configurable rate limiting |
| 0 | `pbBodyLimit` | Body Limit | Request body size limit (default 32MB) |
| (per-route) | `pbCors` | CORS | Cross-origin resource sharing |
| (per-route) | `pbGzip` | Gzip | Response compression |

#### Auth Token Loading

The `loadAuthToken` middleware:
1. Extracts token from `Authorization: Bearer TOKEN` header
2. Strips optional "Bearer " prefix
3. Calls `app.FindAuthRecordByToken(token, TokenTypeAuth)`
4. If valid, sets `e.Auth = record`
5. On failure, silently continues (allows custom handling)

#### Available Auth Middlewares

```go
RequireGuestOnly()                          // Rejects if auth is present
RequireAuth(optCollectionNames ...string)   // Requires valid auth token
RequireSuperuserAuth()                      // Requires superuser auth
RequireSuperuserOrOwnerAuth(param string)   // Superuser OR record owner
RequireSameCollectionContextAuth(param string) // Auth from same collection as path
```

### 4.5 Request Event

**File: `core/event_request.go`**

Every HTTP request creates a `RequestEvent`:

```go
type RequestEvent struct {
    hook.Event
    App      App
    Auth     *Record              // authenticated user (if any)
    Response http.ResponseWriter
    Request  *http.Request
    // ... helper methods
}
```

Key helper methods:
- `e.RequestInfo()` - returns parsed `RequestInfo` with auth, body, query params, headers
- `e.JSON(status, data)` - sends JSON response
- `e.NoContent(status)` - sends empty response
- `e.NotFoundError(msg, err)` - returns 404
- `e.BadRequestError(msg, err)` - returns 400
- `e.ForbiddenError(msg, err)` - returns 403
- `e.UnauthorizedError(msg, err)` - returns 401
- `e.InternalServerError(msg, err)` - returns 500
- `e.Redirect(status, url)` - HTTP redirect
- `e.FileFS(fsys, path)` - serve file from filesystem
- `e.FindUploadedFiles(key)` - extract multipart uploaded files

### 4.6 Search/Filter Provider

**File: `tools/search/provider.go`**

The search provider handles list pagination, filtering, and sorting:

```go
type Provider struct {
    fieldResolver      FieldResolver
    query              *dbx.SelectQuery
    countCol           string
    sort               []SortField
    filter             []FilterData
    page               int
    perPage            int
    skipTotal          bool
}
```

Query parameters:
- `page` - Page number (default 1)
- `perPage` - Items per page (default 30, max 1000)
- `sort` - Sort expression (e.g., `-created,title`)
- `filter` - Filter expression (e.g., `title ~ "test" && status = "active"`)
- `skipTotal` - Skip counting total items (performance optimization)

Returns a `Result`:
```go
type Result struct {
    Items      any `json:"items"`
    Page       int `json:"page"`
    PerPage    int `json:"perPage"`
    TotalItems int `json:"totalItems"`
    TotalPages int `json:"totalPages"`
}
```

---

## 5. Collection Schema

### 5.1 Field Type System

**File: `core/field.go`**

All field types implement the `Field` interface:

```go
type Field interface {
    GetId() string
    SetId(id string)
    GetName() string
    SetName(name string)
    GetSystem() bool
    SetSystem(system bool)
    GetHidden() bool
    SetHidden(hidden bool)
    Type() string
    ColumnType(app App) string
    PrepareValue(record *Record, raw any) (any, error)
    ValidateValue(ctx context.Context, app App, record *Record) error
    ValidateSettings(ctx context.Context, app App, collection *Collection) error
}
```

Optional field interfaces:
- `MaxBodySizeCalculator` - custom max body size for file fields
- `SetterFinder` - custom setter functions (e.g., `field+` for append)
- `GetterFinder` - custom getter functions (e.g., `field:excerpt`)
- `DriverValuer` - custom DB value export
- `MultiValuer` - indicates multi-valued field (e.g., multi-select, multi-file)
- `RecordInterceptor` - hook into record CRUD lifecycle per field

### 5.2 Available Field Types

| Type | File | DB Column | Go Type | Description |
|------|------|-----------|---------|-------------|
| `text` | `field_text.go` | `TEXT DEFAULT '' NOT NULL` | `string` | Plain text with optional pattern, min/max length, autogenerate |
| `editor` | `field_editor.go` | `TEXT DEFAULT '' NOT NULL` | `string` | Rich text editor content |
| `number` | `field_number.go` | `NUMERIC DEFAULT 0 NOT NULL` | `float64` | Numeric value with optional min/max |
| `bool` | `field_bool.go` | `BOOLEAN DEFAULT FALSE NOT NULL` | `bool` | Boolean true/false |
| `email` | `field_email.go` | `TEXT DEFAULT '' NOT NULL` | `string` | Email with format validation |
| `url` | `field_url.go` | `TEXT DEFAULT '' NOT NULL` | `string` | URL with format validation |
| `date` | `field_date.go` | `TEXT DEFAULT '' NOT NULL` | `types.DateTime` | ISO date string |
| `autodate` | `field_autodate.go` | `TEXT DEFAULT '' NOT NULL` | `types.DateTime` | Auto-set on create/update |
| `select` | `field_select.go` | `TEXT/JSON` | `string/[]string` | Single or multi-select from predefined values |
| `file` | `field_file.go` | `TEXT/JSON` | `string/[]string` | File upload (stores filename, actual file in filesystem) |
| `relation` | `field_relation.go` | `TEXT/JSON` | `string/[]string` | Relation to other collection records |
| `json` | `field_json.go` | `JSON DEFAULT 'null'` | `any` | Arbitrary JSON data |
| `password` | `field_password.go` | `TEXT DEFAULT '' NOT NULL` | `*PasswordFieldValue` | Bcrypt hashed password (always hidden) |
| `geoPoint` | `field_geo_point.go` | `JSON DEFAULT '{"lon":0,"lat":0}'` | `GeoPoint` | Geographic coordinates (lon/lat) |

### 5.3 Field Registration

Fields are registered in a global registry map via `init()`:

```go
var Fields = map[string]FieldFactoryFunc{}

// Example from field_text.go:
func init() {
    Fields[FieldTypeText] = func() Field {
        return &TextField{}
    }
}
```

### 5.4 FieldsList

**File: `core/fields_list.go`**

`FieldsList` is a `[]Field` slice with helper methods:

```go
type FieldsList []Field

func (l FieldsList) GetById(fieldId string) Field
func (l FieldsList) GetByName(fieldName string) Field
func (l *FieldsList) Add(fields ...Field)         // Add or replace by id/name
func (l *FieldsList) RemoveById(fieldId string)
func (l *FieldsList) RemoveByName(fieldName string)
func (l FieldsList) FieldNames() []string
func (l FieldsList) AsMap() map[string]Field
func (l FieldsList) Clone() (FieldsList, error)
```

The `Add` method has smart replace behavior:
- If a field with the same ID exists, it replaces it
- If no matching ID but matching name, it replaces it
- Otherwise appends to the end
- Auto-generates an ID if none is set

### 5.5 RecordInterceptor Pattern

The `RecordInterceptor` interface allows fields to hook into record CRUD operations:

```go
type RecordInterceptor interface {
    Intercept(ctx context.Context, app App, record *Record, actionName string, actionFunc func() error) error
}
```

This is used primarily by the `FileField` to:
- Upload files during create/update execute
- Delete old files after successful save
- Cleanup uploaded files on failure
- Delete files when records are deleted

Action names: `validate`, `create`, `createExecute`, `afterCreate`, `afterCreateError`, `update`, `updateExecute`, `afterUpdate`, `afterUpdateError`, `delete`, `deleteExecute`, `afterDelete`, `afterDeleteError`

### 5.6 Collection Validation

**File: `core/collection_validate.go`**

Collections are validated before save with rules including:
- Name must be unique (case insensitive)
- Name must be valid identifier
- Name cannot conflict with system tables
- Type cannot be changed after creation
- System collections have additional restrictions
- Fields are validated for consistency
- Indexes are validated
- Auth options are validated (if auth type)

### 5.7 Record Table Sync

**File: `core/collection_record_table_sync.go`**

When a collection is created or updated, `SyncRecordTableSchema` ensures the underlying SQLite table matches the collection's field definitions:
- Creates new columns for new fields
- Renames columns for renamed fields
- Drops columns for removed fields
- Updates column types if changed
- Manages indexes

---

## 6. Record CRUD Operations

### 6.1 Record Create Flow (API to DB)

**File: `apis/record_crud.go` -> `forms/record_upsert.go` -> `core/db.go`**

```
1. HTTP POST /api/collections/{collection}/records
2. apis.recordCreate handler:
   a. Find collection by path param
   b. Check rate limit
   c. Parse RequestInfo
   d. Check CreateRule (nil = superuser only)
   e. Create new empty Record: core.NewRecord(collection)
   f. Extract request data (form fields + uploaded files)
   g. Handle OAuth2 context (auto-generate password if needed)
   h. Replace modifier fields in request body
   i. Create RecordUpsert form
   j. Grant superuser/manager access if applicable
   k. form.Load(data) - load data into record
   l. Evaluate CreateRule filter expression against dummy record
   m. Check ManageRule for auth collections
   n. Trigger OnRecordCreateRequest hook
   o. Inside hook: form.Submit()
      - Validates form-specific fields (password, email, verified for auth)
      - Calls app.SaveWithContext(record) which triggers:
        - OnModelCreate -> OnRecordCreate
        - OnModelValidate -> OnRecordValidate
          - PreValidate: auto-generate ID, set created/updated timestamps
          - Field-level validation via each field's ValidateValue()
          - Field interceptors for validate action
        - OnModelCreateExecute -> OnRecordCreateExecute
          - Field interceptors for createExecute (file uploads happen here)
          - record.DBExport(app) -> INSERT INTO table
          - record.MarkAsNotNew()
        - OnModelAfterCreateSuccess -> OnRecordAfterCreateSuccess
          - Field interceptors for afterCreate (file deletions happen here)
   p. EnrichRecord (add expand data, apply visibility rules)
   q. Return JSON 200 with record data
```

### 6.2 Record Update Flow

```
1. HTTP PATCH /api/collections/{collection}/records/{id}
2. apis.recordUpdate handler:
   a. Find collection, check rate limit
   b. Eager fetch record by ID (needed for modifier field resolution)
   c. Extract request data
   d. Apply UpdateRule filter (refetch record with rule constraints)
   e. Create RecordUpsert form
   f. Check ManageRule for auth collections
   g. form.Load(data) - merge new data into existing record
   h. Trigger OnRecordUpdateRequest hook
   i. Inside hook: form.Submit()
      - Same validation chain as create but with OnModelUpdate hooks
      - OnModelUpdateExecute:
        - Field interceptors for updateExecute (upload new files, track old files)
        - record.DBExport(app) -> UPDATE table SET ... WHERE id = ?
      - OnModelAfterUpdateSuccess:
        - Field interceptors for afterUpdate (delete old replaced files)
   j. Return JSON 200 with updated record
```

### 6.3 Record Delete Flow

```
1. HTTP DELETE /api/collections/{collection}/records/{id}
2. apis.recordDelete handler:
   a. Find collection, check rate limit
   b. Apply DeleteRule filter
   c. Find record by ID with rule constraints
   d. Trigger OnRecordDeleteRequest hook
   e. Inside hook: app.Delete(record)
      - OnModelDelete -> OnRecordDelete
      - OnModelDeleteExecute -> OnRecordDeleteExecute
        - DELETE FROM table WHERE id = ?
      - OnModelAfterDeleteSuccess -> OnRecordAfterDeleteSuccess
        - File cleanup hooks (delete all record files from storage)
   f. Return 204 No Content
```

### 6.4 Record List Flow

```
1. HTTP GET /api/collections/{collection}/records?page=1&perPage=20&filter=...&sort=...
2. apis.recordsList handler:
   a. Find collection, check rate limit
   b. Check ListRule (nil = superuser only)
   c. Check for superuser-only filter/sort fields
   d. Build base query: app.RecordQuery(collection)
   e. Apply ListRule as WHERE clause (if non-empty)
   f. Create RecordFieldResolver for filter/sort parsing
   g. Create SearchProvider with the query
   h. Parse URL query params and execute (page, perPage, filter, sort)
   i. Trigger OnRecordsListRequest hook
   j. EnrichRecords (expand relations, apply visibility)
   k. Anti-timing attack throttle for empty filtered results
   l. Return JSON 200 with paginated Result
```

### 6.5 Record View Flow

```
1. HTTP GET /api/collections/{collection}/records/{id}
2. apis.recordView handler:
   a. Find collection, check rate limit
   b. Check ViewRule
   c. Apply ViewRule as additional WHERE clause
   d. FindRecordById with rule constraints
   e. Trigger OnRecordViewRequest hook
   f. EnrichRecord
   g. Return JSON 200 with record
```

### 6.6 RecordUpsert Form

**File: `forms/record_upsert.go`**

The `RecordUpsert` form is the validation/processing layer between API input and `app.Save()`:

```go
type RecordUpsert struct {
    ctx         context.Context
    app         core.App
    record      *core.Record
    accessLevel int  // default, manager, or superuser

    // auth-specific password fields
    password        string
    passwordConfirm string
    oldPassword     string
}
```

Access levels:
- `accessLevelDefault` (0): Normal user access
- `accessLevelManager` (1): Can change email, verified without oldPassword
- `accessLevelSuperuser` (2): Can change all fields including hidden ones

The `Load` method:
1. Iterates over provided data map
2. Skips excluded fields (expand, passwordConfirm, oldPassword)
3. Calls `record.SetIfFieldExists(key, value)` for each field
4. Restores original value if field is hidden and access is not superuser
5. Handles special auth password fields separately

The `Submit` method:
1. Validates form-specific fields (auth password rules)
2. Calls `app.SaveWithContext(record)` which handles all model lifecycle hooks

---

## 7. Admin/Superuser Operations

### 7.1 Superuser Architecture

**File: `core/record_model_superusers.go`**

In PocketBase v0.23+, **there is no separate Admin model**. Superusers are simply records in a special system auth collection named `_superusers`.

```go
const CollectionNameSuperusers = "_superusers"

func (m *Record) IsSuperuser() bool {
    return m.Collection().Name == CollectionNameSuperusers
}
```

### 7.2 Superuser Hooks

System hooks registered for `_superusers` collection:

1. **Delete protection**: Cannot delete the last remaining superuser
2. **Auto-verify**: Superusers are always marked as verified on save
3. **Installer cleanup**: When a new superuser is created, the default installer superuser (`__pbinstaller@example.com`) is automatically deleted
4. **Collection protection**: The `_superusers` collection:
   - Cannot be renamed (name is forced to `_superusers`)
   - Cannot enable OAuth2 (to prevent accidental superuser creation)
   - Password auth is always forced enabled
   - OTP requires MFA to be enabled

### 7.3 Superuser CLI

**File: `cmd/superuser.go`**

CLI commands for managing superusers:
```
./pocketbase superuser create EMAIL PASSWORD
./pocketbase superuser update EMAIL PASSWORD
./pocketbase superuser upsert EMAIL PASSWORD
./pocketbase superuser delete EMAIL
```

### 7.4 Superuser Auth Check in API

Throughout the API handlers, superuser auth is checked via:

```go
requestInfo.HasSuperuserAuth()
// which internally checks:
// requestInfo.Auth != nil && requestInfo.Auth.IsSuperuser()
```

Superusers bypass all collection access rules.

---

## 8. Authentication Flow

### 8.1 Token Architecture

**File: `core/record_tokens.go`**

PocketBase uses **JWT tokens** for authentication with different token types:

| Token Type | Constant | Purpose | Secret Source |
|------------|----------|---------|---------------|
| `auth` | `TokenTypeAuth` | API authentication | `record.TokenKey() + collection.AuthToken.Secret` |
| `file` | `TokenTypeFile` | Protected file access | `record.TokenKey() + collection.FileToken.Secret` |
| `verification` | `TokenTypeVerification` | Email verification | `record.TokenKey() + collection.VerificationToken.Secret` |
| `passwordReset` | `TokenTypePasswordReset` | Password reset | `record.TokenKey() + collection.PasswordResetToken.Secret` |
| `emailChange` | `TokenTypeEmailChange` | Email change confirmation | `record.TokenKey() + collection.EmailChangeToken.Secret` |

#### Token Claims

All tokens contain:
```json
{
    "id": "recordId",
    "type": "tokenType",
    "collectionId": "collectionId",
    "exp": 1234567890
}
```

Additional claims per type:
- **auth**: `"refreshable": true/false`
- **verification/passwordReset/emailChange**: `"email": "user@example.com"`
- **emailChange**: `"newEmail": "new@example.com"`

#### Token Secret Composition

Each token's signing key is: `record.TokenKey() + collection.{Type}Token.Secret`

Where:
- `record.TokenKey()` is a 50-char random string stored per-record (in the `tokenKey` field)
- `collection.{Type}Token.Secret` is a 50-char random string stored per-collection
- Changing either invalidates all existing tokens of that type

### 8.2 Password Authentication Flow

**File: `apis/record_auth_with_password.go`**

```
1. POST /api/collections/{collection}/auth-with-password
2. Body: { "identity": "user@example.com", "password": "secret123", "identityField": "" }
3. Handler:
   a. Find auth collection
   b. Check collection.PasswordAuth.Enabled
   c. Parse and validate form
   d. If identityField specified: search by that specific field
      Else: iterate through collection.PasswordAuth.IdentityFields
        - Prioritizes "email" field for backward compatibility
        - Searches each identity field using unique index with proper collation
   e. Trigger OnRecordAuthWithPasswordRequest hook
   f. Inside hook:
      - Verify record exists and password matches: record.ValidatePassword(password)
      - Call RecordAuthResponse() which:
        1. Checks collection.AuthRule (post-auth filter)
        2. Handles MFA if enabled
        3. Generates auth token: record.NewAuthToken()
        4. Creates auth origin record (device fingerprint)
        5. Sends auth alert email (if new device)
        6. Triggers OnRecordAuthRequest hook
        7. Returns: { "token": "jwt...", "record": {...} }
```

### 8.3 Token Verification Flow

```
FindAuthRecordByToken(token, validTypes):
1. Parse unverified JWT claims
2. Extract: id, collectionId, tokenType
3. Validate token type against allowed types
4. FindRecordById(collectionId, id)
5. Verify collection is auth type
6. Determine base secret from token type
7. Build full secret: record.TokenKey() + baseTokenKey
8. Verify JWT signature with full secret
9. Return record
```

### 8.4 OAuth2 Authentication

**File: `apis/record_auth_with_oauth2.go`**

Flow:
1. Client sends code, provider name, redirectURL, codeVerifier (PKCE)
2. Server exchanges code for OAuth2 token
3. Fetches user info from OAuth2 provider
4. Looks for existing `ExternalAuth` record linking provider user to local record
5. If found: authenticates as that record
6. If not found: creates new record (if CreateRule allows) or links to authenticated user
7. Maps OAuth2 fields to record fields based on `collection.OAuth2.MappedFields`

### 8.5 Multi-Factor Authentication (MFA)

When MFA is enabled on a collection:
1. First auth attempt returns an MFA challenge instead of a token
2. An `_mfas` record is created with a temporary ID
3. Client must complete a second auth method
4. Second auth validates the MFA record and then issues the full auth token

### 8.6 One-Time Password (OTP)

Flow:
1. `POST /api/collections/{collection}/request-otp` with `email`
2. Server generates OTP, stores hashed in `_otps` table, sends via email
3. `POST /api/collections/{collection}/auth-with-otp` with `otpId` and `password`
4. Server verifies OTP and authenticates

---

## 9. File Storage

### 9.1 Filesystem Abstraction

**Package: `tools/filesystem`**

PocketBase supports two storage backends:
- **Local filesystem**: Files stored in `DataDir/storage/`
- **S3-compatible**: Any S3-compatible object storage (AWS S3, MinIO, etc.)

The `System` struct provides a unified interface:

```go
type System struct {
    // Methods:
    Exists(path string) (bool, error)
    Attributes(path string) (*blob.Attributes, error)
    Upload(content []byte, path string) error
    UploadFile(file *File, path string) error
    Delete(path string) error
    DeletePrefix(prefix string) []error
    Serve(w http.ResponseWriter, r *http.Request, path string, name string) error
    CreateThumb(originalPath, thumbPath, thumbSize string) error
    // ...
}
```

### 9.2 File Storage Path Structure

Files are stored with this path pattern:
```
{collectionId}/{recordId}/{filename}
```

Thumbnails:
```
{collectionId}/{recordId}/thumbs_{filename}/{size}_{filename}
```

### 9.3 File Upload Flow

1. File field's `RecordInterceptor.Intercept()` is called during `createExecute`/`updateExecute`
2. `processFilesToUpload()` iterates over new `*filesystem.File` values in the record
3. Each file is uploaded to the filesystem at `record.BaseFilesPath() + "/" + file.Name`
4. On success, file objects are replaced with their string filenames in the record data
5. On failure, already uploaded files are cleaned up

### 9.4 File Naming

New uploaded files get a unique name generated by the framework:
```
{originalName}_{randomSuffix}.{extension}
```

### 9.5 File Download/Serving

**File: `apis/file.go`**

```
GET /api/files/{collection}/{recordId}/{filename}?thumb=100x100&token=...
```

Flow:
1. Find collection and record
2. Find the file field that contains the filename
3. If field is `Protected`:
   - Validate file token from `?token=` query param
   - Check ViewRule against the token's auth record
4. Create filesystem instance
5. Check for thumb size parameter
6. If valid thumb requested and file is image:
   - Check if thumb already exists
   - If not, generate thumb with concurrency control (semaphore)
7. Serve file with appropriate headers
8. X-Frame-Options is unset to allow embedding

Thumbnail generation uses a `singleflight.Group` to prevent duplicate generation and a `semaphore.Weighted` to limit concurrent generation (default: `NumCPU + 2` workers).

### 9.6 File Token

Protected files require a special file token:
```
POST /api/files/token  (requires auth)
-> Returns: { "token": "jwt_file_token" }
```

The file token is short-lived (default 3 minutes) and scoped to the authenticated user.

---

## 10. Migrations

### 10.1 Migration System Architecture

**File: `core/migrations_runner.go`, `core/migrations_list.go`**

PocketBase has TWO migration lists:
- `SystemMigrations` - Built-in migrations for PocketBase schema (in `migrations/` directory)
- `AppMigrations` - User-defined application migrations

### 10.2 MigrationsList

```go
type MigrationsList struct {
    list []*Migration
}

type Migration struct {
    File             string
    Up               func(app App) error
    Down             func(app App) error
    ReapplyCondition func(app App, runner *MigrationsRunner, fileName string) (bool, error)
}
```

### 10.3 MigrationsRunner

```go
type MigrationsRunner struct {
    app            App
    tableName      string      // default: "_migrations"
    migrationsList MigrationsList
}
```

Commands:
- `up` - Apply all unapplied migrations
- `down [n]` - Revert last n migrations (default 1)
- `history-sync` - Sync migrations table with available migrations list

### 10.4 Migration Execution

The `Up()` method:
1. Wraps all migrations in a dual transaction (both data.db and auxiliary.db)
2. For each migration in order:
   - Check if already applied (exists in `_migrations` table)
   - If applied and has `ReapplyCondition`, evaluate whether to reapply
   - Execute `migration.Up(txApp)` within the transaction
   - Record in `_migrations` table: `{file: name, applied: timestamp}`
3. All or nothing - if any migration fails, all are rolled back

### 10.5 Built-in Migrations

Located in `migrations/`:

```
1640988000_init.go             - Initial schema (creates _collections, _params, _externalAuths, _superusers, etc.)
1640988000_aux_init.go         - Auxiliary DB schema (creates _logs table)
1717233556_v0.23_migrate.go    - v0.23 schema migration
1717233557_v0.23_migrate2.go   - v0.23 continuation
1717233558_v0.23_migrate3.go   - v0.23 continuation
1717233559_v0.23_migrate4.go   - v0.23 continuation
1763020353_update_default_auth_alert_templates.go - Template update
```

### 10.6 Initial Schema

The init migration creates these system tables:

```sql
-- _collections table
CREATE TABLE _collections (
    id TEXT PRIMARY KEY NOT NULL,
    type TEXT DEFAULT 'base' NOT NULL,
    name TEXT UNIQUE NOT NULL,
    system BOOLEAN DEFAULT FALSE NOT NULL,
    fields JSON DEFAULT '[]' NOT NULL,
    indexes JSON DEFAULT '[]' NOT NULL,
    listRule TEXT,
    viewRule TEXT,
    createRule TEXT,
    updateRule TEXT,
    deleteRule TEXT,
    options JSON DEFAULT '{}' NOT NULL,
    created TEXT DEFAULT '' NOT NULL,
    updated TEXT DEFAULT '' NOT NULL
);

-- _params table (stores settings)
CREATE TABLE _params (
    id TEXT PRIMARY KEY NOT NULL,
    value JSON DEFAULT NULL,
    created TEXT DEFAULT '' NOT NULL,
    updated TEXT DEFAULT '' NOT NULL
);

-- _externalAuths table
-- _mfas table
-- _otps table
-- _authOrigins table
-- _superusers table (auth collection records table)
-- _migrations table
```

---

## 11. Hook/Event System

### 11.1 Hook Architecture

**File: `tools/hook/hook.go`**

The hook system is a typed, priority-ordered, chain-of-responsibility pattern:

```go
type Hook[T Resolver] struct {
    handlers []*Handler[T]
    mu       sync.RWMutex
}

type Handler[T Resolver] struct {
    Func     func(T) error
    Id       string
    Priority int    // lower = executes first
}
```

Key operations:
- `Bind(handler)` - Register/replace handler (sorted by priority)
- `BindFunc(fn)` - Shorthand for binding a function
- `Unbind(ids...)` - Remove handlers by ID
- `Trigger(event, finalizer)` - Execute handler chain

### 11.2 Chain Execution (Resolver Pattern)

Each event implements the `Resolver` interface (via embedding `hook.Event`):
```go
type Resolver interface {
    Next() error
}
```

**Critical**: Handlers MUST call `e.Next()` to continue the chain. If they don't call `e.Next()`, subsequent handlers and the finalizer are NOT executed.

```go
h.BindFunc(func(e *MyEvent) error {
    // before logic
    err := e.Next()  // execute remaining chain
    // after logic
    return err
})
```

### 11.3 Model/Record/Collection Hook Hierarchy

PocketBase uses a THREE-LEVEL hook hierarchy:

```
Model hooks (generic, fire for ALL models)
  -> Record hooks (fire for Record models only)
  -> Collection hooks (fire for Collection models only)
```

**Model-level hooks** (on BaseApp):
```go
OnModelValidate()
OnModelCreate()
OnModelCreateExecute()
OnModelAfterCreateSuccess()
OnModelAfterCreateError()
OnModelUpdate()
OnModelUpdateExecute()
OnModelAfterUpdateSuccess()
OnModelAfterUpdateError()
OnModelDelete()
OnModelDeleteExecute()
OnModelAfterDeleteSuccess()
OnModelAfterDeleteError()
```

**Record-level hooks** (automatically triggered by model hooks when model is a Record):
```go
OnRecordValidate()
OnRecordCreate()
OnRecordCreateExecute()
OnRecordAfterCreateSuccess()
OnRecordAfterCreateError()
OnRecordUpdate()
OnRecordUpdateExecute()
// ... same pattern
```

**Collection-level hooks** (triggered when model is a Collection):
```go
OnCollectionValidate()
OnCollectionCreate()
OnCollectionCreateExecute()
// ... same pattern
```

### 11.4 Tag-based Hook Binding

Hooks support optional tag-based filtering:

```go
// Fire for ALL records
app.OnRecordCreate().BindFunc(func(e *RecordEvent) error { ... })

// Fire only for "posts" collection records
app.OnRecordCreate("posts").BindFunc(func(e *RecordEvent) error { ... })

// Fire for "posts" OR "comments" collection records
app.OnRecordCreate("posts", "comments").BindFunc(func(e *RecordEvent) error { ... })
```

Tags are matched against `record.HookTags()` which returns `[collectionId, collectionName]`.

### 11.5 Request-level Hooks

```go
OnRecordsListRequest()
OnRecordViewRequest()
OnRecordCreateRequest()
OnRecordUpdateRequest()
OnRecordDeleteRequest()
OnRecordAuthRequest()
OnRecordAuthWithPasswordRequest()
OnRecordAuthWithOAuth2Request()
// ... etc
```

### 11.6 App Lifecycle Hooks

```go
OnBootstrap()       // App initialization
OnTerminate()       // App shutdown
OnServe()           // HTTP server start
OnSettingsReload()  // Settings reloaded
```

---

## 12. Settings System

### 12.1 Settings Storage

**File: `core/settings_model.go`, `core/settings_query.go`**

Settings are stored as a single JSON blob in the `_params` table with key `"settings"`:

```go
type Settings struct {
    SMTP         SMTPConfig
    Backups      BackupsConfig
    S3           S3Config
    Meta         MetaConfig
    RateLimits   RateLimitsConfig
    TrustedProxy TrustedProxyConfig
    Batch        BatchConfig
    Logs         LogsConfig
}
```

Settings can be optionally encrypted using an environment variable key (32 characters).

### 12.2 Settings Access

```go
app.Settings()                    // returns current cached *Settings
app.ReloadSettings()              // reload from DB
app.Save(app.Settings())          // persist changes
```

### 12.3 Settings API

```
GET   /api/settings               -> returns settings (sensitive fields masked)
PATCH /api/settings               -> updates settings (superuser only)
POST  /api/settings/test-s3       -> test S3 connection
POST  /api/settings/test-email    -> send test email
POST  /api/settings/apple-client-secret -> generate Apple client secret
```

---

## 13. Realtime/SSE

### 13.1 Architecture

**File: `apis/realtime.go`**

PocketBase implements Server-Sent Events (SSE) for realtime updates:

```
GET  /api/realtime    -> Establish SSE connection
POST /api/realtime    -> Set subscriptions
```

### 13.2 Connection Flow

1. Client opens SSE connection via GET `/api/realtime`
2. Server creates a new `subscriptions.Client` with unique ID
3. Registers client with `app.SubscriptionsBroker()`
4. Sends initial "connect" message with client ID
5. Keeps connection alive with periodic "ping" messages
6. Client sends POST to set subscriptions (collection names, record IDs)

### 13.3 Subscription Topics

Clients can subscribe to:
- Collection-level: `collectionName` (e.g., `"posts"`)
- Record-level: `collectionName/recordId` (e.g., `"posts/abc123"`)

### 13.4 Event Broadcasting

When records are created/updated/deleted, the system hooks broadcast events to all subscribed clients:
- Checks each client's subscriptions
- Evaluates collection access rules against the client's auth
- Sends only allowed data (respecting field visibility, expand rules)

---

## 14. Key Design Patterns for Python Replication

### 14.1 Central App Instance

Everything revolves around a single `App` instance that provides:
- Database access (dual DB: data + auxiliary)
- Settings management
- Filesystem access
- Hook/event system
- Collection cache
- Subscription broker
- Logger
- Cron scheduler

**Python equivalent**: A central application class with dependency injection.

### 14.2 Dynamic Record Schema

Records do NOT use fixed struct fields. Instead:
- Collection defines the schema (field types, rules)
- Record stores data in a generic dictionary
- Field types provide validation, serialization, and DB column definitions

**Python equivalent**: Use dictionaries or a dynamic model class. Consider SQLAlchemy with dynamic columns or raw SQL with dict-based record handling.

### 14.3 Hook System with Chain of Responsibility

The hook system is the core extensibility mechanism:
- Priority-ordered handlers
- Each handler MUST call `next()` to continue the chain
- Supports tag-based filtering
- Three-level hierarchy (Model -> Record/Collection -> specific collection)

**Python equivalent**: A priority-sorted list of callables with a chain runner. Consider using a class-based approach with `__call__` and explicit `next()`.

### 14.4 Unified Save/Delete (No DAO)

There is NO separate DAO layer. The App itself provides `Save()` and `Delete()`:
- `Save()` auto-detects create vs update based on `IsNew()`
- All operations trigger hooks at multiple points in the lifecycle
- Supports transactions with deferred after-completion callbacks

**Python equivalent**: Repository pattern on the App class, or SQLAlchemy session-like approach.

### 14.5 Filter Expression Language

PocketBase has its own filter expression language for access rules and search:
```
title ~ "test" && status = "active" && @request.auth.id != ""
```

This is parsed by a custom tokenizer and converted to SQL WHERE clauses via `RecordFieldResolver`.

**Python equivalent**: Build a parser (consider using `lark` or `pyparsing`) that converts filter expressions to SQLAlchemy filters or raw SQL.

### 14.6 Collection-per-Table Mapping

Each collection has its own SQLite table:
- Table name = collection name
- Columns = collection fields
- Schema sync on collection save (add/rename/drop columns)

**Python equivalent**: Dynamic SQL DDL execution. Create/alter tables based on collection definitions.

### 14.7 File Field as RecordInterceptor

Files are NOT handled by a separate service. Instead, the `FileField` hooks into the record lifecycle:
- Uploads during `createExecute`/`updateExecute`
- Cleanup on failure
- Deletion during `afterCreate`/`afterUpdate` (for replaced files)
- Full cleanup on record delete

**Python equivalent**: Field-level interceptors or signals that fire during the record save/delete lifecycle.

### 14.8 JWT Token Composition

Token signing keys are composed of: `record.tokenKey + collection.typeToken.secret`

This means:
- Changing the record's tokenKey invalidates ALL that record's tokens
- Changing the collection's token secret invalidates ALL tokens of that type for ALL records in the collection

### 14.9 Access Rule Evaluation

Rules are evaluated as SQL WHERE clauses:
```go
resolver := NewRecordFieldResolver(app, collection, requestInfo, true)
expr, _ := search.FilterData(*rule).BuildExpr(resolver)
query.AndWhere(expr)
```

The resolver translates high-level field references (including `@request.auth.id`, `@request.body.field`, relation traversal) into SQL expressions.

### 14.10 Key External Dependencies

| Go Package | Purpose | Python Equivalent |
|------------|---------|-------------------|
| `modernc.org/sqlite` | SQLite driver (pure Go) | `sqlite3` (stdlib) or `aiosqlite` |
| `github.com/pocketbase/dbx` | SQL query builder | SQLAlchemy Core or raw SQL builder |
| `github.com/golang-jwt/jwt/v5` | JWT tokens | `PyJWT` |
| `github.com/go-ozzo/ozzo-validation/v4` | Validation framework | `pydantic`, `marshmallow`, or custom |
| `github.com/spf13/cobra` | CLI framework | `click` or `argparse` |
| `github.com/spf13/cast` | Type casting | Custom or existing casting lib |
| `golang.org/x/crypto/bcrypt` | Password hashing | `bcrypt` package |
| `golang.org/x/oauth2` | OAuth2 client | `authlib` or `requests-oauthlib` |
| `gocloud.dev/blob` | Cloud storage abstraction | `boto3` (S3) + local FS |
| `golang.org/x/crypto/acme/autocert` | Auto TLS certificates | `certbot` or manual |

### 14.11 System Collections

These collections are created during initial migration and marked as `system: true`:

| Collection Name | Type | Purpose |
|----------------|------|---------|
| `_superusers` | auth | Superuser/admin accounts |
| `_externalAuths` | base | OAuth2 linked accounts |
| `_mfas` | base | MFA challenge records |
| `_otps` | base | OTP records |
| `_authOrigins` | base | Auth device fingerprints |

System collections cannot be deleted or renamed. Their rules cannot be changed by non-superusers.

### 14.12 Concurrency Model

- SQLite WAL mode allows concurrent reads
- Write operations use a nonconcurrent DB connection (serialized)
- Lock retry mechanism with exponential backoff (max 8 retries)
- Thread-safe in-memory stores (`tools/store`) using `sync.RWMutex`
- Goroutine pools for background tasks (`tools/routine`)
- Semaphore for thumbnail generation concurrency control

**Python equivalent**: Consider `asyncio` with `aiosqlite` for async I/O, or thread pools with proper SQLite connection management. Use `threading.Lock` for shared state.
