# Comparison: Collections, Field Types, Files & Relations

## 1. Field Types

PPBase defines 14 field types in `ppbase/models/field_types.py` via the `FieldType` enum, matching PocketBase's field set. Each type has a dedicated validator and a PostgreSQL column mapping in `ppbase/db/schema_manager.py`.

### 1.1 TextField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Basic text storage | Yes | Yes (`TEXT NOT NULL DEFAULT ''`) | ✅ Implemented |
| `min` / `max` length options | Yes | Yes (validated in `_validate_text`) | ✅ Implemented |
| `pattern` regex validation | Yes | Yes (validated in `_validate_text`) | ✅ Implemented |
| `AutogeneratePattern` option | Yes (e.g. `fieldName:autogenerate`) | No - not referenced anywhere in codebase | ❌ Missing |
| `:autogenerate` set modifier | Yes | No | ❌ Missing |

### 1.2 EditorField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| HTML text storage | Yes | Yes (`TEXT NOT NULL DEFAULT ''`) | ✅ Implemented |
| `maxSize` byte limit | Yes | Yes (validated in `_validate_editor`) | ✅ Implemented |
| `convertURLs` option | Yes | Not implemented | ❌ Missing |

### 1.3 NumberField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Float64 storage | Yes | Yes (`DOUBLE PRECISION NOT NULL DEFAULT 0`) | ✅ Implemented |
| `onlyInt` option | Yes | Yes (uses `INTEGER` column type, validated) | ✅ Implemented |
| `min` / `max` constraints | Yes | Yes (validated in `_validate_number`) | ✅ Implemented |
| NaN/Inf rejection | Yes | Yes | ✅ Implemented |
| `field+` / `field-` modifiers | Yes (add/subtract) | Yes (`_apply_append` / `_apply_remove` in record_service) | ✅ Implemented |

### 1.4 BoolField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Boolean storage | Yes | Yes (`BOOLEAN NOT NULL DEFAULT FALSE`) | ✅ Implemented |
| Required = must be true | Yes | Yes | ✅ Implemented |

### 1.5 EmailField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Email string storage | Yes | Yes (`VARCHAR(255) NOT NULL DEFAULT ''`) | ✅ Implemented |
| Format validation | Yes | Yes (regex `_EMAIL_RE`) | ✅ Implemented |
| `onlyDomains` filter | Yes | Yes | ✅ Implemented |
| `exceptDomains` filter | Yes | Yes | ✅ Implemented |

### 1.6 URLField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| URL string storage | Yes | Yes (`TEXT NOT NULL DEFAULT ''`) | ✅ Implemented |
| Format validation (scheme required) | Yes | Yes (regex `_URL_RE`, requires `http(s)://`) | ✅ Implemented |
| `onlyDomains` filter | Yes | Yes | ✅ Implemented |
| `exceptDomains` filter | Yes | Yes | ✅ Implemented |

### 1.7 DateField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Datetime storage | Yes (RFC3339 string) | Yes (`TIMESTAMPTZ NULL`) | ✅ Implemented |
| ISO-8601 / RFC-3339 parsing | Yes | Yes (`datetime.fromisoformat`) | ✅ Implemented |
| `min` / `max` date constraints | Yes | Yes (validated in `_validate_date`) | ✅ Implemented |
| Nullable zero-value | Yes (empty string) | Yes (returns `None`) | ✅ Implemented |

### 1.8 AutodateField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Auto-set on create/update | Yes | Yes (set via `now` in `create_record`/`update_record`, autodate fields skipped during validation) | ✅ Implemented |
| `onCreate` / `onUpdate` options | Yes | Not checked - always auto-sets both `created` and `updated` | ⚠️ Partial |

### 1.9 SelectField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Single select (maxSelect <= 1) | Yes (string) | Yes (`TEXT NOT NULL DEFAULT ''`) | ✅ Implemented |
| Multiple select (maxSelect >= 2) | Yes (array) | Yes (`TEXT[] NOT NULL DEFAULT '{}'`) | ✅ Implemented |
| `values` (allowed options) validation | Yes | Yes | ✅ Implemented |
| `maxSelect` enforcement | Yes | Yes | ✅ Implemented |
| Deduplication | Yes | Yes (preserves order) | ✅ Implemented |
| `field+` append modifier | Yes | Yes (suffix only) | ⚠️ Partial |
| `+field` prepend modifier | Yes | No - only suffix `field+` is supported, not prefix `+field` | ❌ Missing |
| `field-` remove modifier | Yes | Yes | ✅ Implemented |

