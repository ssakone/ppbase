# API Comparison: PocketBase vs PPBase (Records & Collections)

## Summary

| Category | Total Endpoints | Implemented | Partial | Missing |
|----------|----------------|-------------|---------|---------|
| **Records CRUD** | 5 | 5 | 0 | 0 |
| **Records Batch** | 1 | 0 | 0 | 1 |
| **Auth Record Actions** | 12 | 8 | 1 | 3 |
| **Collections CRUD** | 5 | 5 | 0 | 0 |
| **Collections Import** | 1 | 1 | 0 | 0 |
| **Collections Truncate** | 1 | 1 | 0 | 0 |
| **Collections Scaffolds** | 1 | 0 | 0 | 1 |
| **TOTAL** | **26** | **20** | **1** | **5** |

---

## 1. Records CRUD Endpoints

### 1.1 List/Search Records
`GET /api/collections/{collectionIdOrName}/records`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `page` query param (default 1) | Yes | Yes | ✅ |
| `perPage` query param (default 30) | Yes | Yes | ✅ |
| `sort` (ASC/DESC with +/-) | Yes | Yes | ✅ |
| `filter` (full filter syntax) | Yes | Yes | ✅ |
| `expand` (relation expansion) | Yes | Yes | ✅ |
| `fields` (field selection) | Yes | Yes (passed to `list_records`) | ✅ |
| `skipTotal` | Yes | Yes | ✅ |
| `@random` sort | Yes | Depends on filter_parser | ⚠️ Needs verification |
| `@rowid` sort | Yes | Depends on filter_parser | ⚠️ Needs verification |
| `@collection.*` filter (superuser only) | Yes | Not implemented | ❌ |
| `?=`, `?!=`, `?>`, etc. (any/at-least-one operators) | Yes | Depends on Lark grammar | ⚠️ Needs verification |
| `:excerpt(maxLength, withEllipsis?)` field modifier | Yes | Not implemented | ❌ |
| listRule enforcement | Yes | Yes (via `check_rule` + filter merge) | ✅ |
| Paginated response format (`page`, `perPage`, `totalItems`, `totalPages`, `items`) | Yes | Yes (via `build_list_response`) | ✅ |

**Behavior differences:**
- PPBase merges rule expressions with user-supplied filter using `&&`, consistent with PocketBase.
- PPBase returns 404 for missing collections vs PocketBase which may return different messages.
- The `@collection.*` cross-collection filter is not supported (PocketBase returns 403 for non-superusers).
- The `:excerpt()` field modifier is not implemented.

---

### 1.2 View Record
`GET /api/collections/{collectionIdOrName}/records/{recordId}`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Resolve by collection ID or name | Yes | Yes | ✅ |
| `expand` query param | Yes | Yes | ✅ |
| `fields` query param | Yes | Yes (passed to `get_record`) | ✅ |
| `:excerpt()` field modifier | Yes | Not implemented | ❌ |
| viewRule enforcement | Yes | Yes (via `check_rule` + `check_record_rule`) | ✅ |
| 403 for null rule (admin-only) | Yes | Yes | ✅ |
| 404 for record not found | Yes | Yes | ✅ |
| 404 when rule expression doesn't match | Yes | Yes | ✅ |

**Behavior differences:**
- PPBase returns 404 with "Missing collection context." for unknown collections; PocketBase uses similar messaging.

---

### 1.3 Create Record
`POST /api/collections/{collectionIdOrName}/records`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| JSON body | Yes | Yes | ✅ |
| `multipart/form-data` body | Yes | Yes | ✅ |
| Optional custom `id` (15-char string) | Yes | Yes (handled by `create_record`) | ✅ |
| Schema field validation | Yes | Yes (14 field types) | ✅ |
| File upload via multipart | Yes | Yes (files dict extracted) | ✅ |
| `expand` query param | Yes | Yes | ✅ |
| `fields` query param | Yes | Yes | ✅ |
| `:excerpt()` field modifier | Yes | Not implemented | ❌ |
| createRule enforcement | Yes | Yes | ✅ |
| Post-create rule verification | Yes | Yes (checks after insert, deletes on mismatch) | ✅ |
| Auth record `password`/`passwordConfirm` | Yes | Depends on record_service auth handling | ⚠️ |
| Blocks `_superusers` creation via this endpoint | N/A (PB uses same endpoint) | Yes (explicit guard) | ✅ |

