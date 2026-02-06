# PocketBase Data Models & Database Layer - Deep Source Analysis

> Comprehensive analysis of PocketBase's Go source code (v0.23+) from the `pocketbase/pocketbase` GitHub repository.
> Source files analyzed from the `core/` package and `migrations/` directory.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Base Model](#2-base-model)
3. [Collection Model](#3-collection-model)
4. [Record Model](#4-record-model)
5. [Field System](#5-field-system)
6. [All Field Types Detailed](#6-all-field-types-detailed)
7. [Auth Collection Options](#7-auth-collection-options)
8. [System Collections](#8-system-collections)
9. [Database Layer & Save/Delete Mechanics](#9-database-layer--savedelete-mechanics)
10. [Collection Table Sync / Schema Migration](#10-collection-table-sync--schema-migration)
11. [Query Layer](#11-query-layer)
12. [Supporting Models](#12-supporting-models)
13. [SQLite Schema Definitions](#13-sqlite-schema-definitions)
14. [Python/SQLAlchemy Replication Guide](#14-pythonsqlalchemy-replication-guide)

---

## 1. Architecture Overview

PocketBase v0.23+ consolidates everything into the `core/` package. The older `models/` and `daos/` directories no longer exist. Key architectural patterns:

- **No traditional DAO layer** - The `App` interface methods (Save, Delete, RecordQuery, etc.) directly handle persistence
- **Hook-driven lifecycle** - All model operations fire through a chain of hooks (OnModelCreate, OnModelValidate, etc.)
- **Dynamic schema** - Collection fields are stored as JSON in the `_collections` table; record tables are dynamically created/altered
- **DBExporter pattern** - Models implement `DBExport(app) (map[string]any, error)` to produce the map used for INSERT/UPDATE
- **SQLite-native** - Uses SQLite with `dbx` (a thin Go SQL query builder), leveraging JSON functions, PRAGMA, and TEXT-typed columns for most things
- **Two databases** - A primary `data.db` and an auxiliary `auxiliary.db` (for logs)

### Source Files Map

| File | Purpose |
|------|---------|
| `core/db_model.go` | BaseModel (id, IsNew) |
| `core/collection_model.go` | Collection struct, hooks, init defaults |
| `core/record_model.go` | Record struct, dynamic field store, hooks |
| `core/field.go` | Field interface, common field names/constants |
| `core/fields_list.go` | FieldsList (ordered, JSON-serializable field collection) |
| `core/field_*.go` | Individual field type implementations |
| `core/db.go` | Save/Delete/Validate + id generation |
| `core/db_table.go` | Table operations (create, drop, info) |
| `core/collection_query.go` | Collection finder/query methods |
| `core/record_query.go` | Record finder/query methods |
| `core/collection_record_table_sync.go` | Dynamic table CREATE/ALTER logic |
| `core/collection_model_auth_options.go` | Auth collection config structs |
| `migrations/1640988000_init.go` | System table/collection SQL definitions |

---

## 2. Base Model

**Source: `core/db_model.go`**

```go
type Model interface {
    TableName() string
    PK() any
    LastSavedPK() any
    IsNew() bool
    MarkAsNew()
    MarkAsNotNew()
}

type BaseModel struct {
    lastSavedPK string   // unexported, tracks if record exists in DB
    Id          string   `db:"id" json:"id"`
}
```

### Key behaviors:
- `IsNew()` returns `true` when `lastSavedPK == ""` (determines INSERT vs UPDATE)
- `MarkAsNotNew()` copies `Id` to `lastSavedPK` (called after PostScan / successful save)
- `PostScan()` is called after loading from DB rows, calls `MarkAsNotNew()`
- **ID format**: 15-character lowercase alphanumeric by default (`[a-z0-9]{15}`)
- **ID generation**: `DefaultIdAlphabet = "abcdefghijklmnopqrstuvwxyz0123456789"`, length 15

### Python equivalent:
```python
# Base mixin for all models
class BaseModel:
    id: str  # TEXT PRIMARY KEY, 15-char random alphanumeric
    # Internal tracking of saved state handled by SQLAlchemy session
```

---

## 3. Collection Model

**Source: `core/collection_model.go`**

### Collection Types
```go
const (
    CollectionTypeBase = "base"
    CollectionTypeAuth = "auth"
    CollectionTypeView = "view"
)
```

### Core Struct

```go
type baseCollection struct {
    BaseModel

    ListRule   *string         `db:"listRule"`    // nil = superusers only
    ViewRule   *string         `db:"viewRule"`
    CreateRule *string         `db:"createRule"`
    UpdateRule *string         `db:"updateRule"`
    DeleteRule *string         `db:"deleteRule"`

    RawOptions types.JSONRaw   `db:"options"`     // serialized type-specific options
    Name       string          `db:"name"`        // also used as the record table name
    Type       string          `db:"type"`        // "base", "auth", or "view"
    Fields     FieldsList      `db:"fields"`      // JSON array of field definitions
    Indexes    types.JSONArray[string] `db:"indexes"` // JSON array of CREATE INDEX statements
    Created    types.DateTime  `db:"created"`
    Updated    types.DateTime  `db:"updated"`
    System     bool            `db:"system"`      // prevents rename/delete/rule change
}

type Collection struct {
    baseCollection
    collectionAuthOptions    // auth-specific (tokens, OAuth2, MFA, etc.)
    collectionViewOptions    // view-specific (ViewQuery)
}
```

### Table Name
```go
func (m *Collection) TableName() string { return "_collections" }
```

### Collection ID Generation
- Auto-generated as `"pbc_" + crc32Checksum(type + name)`
- Checked for uniqueness; if collision, appends incrementing number

### Rules System
- Rules are `*string` (pointer to string):
  - `nil` = only superusers can access (locked)
  - `""` (empty string) = everyone can access (public)
  - `"some_filter_expression"` = conditional access
- Rule expressions use PocketBase's filter syntax (e.g., `"@request.auth.id != ''"`)

### Default Fields Initialization
On `NewBaseCollection()`:
- Adds `id` field (TextField, primaryKey=true, system=true, min=15, max=15, pattern=`^[a-z0-9]+$`, autogenerate=`[a-z0-9]{15}`)

On `NewAuthCollection()` - adds all base fields plus:
- `password` (PasswordField, system=true, hidden=true, required=true, min=8)
- `tokenKey` (TextField, system=true, hidden=true, required=true, min=30, max=60, autogenerate=`[a-zA-Z0-9]{50}`) + UNIQUE index
- `email` (EmailField, system=true, required=true) + UNIQUE index (with WHERE email != '')
- `emailVisibility` (BoolField, system=true)
- `verified` (BoolField, system=true)

### DBExport
The `DBExport` method produces a flat map for persistence:
```go
map[string]any{
    "id", "type", "listRule", "viewRule", "createRule",
    "updateRule", "deleteRule", "name", "fields", "indexes",
    "system", "created", "updated", "options"  // options = serialized auth/view config
}
```

---

## 4. Record Model

**Source: `core/record_model.go`**

### Struct

```go
type Record struct {
    collection       *Collection
    originalData     map[string]any           // snapshot after DB load
    customVisibility *store.Store[string, bool]
    data             *store.Store[string, any] // current field values (changes)
    expand           *store.Store[string, any] // expanded relations

    BaseModel  // embeds Id, lastSavedPK

    exportCustomData      bool
    ignoreEmailVisibility bool
    ignoreUnchangedFields bool
}
```

### Key Design Patterns

1. **Dynamic field storage**: Record uses `map[string]any` stores, NOT Go struct fields. The collection's `Fields` list determines what's valid.

2. **Two-layer data**: `originalData` holds the DB-loaded values; `data` holds mutations. `GetRaw(key)` checks `data` first, falls back to `originalData`.

3. **Table name is collection name**: `func (m *Record) TableName() string { return m.collection.Name }`

4. **Set/Get with field normalization**:
   - `Set(key, value)` looks for a matching Field via `SetIfFieldExists`, which calls `field.PrepareValue()` or custom `SetterFinder.FindSetter(key)`
   - `Get(key)` looks for custom `GetterFinder.FindGetter(key)`, falls back to `GetRaw(key)`

5. **Modifier keys**: Fields can support modifiers like `"field+"`, `"field-"`, `"+field"`, `"field:autogenerate"`, etc.

6. **DBExport for persistence**:
   - Iterates collection fields, calls `field.DriverValue(record)` if available, else `record.GetRaw(fieldName)`
   - Optionally skips unchanged fields (`ignoreUnchangedFields`)

7. **Validation**: On save, each field's `ValidateValue(ctx, app, record)` is called. Errors are collected as `validation.Errors` map keyed by field name.

8. **Cascade delete**: When a record is deleted, the system finds all RelationField references and either cascade-deletes or unsets the relation.

### Helper Methods
```go
record.GetString(key)    // cast to string
record.GetInt(key)       // cast to int
record.GetFloat(key)     // cast to float64
record.GetBool(key)      // cast to bool
record.GetDateTime(key)  // parse to types.DateTime
record.GetGeoPoint(key)  // parse to types.GeoPoint
record.GetStringSlice(key) // cast to []string (unique, non-zero)
```

### Auth Record Methods
```go
record.Email()             // shorthand for GetString("email")
record.SetEmail(email)
record.Verified()          // GetBool("verified")
record.SetVerified(bool)
record.TokenKey()          // GetString("tokenKey")
record.SetPassword(pass)   // hashes with bcrypt
record.ValidatePassword(pass) // bcrypt compare
record.RefreshTokenKey()   // regenerate random tokenKey
```

---

## 5. Field System

**Source: `core/field.go`, `core/fields_list.go`**

### Field Interface

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
    ColumnType(app App) string              // SQLite column DDL
    PrepareValue(record *Record, raw any) (any, error)  // normalize raw input
    ValidateValue(ctx context.Context, app App, record *Record) error
    ValidateSettings(ctx context.Context, app App, collection *Collection) error
}
```

### Optional Field Interfaces

| Interface | Purpose |
|-----------|---------|
| `SetterFinder` | Custom setter via `FindSetter(key) SetterFunc` (e.g., `"field+"` modifier) |
| `GetterFinder` | Custom getter via `FindGetter(key) GetterFunc` (e.g., `"password:hash"`) |
| `DriverValuer` | Custom DB export via `DriverValue(record) (driver.Value, error)` |
| `MultiValuer` | Check if field supports multiple values via `IsMultiple() bool` |
| `RecordInterceptor` | Hook into record lifecycle via `Intercept(ctx, app, record, actionName, actionFunc)` |
| `MaxBodySizeCalculator` | Report max body size for file upload limits |

### Common Field Properties (shared by all types)

| Property | Type | Description |
|----------|------|-------------|
| `Name` | string | Unique field name (required) |
| `Id` | string | Stable field identifier (auto-generated from name if empty) |
| `System` | bool | Prevents renaming/removal |
| `Hidden` | bool | Hides from API response |
| `Presentable` | bool | UI hint for relation preview labels |

### FieldsList

`FieldsList` is `[]Field` with:
- JSON serialization that includes `"type"` discriminator
- Field factory registry: `Fields map[string]FieldFactoryFunc`
- Add/replace by id or name, insert at position
- Auto-generates field IDs as `fieldType + crc32(fieldName)`

### Field Registration
Each field type registers itself in `init()`:
```go
func init() {
    Fields[FieldTypeText] = func() Field { return &TextField{} }
}
```

---

## 6. All Field Types Detailed

### 6.1 TextField (`"text"`)

**Source: `core/field_text.go`**

```go
type TextField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    Min          int      // min string length (0 = no limit)
    Max          int      // max string length (0 = default 5000)
    Pattern      string   // optional regex
    AutogeneratePattern string // regex-based random generation on create
    Required     bool
    PrimaryKey   bool     // marks as primary key (only 1 per collection)
}
```

| Property | SQLite Column | Go Value | Default |
|----------|--------------|----------|---------|
| Regular | `TEXT DEFAULT '' NOT NULL` | `string` | `""` |
| PrimaryKey | `TEXT PRIMARY KEY DEFAULT ('r'\|\|lower(hex(randomblob(7)))) NOT NULL` | `string` | auto-generated |

**Validation**: Required, min/max length (rune count), regex pattern, PK uniqueness (case-insensitive for custom patterns), forbidden PK characters.

**Setters**: `"name"` (direct), `"name:autogenerate"` (append random to value)

**Interceptor**: On create/validate, if `AutogeneratePattern` set and value is empty, generates random value.

---

### 6.2 NumberField (`"number"`)

**Source: `core/field_number.go`**

```go
type NumberField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    Min          *float64   // nil = no min
    Max          *float64   // nil = no max
    OnlyInt      bool       // require integer values
    Required     bool       // require non-zero
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `NUMERIC DEFAULT 0 NOT NULL` | `float64` | `0` |

**Validation**: NaN/Inf check, OnlyInt (`val != float64(int64(val))`), min/max bounds.

**Setters**: `"name"` (set), `"name+"` (add), `"name-"` (subtract)

---

### 6.3 BoolField (`"bool"`)

**Source: `core/field_bool.go`**

```go
type BoolField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    Required     bool    // if true, value MUST be true
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `BOOLEAN DEFAULT FALSE NOT NULL` | `bool` | `false` |

---

### 6.4 EmailField (`"email"`)

**Source: `core/field_email.go`**

```go
type EmailField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    ExceptDomains []string  // blocklist
    OnlyDomains   []string  // allowlist (mutually exclusive with ExceptDomains)
    Required     bool
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `TEXT DEFAULT '' NOT NULL` | `string` | `""` |

**Validation**: Email format, domain allowlist/blocklist.

---

### 6.5 URLField (`"url"`)

**Source: `core/field_url.go`**

```go
type URLField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    ExceptDomains []string
    OnlyDomains   []string
    Required     bool
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `TEXT DEFAULT '' NOT NULL` | `string` | `""` |

**Validation**: URL format, host allowlist/blocklist.

---

### 6.6 DateField (`"date"`)

**Source: `core/field_date.go`**

```go
type DateField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    Min          types.DateTime  // zero = no min
    Max          types.DateTime  // zero = no max
    Required     bool
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `TEXT DEFAULT '' NOT NULL` | `types.DateTime` | zero DateTime |

**DateTime format**: `"2006-01-02 15:04:05.000Z"` (stored as TEXT in SQLite)

---

### 6.7 AutodateField (`"autodate"`)

**Source: `core/field_autodate.go`**

```go
type AutodateField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    OnCreate     bool   // auto-set on record create
    OnUpdate     bool   // auto-set on record update
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `TEXT DEFAULT '' NOT NULL` | `types.DateTime` | zero DateTime |

**Interceptor**: Sets `types.NowDateTime()` on create/update execute. Setter is a no-op (prevents manual changes via `Set()`; use `SetRaw()` to override).

---

### 6.8 SelectField (`"select"`)

**Source: `core/field_select.go`**

```go
type SelectField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    Values       []string   // allowed values (required)
    MaxSelect    int        // >1 = multiple; 0 or 1 = single
    Required     bool
}
```

| Mode | SQLite Column | Go Value | Default |
|------|--------------|----------|---------|
| Single (MaxSelect <= 1) | `TEXT DEFAULT '' NOT NULL` | `string` | `""` |
| Multiple (MaxSelect > 1) | `JSON DEFAULT '[]' NOT NULL` | `[]string` | `[]` |

**Validation**: Max count, values must be in allowed `Values` list.

**Setters**: `"name"` (set), `"name+"` (append), `"+name"` (prepend), `"name-"` (subtract)

---

### 6.9 RelationField (`"relation"`)

**Source: `core/field_relation.go`**

```go
type RelationField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    CollectionId  string   // id of the related collection (required)
    CascadeDelete bool     // delete this record if all relations removed
    MinSelect     int      // min required relations
    MaxSelect     int      // >1 = multiple; 0 or 1 = single
    Required      bool
}
```

| Mode | SQLite Column | Go Value | Default |
|------|--------------|----------|---------|
| Single (MaxSelect <= 1) | `TEXT DEFAULT '' NOT NULL` | `string` (record id) | `""` |
| Multiple (MaxSelect > 1) | `JSON DEFAULT '[]' NOT NULL` | `[]string` (record ids) | `[]` |

**Validation**: Record existence check against related collection, min/max count.

**CascadeDelete**: When a related record is deleted, if `CascadeDelete=true` and no other relations remain, the referencing record is also deleted. If `Required=true` and relations drop to zero, deletion is blocked.

**Constraint**: Only view collections can have relations to other view collections. CollectionId cannot be changed after creation.

**Setters**: `"name"` (set), `"name+"` (append), `"+name"` (prepend), `"name-"` (subtract)

---

### 6.10 FileField (`"file"`)

**Source: `core/field_file.go`**

```go
type FileField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    MaxSize      int64     // max file size in bytes (0 = 5MB default)
    MaxSelect    int       // >1 = multiple; 0 or 1 = single
    MimeTypes    []string  // allowed MIME types (empty = all)
    Thumbs       []string  // thumbnail specs: "WxH", "WxHt", "WxHb", "WxHf", "0xH", "Wx0"
    Protected    bool      // require file token to access
    Required     bool
}
```

| Mode | SQLite Column | Go Value | Default |
|------|--------------|----------|---------|
| Single | `TEXT DEFAULT '' NOT NULL` | `string` (filename) | `""` |
| Multiple | `JSON DEFAULT '[]' NOT NULL` | `[]string` (filenames) | `[]` |

**Storage path**: `{collectionId}/{recordId}/{filename}`

**Interceptor**: Handles file upload on create/update execute, cleanup on failure, deletion of removed files after success.

**Setters**: `"name"` (set), `"name+"` (append), `"+name"` (prepend), `"name-"` (subtract/delete)
**Getters**: `"name"` (raw value), `"name:unsaved"` (uploaded `*filesystem.File` objects)

---

### 6.11 JSONField (`"json"`)

**Source: `core/field_json.go`**

```go
type JSONField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    MaxSize      int64   // max bytes (0 = 1MB default)
    Required     bool    // non-empty JSON (not null, "", [], {})
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `JSON DEFAULT NULL` | `types.JSONRaw` (`[]byte`) | `nil` |

**PrepareValue**: Handles smart string normalization: `"true"` -> JSON true, numeric strings -> JSON numbers, plain strings -> quoted JSON strings, etc.

---

### 6.12 EditorField (`"editor"`)

**Source: `core/field_editor.go`**

```go
type EditorField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    MaxSize      int64   // max bytes (0 = 5MB default)
    ConvertURLs  bool    // hint for TinyMCE URL handling
    Required     bool
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `TEXT DEFAULT '' NOT NULL` | `string` | `""` |

---

### 6.13 PasswordField (`"password"`)

**Source: `core/field_password.go`**

```go
type PasswordField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    Pattern      string    // optional regex for plain password
    Min          int       // min plain password length
    Max          int       // max plain password length (0 = 71, bcrypt limit)
    Cost         int       // bcrypt cost (0 = bcrypt.DefaultCost)
    Required     bool
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `TEXT DEFAULT '' NOT NULL` | `*PasswordFieldValue` | `{Hash: ""}` |

**Internal value type**:
```go
type PasswordFieldValue struct {
    LastError error
    Hash      string    // bcrypt hash (stored in DB)
    Plain     string    // plain text (transient, cleared after save)
}
```

**Setter**: Hashes plain password with bcrypt on `Set()`. `SetRaw()` can set a bcrypt hash directly.
**Getter**: `"password"` returns plain text (empty after save), `"password:hash"` returns bcrypt hash.
**DriverValue**: Returns only the hash for DB persistence.

---

### 6.14 GeoPointField (`"geoPoint"`)

**Source: `core/field_geo_point.go`**

```go
type GeoPointField struct {
    Name, Id     string
    System, Hidden, Presentable bool
    Required     bool   // non-zero coordinates (not "Null Island")
}
```

| SQLite Column | Go Value | Default |
|--------------|----------|---------|
| `JSON DEFAULT '{"lon":0,"lat":0}' NOT NULL` | `types.GeoPoint` | `{Lon:0, Lat:0}` |

**Validation**: Lat must be -90 to 90, Lon must be -180 to 180.

---

### Field Type Summary Table

| Type | FieldType Const | SQLite Column (single) | Go Type | Default Value |
|------|----------------|----------------------|---------|---------------|
| `text` | `"text"` | `TEXT DEFAULT '' NOT NULL` | `string` | `""` |
| `number` | `"number"` | `NUMERIC DEFAULT 0 NOT NULL` | `float64` | `0` |
| `bool` | `"bool"` | `BOOLEAN DEFAULT FALSE NOT NULL` | `bool` | `false` |
| `email` | `"email"` | `TEXT DEFAULT '' NOT NULL` | `string` | `""` |
| `url` | `"url"` | `TEXT DEFAULT '' NOT NULL` | `string` | `""` |
| `date` | `"date"` | `TEXT DEFAULT '' NOT NULL` | `types.DateTime` | zero |
| `autodate` | `"autodate"` | `TEXT DEFAULT '' NOT NULL` | `types.DateTime` | zero |
| `select` | `"select"` | `TEXT`/`JSON` | `string`/`[]string` | `""`/`[]` |
| `relation` | `"relation"` | `TEXT`/`JSON` | `string`/`[]string` | `""`/`[]` |
| `file` | `"file"` | `TEXT`/`JSON` | `string`/`[]string` | `""`/`[]` |
| `json` | `"json"` | `JSON DEFAULT NULL` | `types.JSONRaw` | `nil` |
| `editor` | `"editor"` | `TEXT DEFAULT '' NOT NULL` | `string` | `""` |
| `password` | `"password"` | `TEXT DEFAULT '' NOT NULL` | `*PasswordFieldValue` | `""` hash |
| `geoPoint` | `"geoPoint"` | `JSON DEFAULT '{"lon":0,"lat":0}' NOT NULL` | `types.GeoPoint` | `{0,0}` |

---

## 7. Auth Collection Options

**Source: `core/collection_model_auth_options.go`**

Auth collections embed `collectionAuthOptions` with these key configs:

### Rules
```go
AuthRule   *string   // filter applied after authentication; nil = auth disabled
ManageRule *string   // admin-like access for managing other auth records
```

### Authentication Methods

**PasswordAuth**:
```go
type PasswordAuthConfig struct {
    Enabled        bool
    IdentityFields []string  // fields used as login identity (e.g., ["email"])
}
```

**OAuth2**:
```go
type OAuth2Config struct {
    Enabled      bool
    Providers    []OAuth2ProviderConfig  // provider configs
    MappedFields OAuth2KnownFields      // field mapping for auto-fill
}

type OAuth2ProviderConfig struct {
    PKCE         *bool
    Name         string   // "google", "github", etc.
    ClientId     string
    ClientSecret string
    AuthURL, TokenURL, UserInfoURL string
    DisplayName  string
    Extra        map[string]any
}

type OAuth2KnownFields struct {
    Id, Name, Username, AvatarURL string  // maps to record field names
}
```

**MFA**:
```go
type MFAConfig struct {
    Enabled  bool
    Duration int64   // validity in seconds (default 1800 = 30min)
    Rule     string  // optional filter to apply MFA conditionally
}
```

**OTP**:
```go
type OTPConfig struct {
    Enabled       bool
    Duration      int64          // validity in seconds (default 180 = 3min)
    Length        int            // password length (default 8)
    EmailTemplate EmailTemplate  // email content
}
```

### Token Configurations
```go
type TokenConfig struct {
    Secret   string  // JWT signing secret (30-255 chars)
    Duration int64   // validity in seconds
}
```

Default durations:
| Token Type | Default Duration |
|------------|-----------------|
| AuthToken | 604800 (7 days) |
| PasswordResetToken | 1800 (30 min) |
| EmailChangeToken | 1800 (30 min) |
| VerificationToken | 259200 (3 days) |
| FileToken | 180 (3 min) |

### Auth Alert
```go
type AuthAlertConfig struct {
    Enabled       bool
    EmailTemplate EmailTemplate
}
```

### Email Templates
```go
type EmailTemplate struct {
    Subject string
    Body    string
}
```

---

## 8. System Collections

**Source: `migrations/1640988000_init.go`**

PocketBase creates these system collections on first boot:

### `_superusers` (auth type)
- System: true
- Fields: id, password, tokenKey, email (required, unique), emailVisibility, verified, created, updated
- AuthToken duration: 86400 (1 day)
- Default auth collection with all auth fields

### `users` (auth type)
- ID: `_pb_users_auth_`
- Fields: id, password, tokenKey, email, emailVisibility, verified, name (text, max 255), avatar (file, image types), created, updated
- Rules: owner-based (`"id = @request.auth.id"` for list/view/update/delete, `""` for create)
- OAuth2 mapped fields: name -> name, avatarURL -> avatar

### `_externalAuths` (base type)
- System: true
- Fields: id, collectionRef (text, required), recordRef (text, required), provider (text, required), providerId (text, required), created, updated
- Indexes:
  - UNIQUE on `(collectionRef, recordRef, provider)`
  - UNIQUE on `(collectionRef, provider, providerId)`
- Rules: owner-based

### `_mfas` (base type)
- System: true
- Fields: id, collectionRef, recordRef, method (text, required), created, updated
- Index on `(collectionRef, recordRef)`
- Rules: owner-based

### `_otps` (base type)
- System: true
- Fields: id, collectionRef, recordRef, password (password, hidden, cost=8), sentTo (text, hidden), created, updated
- Index on `(collectionRef, recordRef)`
- Rules: owner-based

### `_authOrigins` (base type)
- System: true
- Fields: id, collectionRef, recordRef, fingerprint (text, required), created, updated
- UNIQUE index on `(collectionRef, recordRef, fingerprint)`
- Rules: owner-based (list, view, delete)

---

## 9. Database Layer & Save/Delete Mechanics

**Source: `core/db.go`**

### Save Flow

```
app.Save(model)
  -> IsNew() ? app.create() : app.update()

app.create():
  1. Fire OnModelCreate hooks
  2. Optionally validate (OnModelValidate -> field.ValidateValue for each field)
  3. Fire OnModelCreateExecute hooks
  4. If model implements DBExporter:
       data = model.DBExport(app)  // produces map[string]any
       db.Insert(tableName, data)
     Else:
       db.Model(model).Insert()    // uses struct tags
  5. model.MarkAsNotNew()
  6. Fire OnModelAfterCreateSuccess (or OnModelAfterCreateError)

app.update():
  1. Fire OnModelUpdate hooks
  2. Optionally validate
  3. Fire OnModelUpdateExecute hooks
  4. If model implements DBExporter:
       data = model.DBExport(app)
       db.Update(tableName, data, {id: lastSavedPK})
     Else:
       db.Model(model).Update()
  5. Fire OnModelAfterUpdateSuccess (or OnModelAfterUpdateError)
```

### Delete Flow

```
app.Delete(model)
  1. Verify model has non-empty LastSavedPK
  2. Fire OnModelDelete hooks
  3. Fire OnModelDeleteExecute hooks
  4. db.Delete(tableName, {id: pk})
  5. Fire OnModelAfterDeleteSuccess (or OnModelAfterDeleteError)
```

### Transaction Support

```go
app.RunInTransaction(func(txApp App) error {
    // txApp wraps the same DB connection in a transaction
    // After-hooks are deferred until transaction completes
    return txApp.Save(record)
})
```

### Lock Retry
All DB operations use `baseLockRetry` with `defaultMaxLockRetries` to handle SQLite's SQLITE_BUSY/SQLITE_LOCKED errors.

### Two Database Architecture
- **data.db** (primary): collections, records, settings, etc.
  - `app.DB()` / `app.NonconcurrentDB()` for writes
  - `app.ConcurrentDB()` for reads
- **auxiliary.db**: logs, request stats
  - `app.AuxDB()` / `app.AuxNonconcurrentDB()`
  - `app.AuxConcurrentDB()`

---

## 10. Collection Table Sync / Schema Migration

**Source: `core/collection_record_table_sync.go`**

When a collection is created or updated, `SyncRecordTableSchema(newCollection, oldCollection)` is called inside a transaction:

### Create (oldCollection is nil)
```sql
-- For each field in the collection:
CREATE TABLE {collectionName} (
    {field1Name} {field1.ColumnType()},
    {field2Name} {field2.ColumnType()},
    ...
);
-- Then create all indexes from collection.Indexes
```

### Update (schema diff)
1. **Drop old indexes** (if fields or indexes changed)
2. **Rename table** (if collection name changed)
3. **Drop deleted columns** (fields removed by id)
4. **Add/rename columns**:
   - New fields: `ALTER TABLE ADD COLUMN {tempName} {columnType}`
   - Renamed fields: `ALTER TABLE RENAME COLUMN {oldName} TO {tempName}`
   - Uses temporary names to avoid collisions during name swaps
   - Final rename from temp to actual name
5. **Normalize single/multiple transitions**:
   - Single -> Multiple: `UPDATE table SET col = json_array(col) WHERE col != ''`
   - Multiple -> Single: `UPDATE table SET col = json_extract(col, '$[#-1]')`
   - Temporarily drops views to avoid reference errors, then restores them
6. **Recreate indexes**

### Delete
- View collections: `DROP VIEW`
- Regular collections: `DROP TABLE`
- Checks for existing relation references first (blocks delete if references exist)

### After Sync
- Runs `PRAGMA optimize` for SQLite
- Reloads cached collections
- Triggers resave of view collections with changed fields

---

## 11. Query Layer

**Source: `core/record_query.go`, `core/collection_query.go`**

### Collection Queries
```go
app.CollectionQuery()                          // SELECT * FROM _collections
app.FindCollectionByNameOrId(nameOrId)         // by id or LOWER(name)
app.FindCachedCollectionByNameOrId(nameOrId)   // from in-memory cache
app.FindAllCollections(types...)               // filter by type(s)
app.FindCollectionReferences(collection)       // find relation fields pointing to collection
app.IsCollectionNameUnique(name, excludeIds)   // case-insensitive check
```

### Record Queries
```go
app.RecordQuery(collectionOrNameOrId)          // SELECT {table}.* FROM {table}
app.FindRecordById(collection, id)
app.FindRecordsByIds(collection, ids)
app.FindAllRecords(collection, exprs...)
app.FindFirstRecordByData(collection, key, value)
app.FindRecordsByFilter(collection, filter, sort, limit, offset, params)
app.FindFirstRecordByFilter(collection, filter, params...)
app.CountRecords(collection, exprs...)
app.FindAuthRecordByToken(token, validTypes...)
app.FindAuthRecordByEmail(collection, email)
app.CanAccessRecord(record, requestInfo, accessRule)
```

### RecordQuery Hook System
`RecordQuery()` attaches custom `WithOneHook` and `WithAllHook` handlers that:
1. Execute the query to get `dbx.NullStringMap` rows
2. Call `newRecordFromNullStringMap(collection, data)` to create Record instances
3. For each field, call `field.PrepareValue(record, rawString)` to deserialize
4. Support `RecordProxy` interface for custom record types (like ExternalAuth, MFA, OTP)

---

## 12. Supporting Models

### Log Model (`core/log_model.go`)
```go
type Log struct {
    BaseModel
    Created types.DateTime     `db:"created"`
    Data    types.JSONMap[any] `db:"data"`
    Message string             `db:"message"`
    Level   int                `db:"level"`
}
// TableName: "_logs" (stored in auxiliary.db)
```

### Settings Model (`core/settings_model.go`)
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
// Stored in _params table with key "settings" as JSON value
```

### Auth Origin Model (`core/auth_origin_model.go`)
- RecordProxy wrapping the `_authOrigins` collection
- Tracks device fingerprints for auth alerts

### External Auth Model (`core/external_auth_model.go`)
- RecordProxy wrapping the `_externalAuths` collection
- Links auth records to OAuth2 providers

### MFA Model (`core/mfa_model.go`)
- RecordProxy wrapping the `_mfas` collection
- Tracks multi-factor authentication sessions
- Auto-cleanup via cron every hour

### OTP Model (`core/otp_model.go`)
- RecordProxy wrapping the `_otps` collection
- Tracks one-time passwords
- Auto-cleanup via cron every hour

---

## 13. SQLite Schema Definitions

### `_collections` table
```sql
CREATE TABLE _collections (
    id         TEXT PRIMARY KEY DEFAULT ('r'||lower(hex(randomblob(7)))) NOT NULL,
    system     BOOLEAN DEFAULT FALSE NOT NULL,
    type       TEXT DEFAULT "base" NOT NULL,
    name       TEXT UNIQUE NOT NULL,
    fields     JSON DEFAULT "[]" NOT NULL,
    indexes    JSON DEFAULT "[]" NOT NULL,
    listRule   TEXT DEFAULT NULL,
    viewRule   TEXT DEFAULT NULL,
    createRule TEXT DEFAULT NULL,
    updateRule TEXT DEFAULT NULL,
    deleteRule TEXT DEFAULT NULL,
    options    JSON DEFAULT "{}" NOT NULL,
    created    TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%fZ')) NOT NULL,
    updated    TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%fZ')) NOT NULL
);
CREATE INDEX idx__collections_type ON _collections (type);
```

### `_params` table
```sql
CREATE TABLE _params (
    id      TEXT PRIMARY KEY DEFAULT ('r'||lower(hex(randomblob(7)))) NOT NULL,
    value   JSON DEFAULT NULL,
    created TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%fZ')) NOT NULL,
    updated TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%fZ')) NOT NULL
);
```

### `_logs` table (auxiliary.db)
```sql
CREATE TABLE _logs (
    id      TEXT PRIMARY KEY DEFAULT ('r'||lower(hex(randomblob(7)))) NOT NULL,
    level   INTEGER DEFAULT 0 NOT NULL,
    message TEXT DEFAULT '' NOT NULL,
    data    JSON DEFAULT '{}' NOT NULL,
    created TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%fZ')) NOT NULL
);
```

### Dynamic record table (example for a "posts" base collection)
```sql
CREATE TABLE posts (
    id       TEXT PRIMARY KEY DEFAULT ('r'||lower(hex(randomblob(7)))) NOT NULL,
    title    TEXT DEFAULT '' NOT NULL,
    content  TEXT DEFAULT '' NOT NULL,
    views    NUMERIC DEFAULT 0 NOT NULL,
    tags     JSON DEFAULT '[]' NOT NULL,      -- multi-select
    author   TEXT DEFAULT '' NOT NULL,          -- single relation
    files    JSON DEFAULT '[]' NOT NULL,        -- multi-file
    metadata JSON DEFAULT NULL,                 -- json field
    created  TEXT DEFAULT '' NOT NULL,           -- autodate
    updated  TEXT DEFAULT '' NOT NULL            -- autodate
);
```

### Dynamic record table (example for an auth collection)
```sql
CREATE TABLE users (
    id               TEXT PRIMARY KEY DEFAULT ('r'||lower(hex(randomblob(7)))) NOT NULL,
    password         TEXT DEFAULT '' NOT NULL,          -- bcrypt hash
    tokenKey         TEXT DEFAULT '' NOT NULL,          -- random token
    email            TEXT DEFAULT '' NOT NULL,
    emailVisibility  BOOLEAN DEFAULT FALSE NOT NULL,
    verified         BOOLEAN DEFAULT FALSE NOT NULL,
    name             TEXT DEFAULT '' NOT NULL,
    avatar           TEXT DEFAULT '' NOT NULL,          -- single file
    created          TEXT DEFAULT '' NOT NULL,
    updated          TEXT DEFAULT '' NOT NULL
);
CREATE UNIQUE INDEX idx_tokenKey_... ON users (tokenKey);
CREATE UNIQUE INDEX idx_email_...   ON users (email) WHERE email != '';
```

---

## 14. Python/SQLAlchemy Replication Guide

### Key Design Decisions for PostgreSQL

1. **Collections table**: Create a `_collections` table with JSONB for `fields`, `indexes`, and `options`

2. **Dynamic record tables**: When a collection is created, use `DDL` or `engine.execute()` to create the corresponding record table

3. **Field type mapping (SQLite -> PostgreSQL)**:

| PB Field Type | PB SQLite Type | PostgreSQL Type |
|---------------|---------------|-----------------|
| text | TEXT | VARCHAR / TEXT |
| number | NUMERIC | DOUBLE PRECISION / NUMERIC |
| bool | BOOLEAN | BOOLEAN |
| email | TEXT | VARCHAR(255) |
| url | TEXT | TEXT |
| date | TEXT | TIMESTAMPTZ |
| autodate | TEXT | TIMESTAMPTZ |
| select (single) | TEXT | VARCHAR |
| select (multi) | JSON | JSONB |
| relation (single) | TEXT | VARCHAR(15) + FK |
| relation (multi) | JSON | JSONB |
| file (single) | TEXT | VARCHAR(255) |
| file (multi) | JSON | JSONB |
| json | JSON | JSONB |
| editor | TEXT | TEXT |
| password | TEXT | VARCHAR(255) |
| geoPoint | JSON | JSONB (or PostGIS POINT) |

4. **ID generation**: Use 15-character lowercase alphanumeric strings (`[a-z0-9]{15}`). Can use Python's `secrets` module:
   ```python
   import secrets
   import string
   alphabet = string.ascii_lowercase + string.digits
   id = ''.join(secrets.choice(alphabet) for _ in range(15))
   ```

5. **Record model**: Use a generic Record class with a JSONB `data` column, OR dynamically create SQLAlchemy table definitions from collection field schemas. The dynamic approach is closer to PocketBase's design.

6. **Rules system**: Store as nullable TEXT; interpret nil as superuser-only, empty as public, non-empty as filter expression.

7. **Auth collections**: In PostgreSQL, the same table structure but with bcrypt password hashing (use `passlib` or `bcrypt` library).

8. **Token system**: JWT tokens signed with per-collection secrets (stored in collection options JSON).

9. **Cascade delete**: Implement via application-level logic (like PocketBase does) rather than DB foreign key cascades, because multi-value relations (stored as JSON arrays) cannot use FK constraints.

10. **Multi-value fields**: For PostgreSQL, consider using array columns (`TEXT[]`) instead of JSONB for select/relation/file multi-value fields for better querying. Alternatively, use junction tables for relations.

### SQLAlchemy Model Skeleton

```python
from sqlalchemy import Column, String, Boolean, Text, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Collection(Base):
    __tablename__ = '_collections'

    id         = Column(String(15), primary_key=True)
    system     = Column(Boolean, default=False, nullable=False)
    type       = Column(String(10), default='base', nullable=False)  # base/auth/view
    name       = Column(String(255), unique=True, nullable=False)
    fields     = Column(JSONB, default=list, nullable=False)
    indexes    = Column(JSONB, default=list, nullable=False)
    list_rule  = Column(Text, nullable=True)
    view_rule  = Column(Text, nullable=True)
    create_rule = Column(Text, nullable=True)
    update_rule = Column(Text, nullable=True)
    delete_rule = Column(Text, nullable=True)
    options    = Column(JSONB, default=dict, nullable=False)
    created    = Column(DateTime(timezone=True), nullable=False)
    updated    = Column(DateTime(timezone=True), nullable=False)

class Param(Base):
    __tablename__ = '_params'

    id      = Column(String(15), primary_key=True)
    value   = Column(JSONB, nullable=True)
    created = Column(DateTime(timezone=True), nullable=False)
    updated = Column(DateTime(timezone=True), nullable=False)
```

### Dynamic Table Creation Example

```python
from sqlalchemy import Table, Column, MetaData, inspect

FIELD_TYPE_MAP = {
    'text':     lambda f: Column(f['name'], Text, default='', nullable=False),
    'number':   lambda f: Column(f['name'], Float, default=0, nullable=False),
    'bool':     lambda f: Column(f['name'], Boolean, default=False, nullable=False),
    'email':    lambda f: Column(f['name'], String(255), default='', nullable=False),
    'url':      lambda f: Column(f['name'], Text, default='', nullable=False),
    'date':     lambda f: Column(f['name'], DateTime(timezone=True), nullable=True),
    'autodate': lambda f: Column(f['name'], DateTime(timezone=True), nullable=True),
    'editor':   lambda f: Column(f['name'], Text, default='', nullable=False),
    'password': lambda f: Column(f['name'], String(255), default='', nullable=False),
    'json':     lambda f: Column(f['name'], JSONB, nullable=True),
    'geoPoint': lambda f: Column(f['name'], JSONB, default={'lon':0,'lat':0}, nullable=False),
    'select':   lambda f: Column(f['name'], JSONB if f.get('maxSelect',1) > 1 else String(255)),
    'relation': lambda f: Column(f['name'], JSONB if f.get('maxSelect',1) > 1 else String(15)),
    'file':     lambda f: Column(f['name'], JSONB if f.get('maxSelect',1) > 1 else String(255)),
}

def create_record_table(engine, collection):
    metadata = MetaData()
    columns = []
    for field in collection.fields:
        factory = FIELD_TYPE_MAP.get(field['type'])
        if factory:
            if field.get('primaryKey'):
                columns.append(Column(field['name'], String(15), primary_key=True))
            else:
                columns.append(factory(field))

    table = Table(collection.name, metadata, *columns)
    metadata.create_all(engine)
    return table
```

---

## Summary of Key Differences for Python Port

| Aspect | PocketBase (Go/SQLite) | Python Port (PostgreSQL) |
|--------|----------------------|--------------------------|
| DB Engine | SQLite | PostgreSQL |
| Date storage | TEXT (`"2006-01-02 15:04:05.000Z"`) | TIMESTAMPTZ |
| JSON fields | JSON (SQLite JSON1) | JSONB |
| Multi-value fields | JSON arrays | JSONB arrays (or array cols / junction tables) |
| Unique indexes | Custom SQL CREATE INDEX | SQLAlchemy UniqueConstraint / Index |
| Password hashing | bcrypt (Go) | bcrypt (Python passlib/bcrypt) |
| ID generation | 15-char `[a-z0-9]` | Same algorithm |
| Transactions | Single-writer SQLite | PostgreSQL MVCC |
| Lock retry | SQLITE_BUSY retry loop | Not needed (PostgreSQL handles concurrency) |
| View collections | CREATE VIEW with SQLite | CREATE VIEW with PostgreSQL |
| Full-text search | SQLite FTS5 | PostgreSQL tsvector/tsquery or pg_trgm |
| Geo support | JSON `{"lon":x,"lat":y}` | PostGIS POINT or JSONB |