### 1.10 FileField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Single file (maxSelect <= 1) | Yes (filename string) | Yes (`TEXT NOT NULL DEFAULT ''`) | ✅ Implemented |
| Multiple file (maxSelect >= 2) | Yes (filename array) | Yes (`TEXT[] NOT NULL DEFAULT '{}'`) | ✅ Implemented |
| Filename validation | Yes | Yes (`_validate_file`) | ✅ Implemented |
| `maxSize` option (per-file limit) | Yes (default ~5MB) | Not validated at field level | ❌ Missing |
| `mimeTypes` option | Yes | Stored in bootstrap schema but not validated on upload | ⚠️ Partial |
| `thumbs` option (thumbnail sizes) | Yes | Not implemented | ❌ Missing |
| `protected` option | Yes (requires file token) | Not implemented | ❌ Missing |
| `field+` append modifier | Yes | Yes (suffix only) | ⚠️ Partial |
| `+field` prepend modifier | Yes | No | ❌ Missing |
| `field-` remove modifier | Yes | Yes (with disk cleanup) | ✅ Implemented |

### 1.11 RelationField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Single relation (maxSelect <= 1) | Yes (record ID string) | Yes (`VARCHAR(15) NOT NULL DEFAULT ''`) | ✅ Implemented |
| Multiple relation (maxSelect >= 2) | Yes (record ID array) | Yes (`VARCHAR(15)[] NOT NULL DEFAULT '{}'`) | ✅ Implemented |
| `collectionId` option | Yes | Yes | ✅ Implemented |
| `cascadeDelete` option | Yes | Yes (`_cascade_delete` in record_service) | ✅ Implemented |
| `minSelect` option | Yes | Not validated | ❌ Missing |
| Foreign key existence check | Yes | Not validated (service layer comment says "at service layer" but not implemented) | ❌ Missing |
| `field+` append modifier | Yes | Yes (suffix only) | ⚠️ Partial |
| `+field` prepend modifier | Yes | No | ❌ Missing |
| `field-` remove modifier | Yes | Yes | ✅ Implemented |

### 1.12 JSONField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Arbitrary JSON storage | Yes (nullable) | Yes (`JSONB NOT NULL DEFAULT 'null'::jsonb`) | ✅ Implemented |
| `maxSize` byte limit | Yes | Yes (validated in `_validate_json`) | ✅ Implemented |
| Nullable default | Yes (`null`) | Yes | ✅ Implemented |

### 1.13 PasswordField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Plain text validation | Yes | Yes (`_validate_password`) | ✅ Implemented |
| `min` / `max` length | Yes | Yes (default 8-71) | ✅ Implemented |
| `pattern` regex | Yes | Yes | ✅ Implemented |
| Hashing at service layer | Yes | Yes (bcrypt in auth_service) | ✅ Implemented |
| Hidden in API responses | Yes | Yes (hidden_fields filter) | ✅ Implemented |

### 1.14 GeoPointField

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| lon/lat object storage | Yes (`{"lon":0,"lat":0}`) | Yes (`JSONB NOT NULL DEFAULT '{"lon":0,"lat":0}'::jsonb`) | ✅ Implemented |
| Coordinate range validation | Yes | Yes (lon: -180..180, lat: -90..90) | ✅ Implemented |
| Zero-default ("Null Island") | Yes | Yes | ✅ Implemented |

---

## 2. Collection Types

### 2.1 Base Collection

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Dynamic table creation | Yes | Yes (`create_collection_table`) | ✅ Implemented |
| Auto `id`, `created`, `updated` columns | Yes | Yes (VARCHAR(15) PK, TIMESTAMPTZ) | ✅ Implemented |
| Field-level indexes | Yes | Yes (B-tree for relations, GIN for arrays/JSONB) | ✅ Implemented |
| Custom indexes | Yes | Yes (`indexes` list passed through to DDL) | ✅ Implemented |
| CRUD operations | Yes | Yes (full create/update/delete/list/get) | ✅ Implemented |
| API rules (list/view/create/update/delete) | Yes | Yes (NULL=admin, ""=public, expression=filtered) | ✅ Implemented |