**Behavior differences:**
- PPBase explicitly blocks `_superusers` record creation via the records API with a 400 error, directing users to admin auth endpoints instead.
- PocketBase evaluates create rule via CTE before commit; PPBase inserts first, checks rule, and deletes on mismatch (functionally equivalent but different transaction semantics).

---

### 1.4 Update Record
`PATCH /api/collections/{collectionIdOrName}/records/{recordId}`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| JSON body | Yes | Yes | ✅ |
| `multipart/form-data` body | Yes | Yes | ✅ |
| Partial update (only provided fields) | Yes | Yes | ✅ |
| File upload via multipart | Yes | Yes | ✅ |
| `expand` query param | Yes | Yes | ✅ |
| `fields` query param | Yes | Yes | ✅ |
| `:excerpt()` field modifier | Yes | Not implemented | ❌ |
| updateRule enforcement | Yes | Yes | ✅ |
| Pre-update rule verification | Yes | Yes (checks before update) | ✅ |
| Auth record `oldPassword`/`password`/`passwordConfirm` | Yes | Depends on record_service | ⚠️ |
| Blocks `_superusers` update via this endpoint | N/A | Yes (explicit guard) | ✅ |

**Behavior differences:**
- PPBase checks the rule before performing the update (vs PocketBase's CTE approach).
- Auth-specific fields (oldPassword, password, passwordConfirm) handling depends on record_service implementation.

---

### 1.5 Delete Record
`DELETE /api/collections/{collectionIdOrName}/records/{recordId}`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Returns 204 on success | Yes | Yes | ✅ |
| deleteRule enforcement | Yes | Yes | ✅ |
| Pre-delete rule verification | Yes | Yes | ✅ |
| Cascade delete handling | Yes | Yes (`all_collections` passed for relation checking) | ✅ |
| Blocks `_superusers` delete via this endpoint | N/A | Yes (explicit guard) | ✅ |
| 400 for relation reference conflicts | Yes | Depends on delete_record implementation | ⚠️ |

---

### 1.6 Batch Create/Update/Upsert/Delete Records
`POST /api/batch`

**Status: ❌ Missing**

This endpoint allows transactional batch operations (create, update, upsert, delete) across multiple collections in a single request. It requires explicit enablement from Dashboard settings.

**Missing features:**
- Entire batch endpoint not implemented
- Upsert operation (`PUT`) not supported anywhere
- Transactional multi-record operations
- `@jsonPayload` multipart support for file uploads in batch

---

## 2. Auth Record Actions

### 2.1 List Auth Methods
`GET /api/collections/{collectionIdOrName}/auth-methods`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Password auth config | Yes | Yes | ✅ |
| OAuth2 providers list with auth URLs | Yes | Yes | ✅ |
| PKCE (codeVerifier, codeChallenge, codeChallengeMethod) | Yes | Yes | ✅ |
| MFA config | Yes | Not returned | ❌ |
| OTP config | Yes | Not returned | ❌ |
| Legacy `usernamePassword`/`emailPassword` fields | Yes | Yes | ✅ |
| `fields` query param | Yes | Not implemented | ❌ |

**Behavior differences:**
- PPBase returns both legacy (`usernamePassword`, `emailPassword`, `authProviders`) and new-style (`password`, `oauth2`) response fields.
- PPBase does not include `mfa` and `otp` in the response (these features are not implemented).

---

### 2.2 Auth with Password
`POST /api/collections/{collectionIdOrName}/auth-with-password`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `identity` + `password` body params | Yes | Yes | ✅ |
| `identityField` optional param | Yes | Yes | ✅ |
| `expand` query param | Yes | Yes | ✅ |
| `fields` query param | Yes | Yes | ✅ |
| Returns `{token, record}` | Yes | Yes | ✅ |
| Blocks `_superusers` | N/A (PB routes them normally) | Yes (explicit 404) | ✅ |
| Auth collection type check | Yes | Yes | ✅ |
| Per-collection token signing | Yes | Yes (token_key + collection secret) | ✅ |

**Behavior differences:**
- PPBase explicitly blocks `_superusers` authentication via this endpoint (returns 404).
- PPBase validates `identity` and `password` presence and returns field-level errors, matching PocketBase format.

---

### 2.3 Auth with OAuth2
`POST /api/collections/{collectionIdOrName}/auth-with-oauth2`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `provider`, `code`, `codeVerifier`, `redirectUrl` body params | Yes | Yes | ✅ |
| `createData` for new accounts | Yes | Yes | ✅ |
| `expand` query param | Yes | Yes | ✅ |
| `fields` query param | Yes | Yes | ✅ |
| Returns `{token, record, meta}` | Yes | Yes | ✅ |
| `meta` includes `isNew`, `avatarURL`, `rawUser`, etc. | Yes | Depends on oauth2_service | ⚠️ |
| OAuth2 enabled check | Yes | Yes | ✅ |
| Blocks `_superusers` | N/A | Yes | ✅ |

---

### 2.4 Auth with OTP (Request + Auth)
`POST /api/collections/{collectionIdOrName}/request-otp`
`POST /api/collections/{collectionIdOrName}/auth-with-otp`

**Status: ❌ Missing**

Both OTP endpoints are not implemented. The OTP system requires:
- Sending OTP emails
- Storing OTP records in `_otps` collection
- Rate limiting (429 response)
- OTP verification and auth token generation

---

### 2.5 Auth Refresh
`POST /api/collections/{collectionIdOrName}/auth-refresh`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Requires `Authorization: TOKEN` | Yes | Yes | ✅ |
| Returns new `{token, record}` | Yes | Yes | ✅ |
| `expand` query param | Yes | Yes | ✅ |
| `fields` query param | Yes | Yes | ✅ |
| Token verification (per-collection) | Yes | Yes (via `verify_record_auth_token`) | ✅ |
| 401 for invalid/missing token | Yes | Yes | ✅ |
| 403 for unauthorized record | Yes | Not explicitly (returns 401) | ⚠️ |
| 404 for missing auth record context | Yes | Not explicitly | ⚠️ |

**Behavior differences:**
- PPBase always returns 401 for token issues; PocketBase distinguishes between 401 (missing token), 403 (not allowed), and 404 (missing context).

---

### 2.6 Request Verification
`POST /api/collections/{collectionIdOrName}/request-verification`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `email` body param | Yes | Yes | ✅ |
| Returns 204 always (anti-enumeration) | Yes | Yes | ✅ |
| Auth collection check | Yes | Yes | ✅ |

---

### 2.7 Confirm Verification
`POST /api/collections/{collectionIdOrName}/confirm-verification`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `token` body param | Yes | Yes | ✅ |
| Returns 204 on success | Yes | Yes | ✅ |
| 400 on invalid/expired token | Yes | Yes | ✅ |

---

### 2.8 Request Password Reset
`POST /api/collections/{collectionIdOrName}/request-password-reset`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `email` body param | Yes | Yes | ✅ |
| Returns 204 always | Yes | Yes | ✅ |

---

### 2.9 Confirm Password Reset
`POST /api/collections/{collectionIdOrName}/confirm-password-reset`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `token`, `password`, `passwordConfirm` body params | Yes | Yes | ✅ |
| Returns 204 on success | Yes | Yes | ✅ |
| 400 on invalid token or password mismatch | Yes | Yes | ✅ |
| Invalidates all previous auth tokens | Yes | Depends on implementation | ⚠️ |

---

### 2.10 Request Email Change
`POST /api/collections/{collectionIdOrName}/request-email-change`

**Status: ❌ Missing**

Not implemented. Requires:
- Authenticated request (`Authorization: TOKEN`)
- `newEmail` body parameter
- Sending confirmation email
- Returns 204

### 2.11 Confirm Email Change
`POST /api/collections/{collectionIdOrName}/confirm-email-change`

**Status: ❌ Missing**

Not implemented. Requires:
- `token` and `password` body parameters
- Email update + token invalidation
- Returns 204

---

### 2.12 Impersonate
`POST /api/collections/{collectionIdOrName}/impersonate/{id}`

**Status: ❌ Missing**

Not implemented. Superuser-only endpoint that:
- Generates a non-refreshable auth token for another user
- Optional `duration` body parameter (seconds)
- Supports `expand` and `fields` query params
- Returns `{token, record}`

---

## 3. Collections CRUD Endpoints

### 3.1 List Collections
`GET /api/collections`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Requires superuser auth | Yes | Yes (`require_admin` dependency) | ✅ |
| `page` query param (default 1) | Yes | Yes | ✅ |
| `perPage` query param (default 30) | Yes | Yes (max 500) | ✅ |
| `sort` by `id`, `created`, `updated`, `name`, `type`, `system` | Yes | Yes | ✅ |
| `sort` by `@random` | Yes | Depends on implementation | ⚠️ |
| `filter` support | Yes | Yes | ✅ |
| `fields` query param | Yes | Not implemented in route | ❌ |
| `skipTotal` query param | Yes | Not implemented in route | ❌ |
| Returns paginated format | Yes | Yes | ✅ |

**Behavior differences:**
- PPBase's `require_admin` dependency checks for the presence of an Authorization header but does not perform full JWT validation (marked as TODO in the code).
- `fields` and `skipTotal` query parameters are not wired up in the route handler.
- PocketBase returns collection fields in the new flat format with all field properties; PPBase's collection response format may differ.

---

### 3.2 View Collection
`GET /api/collections/{collectionIdOrName}`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Resolve by ID or name | Yes | Yes | ✅ |
| Requires superuser auth | Yes | Yes | ✅ |
| `fields` query param | Yes | Not implemented | ❌ |
| Returns full collection with fields, rules, options | Yes | Yes (via `CollectionResponse.from_record`) | ✅ |

---

### 3.3 Create Collection
`POST /api/collections`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Requires superuser auth | Yes | Yes | ✅ |
| `name`, `type`, `fields` body params | Yes | Yes (via `CollectionCreate` model) | ✅ |
| `indexes` support | Yes | Yes | ✅ |
| `system` flag | Yes | Yes | ✅ |
| CRUD API rules (`listRule`, `viewRule`, etc.) | Yes | Yes | ✅ |
| Auth collection options (`passwordAuth`, `oauth2`, etc.) | Yes | Yes | ✅ |
| View collection `viewQuery` | Yes | Yes | ✅ |
| Token configuration (`authToken`, `verificationToken`, etc.) | Yes | Yes | ✅ |
| `fields` query param for response filtering | Yes | Not implemented | ❌ |
| Auto-populating fields for view collections | Yes | Depends on implementation | ⚠️ |
| Flat field format normalization | Yes | Yes (in `CollectionCreate`) | ✅ |

---

### 3.4 Update Collection
`PATCH /api/collections/{collectionIdOrName}`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Resolve by ID or name | Yes | Yes | ✅ |
| Requires superuser auth | Yes | Yes | ✅ |
| All body params from create | Yes | Yes (via `CollectionUpdate` model) | ✅ |
| `fields` query param | Yes | Not implemented | ❌ |
| System collection protection (rename/delete rules) | Yes | Depends on collection_service | ⚠️ |

---

### 3.5 Delete Collection
`DELETE /api/collections/{collectionIdOrName}`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Resolve by ID or name | Yes | Yes | ✅ |
| Requires superuser auth | Yes | Yes | ✅ |
| Returns 204 on success | Yes | Yes | ✅ |
| 400 for referenced collections | Yes | Yes (ValueError catch) | ✅ |
| 404 for not found | Yes | Yes | ✅ |

---

### 3.6 Truncate Collection
`DELETE /api/collections/{collectionIdOrName}/truncate`

**Status: ✅ Implemented (with HTTP method difference)**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Requires superuser auth | Yes | Yes | ✅ |
| Deletes all records | Yes | Yes | ✅ |
| Cascade deletes | Yes | Depends on implementation | ⚠️ |
| Returns 204 on success | Yes | Yes | ✅ |
| **HTTP Method** | **DELETE** | **POST** | ⚠️ **Different** |

**Behavior differences:**
- PocketBase uses `DELETE /api/collections/{id}/truncate`; PPBase uses `POST /api/collections/{id}/truncate`. This is an HTTP method mismatch that breaks API compatibility.

---

### 3.7 Import Collections
`PUT /api/collections/import`

**Status: ✅ Implemented**

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Requires superuser auth | Yes | Yes | ✅ |
| `collections` body array | Yes | Yes | ✅ |
| `deleteMissing` option | Yes | Yes | ✅ |
| Returns 204 on success | Yes | Yes | ✅ |

---

### 3.8 Scaffolds
`GET /api/collections/meta/scaffolds`

**Status: ❌ Missing**

This endpoint returns default field configurations for each collection type (`base`, `auth`, `view`). Used primarily by the Dashboard UI. Not implemented in PPBase.

---

## 4. Additional API Endpoints (from router.py)

### 4.1 Extra PPBase Endpoint: Meta Tables
`GET /api/collections/meta/tables`

**Status: PPBase-only (not in PocketBase)**

PPBase adds a custom endpoint that returns all database tables and their columns for the SQL editor autocomplete feature. This is not part of the PocketBase API.

---

## 5. Cross-Cutting Concerns

### 5.1 Error Response Format

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `{status, message, data}` format | Yes | Yes | ✅ |
| Field-level validation errors in `data` | Yes | Yes | ✅ |
| Validation error codes (`validation_required`, etc.) | Yes | Yes | ✅ |

**Note:** PPBase records routes use direct `JSONResponse` with the PB error format. Collections routes use `HTTPException` with `detail` as a dict -- FastAPI's custom exception handler in `app.py` normalizes this to the PB format.

### 5.2 Authentication

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Superuser JWT auth for collections | Yes | Partial (checks header exists, no full JWT validation) | ⚠️ |
| Per-collection auth tokens | Yes | Yes | ✅ |
| Record auth token validation | Yes | Yes | ✅ |
| Optional auth for records (via rules) | Yes | Yes (via `get_optional_auth`) | ✅ |

### 5.3 Rule Engine

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `null` = admin-only | Yes | Yes | ✅ |
| `""` = public | Yes | Yes | ✅ |
| Expression rules as SQL filters | Yes | Yes | ✅ |
| `@request.auth.*` macros | Yes | Yes | ✅ |
| `@request.data.*` macros | Yes | Yes | ✅ |
| `@collection.*` cross-collection filter | Yes | Not implemented | ❌ |

---

## 6. Critical Missing Features Summary

1. **Batch endpoint** (`POST /api/batch`) -- transactional multi-record operations
2. **OTP auth** (`request-otp` + `auth-with-otp`) -- one-time password authentication
3. **Email change** (`request-email-change` + `confirm-email-change`) -- email update flow
4. **Impersonate** (`POST /api/collections/{coll}/impersonate/{id}`) -- superuser impersonation
5. **Scaffolds** (`GET /api/collections/meta/scaffolds`) -- collection type defaults
6. **Truncate HTTP method** -- should be `DELETE` not `POST` for PocketBase compatibility
7. **`:excerpt()` field modifier** -- not implemented anywhere
8. **`@collection.*` cross-collection filter** -- not supported
9. **Collections list `fields`/`skipTotal` query params** -- not wired up
10. **Full JWT validation in collections `require_admin`** -- currently only checks header presence

---

## 7. Overall API Compatibility Estimate

- **Records CRUD**: ~95% compatible (missing batch, excerpt modifier)
- **Auth Record Actions**: ~65% compatible (missing OTP, email change, impersonate; partial auth-methods)
- **Collections CRUD**: ~90% compatible (missing scaffolds, truncate method mismatch, fields/skipTotal params)
- **Overall**: ~83% endpoint coverage (20/26 endpoints implemented, 1 partial)