### 2.2 Auth Collection

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| System fields: email, emailVisibility, verified, password, tokenKey | Yes | Yes (physical columns in `_AUTH_SYSTEM_COLUMNS`) | ✅ Implemented |
| Unique email index | Yes | Yes (partial index where email != '') | ✅ Implemented |
| Token key index | Yes | Yes | ✅ Implemented |
| Password hashing (bcrypt) | Yes | Yes (direct bcrypt, not passlib) | ✅ Implemented |
| Token key generation | Yes | Yes (`generate_token_key`) | ✅ Implemented |
| Per-collection token secrets | Yes | Yes (authToken, verificationToken, etc. in options) | ✅ Implemented |
| `Manage` API rule | Yes | No - manage_rule not referenced in codebase | ❌ Missing |
| User registration endpoint | Yes | Phase 2 - not yet | ❌ Missing |
| User login endpoint | Yes | Phase 2 - not yet | ❌ Missing |
| OAuth2 support | Yes | Phase 2 - not yet | ❌ Missing |
| Email verification flow | Yes | Not implemented | ❌ Missing |
| Password reset flow | Yes | Not implemented | ❌ Missing |
| MFA support | Yes (system collection `_mfas`) | Collection bootstrapped but flow not implemented | ⚠️ Partial |
| OTP support | Yes (system collection `_otps`) | Collection bootstrapped but flow not implemented | ⚠️ Partial |

### 2.3 View Collection

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| CREATE VIEW from SQL query | Yes | Yes (`CREATE OR REPLACE VIEW`) | ✅ Implemented |
| Query validation (temp view rollback) | Yes | Yes (`validate_view_query`) | ✅ Implemented |
| Read-only (no create/update/delete) | Yes | Yes (handled at API layer) | ✅ Implemented |
| No realtime events | Yes | Correctly excluded | ✅ Implemented |
| Fallback ordering when no `created` | Yes | Yes (falls back to `ORDER BY 1`) | ✅ Implemented |

---

## 3. File Handling

### 3.1 File Upload

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Multipart/form-data upload | Yes | Yes (in `create_record` / `update_record`, `files` parameter) | ✅ Implemented |
| Filename sanitization + random suffix | Yes (original_name + ~10 char random) | Partial - uses uuid hex (12 chars) but discards original filename | ⚠️ Partial |
| Max file size enforcement | Yes (default ~5MB per file, configurable) | Not enforced at upload time | ❌ Missing |
| MIME type validation | Yes | Not enforced at upload time | ❌ Missing |
| Single/multi file modes | Yes | Yes (respects `maxSelect`) | ✅ Implemented |
| `field+` append files | Yes | Yes (multi-file append in update) | ✅ Implemented |
| `+field` prepend files | Yes | No | ❌ Missing |
| `field-` delete specific files | Yes | Yes (with disk cleanup in `delete_files`) | ✅ Implemented |

### 3.2 File Download / Serving

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `GET /api/files/{collection}/{record}/{filename}` | Yes | Yes (`ppbase/api/files.py`) | ✅ Implemented |
| Serves from local filesystem | Yes | Yes (`FileResponse` from `data_dir/storage/`) | ✅ Implemented |
| `?thumb=WxH` query parameter | Yes (6 formats: WxH, WxHt, WxHb, WxHf, 0xH, Wx0) | No thumbnail generation | ❌ Missing |
| `?download=1` query parameter | Yes (Content-Disposition: attachment) | Not implemented | ❌ Missing |
| `?token=` for protected files | Yes (short-lived file token) | Not implemented | ❌ Missing |
| Protected file access (View API rule check) | Yes | Not implemented | ❌ Missing |

### 3.3 File Token API

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `POST /api/files/token` | Yes (generates short-lived token) | Not implemented | ❌ Missing |
| Requires auth or superuser | Yes | N/A | ❌ Missing |

### 3.4 Storage Backend

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Local filesystem storage | Yes (default: `pb_data/storage`) | Yes (`data_dir/storage/`) | ✅ Implemented |
| S3-compatible storage | Yes (AWS S3, MinIO, Wasabi, etc.) | Config fields exist (`s3_endpoint`, `s3_bucket`, etc.) but no S3 implementation | ⚠️ Partial (config only) |
| Storage settings via Dashboard | Yes | Settings API returns S3 config scaffold | ⚠️ Partial |

---

## 4. Relation Expansion

### 4.1 Basic Expand

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `?expand=field` query parameter | Yes | Yes (`expand_service.py`, used in records API) | ✅ Implemented |
| Single relation expansion | Yes | Yes | ✅ Implemented |
| Multiple relation expansion | Yes | Yes (handles array of IDs) | ✅ Implemented |
| Comma-separated expand paths | Yes (`?expand=user,tags`) | Yes (`_parse_expand_string` splits by `,`) | ✅ Implemented |
| Nested dot-notation (`author.company`) | Yes (up to 6 levels) | Yes (recursive `_expand_path`, `MAX_EXPAND_DEPTH = 6`) | ✅ Implemented |
| Batch fetching (efficient) | Yes | Yes (`_batch_fetch_records` with parameterized IN clause) | ✅ Implemented |
| Hidden field exclusion in expanded records | Yes | Yes (password + hidden fields filtered) | ✅ Implemented |
| View API rule check on expanded records | Yes (only expand if client can View) | Not checked - all relations expanded regardless of rules | ❌ Missing |

### 4.2 Back-Relations

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `field_via_collection` syntax in expand | Yes (`comments_via_post`) | Not implemented - expand only follows forward relations | ❌ Missing |
| Back-relation in filter/sort | Yes (`comments_via_post.message ?~ 'hello'`) | Not implemented | ❌ Missing |
| Default as multiple (even if original is single) | Yes (unless UNIQUE index) | N/A | ❌ Missing |
| Max 1000 records per back-relation expand | Yes | N/A | ❌ Missing |

### 4.3 Relation Features in Filters

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Dotted path in filter (`author.name = "x"`) | Yes | Yes (`_build_relation_resolver` + filter parser EXISTS subqueries) | ✅ Implemented |
| Multi-level nested relation filter | Yes | Yes (resolver maps field -> target table) | ✅ Implemented |
| Back-relation filter (`collection_via_field.x`) | Yes | Not implemented in filter parser | ❌ Missing |

---

## 5. Field Modifier Summary

| Modifier | PocketBase | PPBase | Status |
|----------|-----------|--------|--------|
| `field+` (append/add) | Select, Relation, File, Number | Select, Relation, File, Number | ✅ Implemented |
| `+field` (prepend) | Select, Relation, File | Not implemented | ❌ Missing |
| `field-` (remove/subtract) | Select, Relation, File, Number | Select, Relation, File, Number | ✅ Implemented |
| `field:autogenerate` | TextField | Not implemented | ❌ Missing |

---

## 6. Summary by Category

### Field Types (14/14 types present)
- **Fully Implemented**: 10 types (Text, Editor, Number, Bool, Email, URL, Date, JSON, Password, GeoPoint)
- **Partially Implemented**: 3 types (Autodate - missing onCreate/onUpdate granularity; Select, Relation - missing prepend modifier)
- **Partially Implemented (File)**: File type present but missing maxSize/MIME enforcement, thumbnails, protected files

### Collection Types (3/3 types present)
- **Base**: ✅ Fully implemented
- **View**: ✅ Fully implemented
- **Auth**: ⚠️ Schema/bootstrap done, but user-facing auth flows (registration, login, OAuth2, email verification, password reset, MFA) all missing

### File Handling
- **Upload**: ⚠️ Partial - basic upload works but no size/MIME validation, no prepend, filename format differs
- **Download/Serve**: ⚠️ Partial - basic serving works but no thumbnails, no download flag, no protected files
- **Token API**: ❌ Missing entirely
- **S3 Storage**: ❌ Missing (config scaffolding only)

### Relation Expansion
- **Forward expansion**: ✅ Implemented (single, multiple, nested up to 6 levels, batch fetch)
- **Back-relations**: ❌ Missing entirely (no `_via_` syntax in expand, filter, or sort)
- **View API rule check on expand**: ❌ Missing

### Key Gaps (Prioritized)
1. **Back-relations** (`_via_` syntax) - major PocketBase feature for expand/filter/sort
2. **File thumbnails** - commonly used PocketBase feature
3. **Protected files + file token API** - security feature
4. **Prepend modifiers** (`+field`) - used by SDKs
5. **TextField autogenerate** - used for slug generation
6. **File maxSize/MIME validation** - data integrity
7. **Relation foreign key existence checks** - data integrity
8. **View API rule enforcement on expand** - security
9. **Auth collection user flows** - registration, login, OAuth2 (Phase 2 items)
10. **S3 storage backend** - scalability (Phase 2 item)
