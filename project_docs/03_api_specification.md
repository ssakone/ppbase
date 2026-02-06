# PocketBase REST API Specification

Complete API contract reference for the Python reimplementation, extracted from official PocketBase documentation (pocketbase.io/docs).

---

## Table of Contents

1. [Common Conventions](#1-common-conventions)
2. [Records API](#2-records-api)
3. [Auth Records API](#3-auth-records-api)
4. [Collections API](#4-collections-api)
5. [Settings API](#5-settings-api)
6. [Logs API](#6-logs-api)
7. [Files API](#7-files-api)
8. [Health API](#8-health-api)
9. [Backups API](#9-backups-api)
10. [Realtime / SSE API](#10-realtime--sse-api)
11. [Batch API](#11-batch-api)
12. [Query Syntax Reference](#12-query-syntax-reference)

---

## 1. Common Conventions

### 1.1 Base URL

All endpoints are relative to the PocketBase server root, e.g. `http://127.0.0.1:8090`.

### 1.2 Authentication

Authentication is passed via the `Authorization` header:

```
Authorization: TOKEN
```

Where `TOKEN` is a JWT obtained from an auth endpoint (either a record auth token or a superuser auth token). There is no `Bearer` prefix in PocketBase's documented convention -- the token is sent directly.

Three authorization levels exist:

| Level | Description |
|-------|-------------|
| **Public** | No `Authorization` header needed |
| **Record Auth** | Token from authenticating against an auth collection |
| **Superuser** | Token from authenticating against the `_superusers` collection |

The `_superusers` collection is a built-in auth collection for administrative access. Superuser tokens are obtained via the standard auth-with-password endpoint:

```
POST /api/collections/_superusers/auth-with-password
```

### 1.3 Content Types

- `application/json` -- for all JSON request bodies
- `multipart/form-data` -- required for file uploads; also accepted for non-file requests

### 1.4 Standard Error Response Format

All error responses follow this structure:

```json
{
    "status": 400,
    "message": "Human-readable error message.",
    "data": {}
}
```

For validation errors (400), the `data` object contains field-level errors:

```json
{
    "status": 400,
    "message": "Failed to create record.",
    "data": {
        "title": {
            "code": "validation_required",
            "message": "Missing required value."
        },
        "email": {
            "code": "validation_is_email",
            "message": "Must be a valid email address."
        }
    }
}
```

Common error codes:

| HTTP Status | Meaning |
|-------------|---------|
| 400 | Bad request / Validation failure |
| 401 | Missing or invalid authorization token |
| 403 | Forbidden -- authorized user lacks permission |
| 404 | Resource not found |
| 429 | Too many requests (rate limited) |

### 1.5 Standard Pagination Response

All list endpoints return paginated responses:

```json
{
    "page": 1,
    "perPage": 30,
    "totalItems": 150,
    "totalPages": 5,
    "items": [...]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `page` | Number | Current page number (1-based) |
| `perPage` | Number | Number of items per page |
| `totalItems` | Number | Total count of matching items |
| `totalPages` | Number | Total number of pages |
| `items` | Array | Array of result objects |

### 1.6 Record ID Format

Record IDs are 15-character alphanumeric strings. They can be auto-generated or provided by the client on creation.

---

## 2. Records API

### 2.1 List / Search Records

```
GET /api/collections/{collectionIdOrName}/records
```

**Auth:** Depends on collection `listRule`. Public if `listRule` is empty string. Denied if `listRule` is `null`.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `collectionIdOrName` | String | Collection ID or name |

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | Number | 1 | Pagination page number |
| `perPage` | Number | 30 | Items per page (max 500) |
| `sort` | String | | Sort expression (see [Sort Syntax](#124-sort-syntax)) |
| `filter` | String | | Filter expression (see [Filter Syntax](#121-filter-syntax)) |
| `expand` | String | | Relation expansion (see [Expand Syntax](#122-expand-syntax)) |
| `fields` | String | | Field selection (see [Fields Syntax](#123-fields-syntax)) |
| `skipTotal` | Boolean | false | If true, skip `totalItems`/`totalPages` counting for performance |

**Response (200):**

```json
{
    "page": 1,
    "perPage": 30,
    "totalItems": 2,
    "totalPages": 1,
    "items": [
        {
            "id": "ae40239d2bc4477",
            "collectionId": "a98f514eb05f454",
            "collectionName": "posts",
            "created": "2022-06-25 11:03:35.163Z",
            "updated": "2022-06-25 11:03:50.052Z",
            "title": "test1"
        }
    ]
}
```

Every record always contains these system fields:

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | 15-char unique identifier |
| `collectionId` | String | ID of the parent collection |
| `collectionName` | String | Name of the parent collection |
| `created` | String | ISO 8601 creation timestamp |
| `updated` | String | ISO 8601 last update timestamp |

Auth records additionally contain:

| Field | Type | Description |
|-------|------|-------------|
| `username` | String | Unique username |
| `email` | String | Email (hidden unless `emailVisibility` is true or requesting own record) |
| `emailVisibility` | Boolean | Whether email is publicly visible |
| `verified` | Boolean | Whether email is verified |

**Errors:**
- 400: Invalid filter syntax
- 403: `listRule` is `null` or filter references `@collection.*` without superuser access

---

### 2.2 View Record

```
GET /api/collections/{collectionIdOrName}/records/{recordId}
```

**Auth:** Depends on collection `viewRule`.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `collectionIdOrName` | String | Collection ID or name |
| `recordId` | String | ID of the record to retrieve |

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `expand` | String | Relation expansion |
| `fields` | String | Field selection |

**Response (200):**

```json
{
    "id": "ae40239d2bc4477",
    "collectionId": "a98f514eb05f454",
    "collectionName": "posts",
    "created": "2022-06-25 11:03:35.163Z",
    "updated": "2022-06-25 11:03:50.052Z",
    "title": "test1"
}
```

**Errors:**
- 403: Permission denied by `viewRule`
- 404: Record not found

---

### 2.3 Create Record

```
POST /api/collections/{collectionIdOrName}/records
```

**Auth:** Depends on collection `createRule`.

**Headers:**
- `Content-Type: application/json` or `Content-Type: multipart/form-data`

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `collectionIdOrName` | String | Collection ID or name |

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | String | No | Custom 15-char ID; auto-generated if omitted |
| `{schemaField}` | varies | Depends | Any field defined in the collection schema |

For **auth collections**, additional body fields:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `password` | String | Yes | Account password |
| `passwordConfirm` | String | Yes | Must match `password` |
| `email` | String | Depends | Email address |
| `emailVisibility` | Boolean | No | Whether email is public |
| `username` | String | No | Auto-generated if omitted |
| `verified` | Boolean | No | Only settable by superusers |

**File uploads:** Use `multipart/form-data`. File fields are sent as standard multipart file parts with the field name matching the schema field name.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `expand` | String | Auto-expand relations in the response |
| `fields` | String | Select response fields |

**Response (200):**

```json
{
    "id": "ae40239d2bc4477",
    "collectionId": "a98f514eb05f454",
    "collectionName": "demo",
    "created": "2022-06-25 11:03:35.163Z",
    "updated": "2022-06-25 11:03:50.052Z",
    "title": "Lorem ipsum"
}
```

**Errors:**
- 400: Validation failure (field-level errors in `data`)
- 403: Permission denied by `createRule`
- 404: Collection not found

---

### 2.4 Update Record

```
PATCH /api/collections/{collectionIdOrName}/records/{recordId}
```

**Auth:** Depends on collection `updateRule`.

**Headers:**
- `Content-Type: application/json` or `Content-Type: multipart/form-data`

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `collectionIdOrName` | String | Collection ID or name |
| `recordId` | String | ID of the record to update |

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `{schemaField}` | varies | No | Any updatable field |

For **auth collections**, additional optional body fields:

| Parameter | Type | Description |
|-----------|------|-------------|
| `oldPassword` | String | Required when changing password (unless superuser) |
| `password` | String | New password |
| `passwordConfirm` | String | Must match `password` |

**File field modifiers for multi-value file fields:**

| Modifier | Syntax | Description |
|----------|--------|-------------|
| Append | `fieldname+` | Append new file(s) to existing files |
| Remove | `fieldname-` | Array of filenames to remove from the field |
| Clear | `fieldname: []` | Remove all files from the field |

Example (multipart/form-data):
- `documents+` = (new file blob) -- appends to existing documents
- `documents-` = `["file1_abc123.pdf", "file2_def456.pdf"]` -- removes specific files

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `expand` | String | Auto-expand relations in the response |
| `fields` | String | Select response fields |

**Response (200):** Updated record object (same structure as create).

**Errors:**
- 400: Validation failure
- 403: Permission denied by `updateRule`
- 404: Record not found

---

### 2.5 Delete Record

```
DELETE /api/collections/{collectionIdOrName}/records/{recordId}
```

**Auth:** Depends on collection `deleteRule`.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `collectionIdOrName` | String | Collection ID or name |
| `recordId` | String | ID of the record to delete |

**Response (204):** Empty body.

**Errors:**
- 400: Cannot delete due to existing relation constraints (cascade not configured)
- 403: Permission denied by `deleteRule`
- 404: Record not found

---

## 3. Auth Records API

All auth endpoints operate on collections of type `auth`. The built-in `_superusers` collection is a special auth collection for administrative users.

### 3.1 List Auth Methods

```
GET /api/collections/{collectionIdOrName}/auth-methods
```

**Auth:** Public.

**Response (200):**

```json
{
    "password": {
        "enabled": true,
        "identityFields": ["email"]
    },
    "oauth2": {
        "enabled": true,
        "providers": [
            {
                "name": "google",
                "displayName": "Google",
                "state": "...",
                "codeVerifier": "...",
                "codeChallenge": "...",
                "codeChallengeMethod": "S256",
                "authURL": "https://accounts.google.com/o/oauth2/auth?..."
            }
        ]
    },
    "mfa": {
        "enabled": false,
        "duration": 0
    },
    "otp": {
        "enabled": false,
        "duration": 0
    }
}
```

---

### 3.2 Auth with Password

```
POST /api/collections/{collectionIdOrName}/auth-with-password
```

**Auth:** Public (subject to collection `authRule` if set).

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `identity` | String | Yes | Login identity (email, username, or other identity field) |
| `password` | String | Yes | Account password |
| `identityField` | String | No | Explicit identity field name to use for lookup |

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `expand` | String | Auto-expand relations |
| `fields` | String | Select response fields |

**Response (200):**

```json
{
    "token": "eyJhbGciOiJIUzI1NiJ9...",
    "record": {
        "id": "8171022dc95a4ed",
        "collectionId": "d2972397d45614e",
        "collectionName": "users",
        "created": "2022-06-24 06:24:18.434Z",
        "updated": "2022-06-24 06:24:18.889Z",
        "username": "test@example.com",
        "email": "test@example.com",
        "emailVisibility": true,
        "verified": false
    }
}
```

**Errors:**
- 400: Missing or invalid credentials

**Note for MFA:** If MFA is enabled, the first auth attempt returns a partial response requiring a second factor. The MFA flow uses the OTP mechanism as the second factor.

---

### 3.3 Auth with OAuth2

```
POST /api/collections/{collectionIdOrName}/auth-with-oauth2
```

**Auth:** Public.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `provider` | String | Yes | OAuth2 provider name (e.g., "google", "github", "facebook") |
| `code` | String | Yes | Authorization code from the provider |
| `codeVerifier` | String | Yes | PKCE code verifier |
| `redirectUrl` | String | Yes | The redirect URL used in the authorization request |
| `createData` | Object | No | Additional data for new account creation (JSON only, no files) |

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `expand` | String | Auto-expand relations |
| `fields` | String | Select response fields |

**Response (200):**

```json
{
    "token": "eyJhbGciOiJIUzI1NiJ9...",
    "record": {
        "id": "...",
        "collectionId": "...",
        "collectionName": "users",
        "created": "...",
        "updated": "...",
        "username": "...",
        "email": "...",
        "emailVisibility": false,
        "verified": true
    },
    "meta": {
        "id": "abc123",
        "name": "John Doe",
        "email": "test@example.com",
        "isNew": false,
        "avatarURL": "https://...",
        "accessToken": "...",
        "refreshToken": "...",
        "expiry": "..."
    }
}
```

The `meta` field contains the raw OAuth2 user info from the provider. `isNew` indicates whether a new record was created.

**Errors:**
- 400: Invalid provider, code, or redirect URL

---

### 3.4 Auth with OTP (One-Time Password)

#### 3.4.1 Request OTP

```
POST /api/collections/{collectionIdOrName}/request-otp
```

**Auth:** Public.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `email` | String | Yes | Email address of the auth record |

**Response (200):**

```json
{
    "otpId": "7XfrWL3IOnZaZMU"
}
```

**Errors:**
- 400: Invalid email
- 429: Too many OTP requests (rate limited)

#### 3.4.2 Auth with OTP

```
POST /api/collections/{collectionIdOrName}/auth-with-otp
```

**Auth:** Public.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `otpId` | String | Yes | OTP ID from the request-otp response |
| `password` | String | Yes | The one-time password sent via email |

**Response (200):** Same format as auth-with-password (token + record).

**Errors:**
- 400: Invalid OTP or otpId

---

### 3.5 Auth Refresh

```
POST /api/collections/{collectionIdOrName}/auth-refresh
```

**Auth:** Required (valid record auth token).

**Headers:**
```
Authorization: TOKEN
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `expand` | String | Auto-expand relations |
| `fields` | String | Select response fields |

**Response (200):** Same format as auth-with-password (new token + refreshed record).

**Errors:**
- 401: Invalid or missing token
- 403: Permission denied (e.g., auth refresh not allowed by collection config)
- 404: Auth record not found

---

### 3.6 Request Email Verification

```
POST /api/collections/{collectionIdOrName}/request-verification
```

**Auth:** Public.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `email` | String | Yes | Email address to verify |

**Response (204):** Empty body. Always returns 204 regardless of whether email exists (to prevent enumeration).

**Errors:**
- 400: Invalid email format

---

### 3.7 Confirm Email Verification

```
POST /api/collections/{collectionIdOrName}/confirm-verification
```

**Auth:** Public.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `token` | String | Yes | Verification token from the email |

**Response (204):** Empty body. Sets `verified` to `true`.

**Errors:**
- 400: Invalid or expired token

---

### 3.8 Request Password Reset

```
POST /api/collections/{collectionIdOrName}/request-password-reset
```

**Auth:** Public.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `email` | String | Yes | Email address for the account |

**Response (204):** Empty body. Always returns 204 regardless of whether email exists.

---

### 3.9 Confirm Password Reset

```
POST /api/collections/{collectionIdOrName}/confirm-password-reset
```

**Auth:** Public.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `token` | String | Yes | Password reset token from the email |
| `password` | String | Yes | New password |
| `passwordConfirm` | String | Yes | Must match `password` |

**Response (204):** Empty body. Invalidates all previously issued tokens for this record.

**Errors:**
- 400: Invalid/expired token or password mismatch

---

### 3.10 Request Email Change

```
POST /api/collections/{collectionIdOrName}/request-email-change
```

**Auth:** Required (record auth token).

**Headers:**
```
Authorization: TOKEN
```

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `newEmail` | String | Yes | New email address |

**Response (204):** Empty body.

**Errors:**
- 401: Missing auth token
- 403: Permission denied

---

### 3.11 Confirm Email Change

```
POST /api/collections/{collectionIdOrName}/confirm-email-change
```

**Auth:** Public (token-based).

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `token` | String | Yes | Email change confirmation token |
| `password` | String | Yes | Current account password for verification |

**Response (204):** Empty body. Invalidates all previously issued tokens for this record.

**Errors:**
- 400: Invalid token or incorrect password

---

### 3.12 Impersonate Record

```
POST /api/collections/{collectionIdOrName}/impersonate/{recordId}
```

**Auth:** Required (superuser token only).

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `collectionIdOrName` | String | Auth collection ID or name |
| `recordId` | String | ID of the record to impersonate |

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `duration` | Number | No | Custom JWT duration in seconds; defaults to collection auth token setting |

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `expand` | String | Auto-expand relations |
| `fields` | String | Select response fields |

**Response (200):**

```json
{
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "record": {
        "id": "...",
        "collectionId": "...",
        "collectionName": "...",
        "...": "..."
    }
}
```

The generated token is non-refreshable.

**Errors:**
- 400: Invalid duration
- 401: Invalid token
- 403: Not a superuser
- 404: Record not found

---

## 4. Collections API

All collection management endpoints require **superuser** authentication.

### 4.1 List Collections

```
GET /api/collections
```

**Auth:** Superuser required.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | Number | 1 | Page number |
| `perPage` | Number | 30 | Items per page |
| `sort` | String | | Sort expression |
| `filter` | String | | Filter expression |
| `fields` | String | | Field selection |
| `skipTotal` | Boolean | false | Skip total count for performance |

**Response (200):**

```json
{
    "page": 1,
    "perPage": 30,
    "totalItems": 10,
    "totalPages": 1,
    "items": [
        {
            "id": "string",
            "name": "string",
            "type": "base",
            "system": false,
            "fields": [...],
            "indexes": [...],
            "created": "2022-01-01 00:00:00.000Z",
            "updated": "2022-01-01 00:00:00.000Z",
            "listRule": null,
            "viewRule": null,
            "createRule": null,
            "updateRule": null,
            "deleteRule": null
        }
    ]
}
```

**Errors:**
- 400: Invalid filter
- 401: Unauthorized
- 403: Not a superuser

---

### 4.2 View Collection

```
GET /api/collections/{collectionIdOrName}
```

**Auth:** Superuser required.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `collectionIdOrName` | String | Collection ID or name |

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `fields` | String | Field selection |

**Response (200):** Single collection object (same structure as list items).

**Errors:**
- 401: Unauthorized
- 403: Not a superuser
- 404: Collection not found

---

### 4.3 Create Collection

```
POST /api/collections
```

**Auth:** Superuser required.

**Body Parameters:**

```json
{
    "id": "optional_15_char_string",
    "name": "required_collection_name",
    "type": "base",
    "system": false,
    "fields": [],
    "indexes": [],
    "listRule": null,
    "viewRule": null,
    "createRule": null,
    "updateRule": null,
    "deleteRule": null
}
```

#### Collection Types

| Type | Description |
|------|-------------|
| `base` | Standard data collection |
| `auth` | Authentication-enabled collection with built-in auth fields |
| `view` | Read-only collection backed by a SQL SELECT query |

#### API Rules

Rules are filter-like expressions that control access. `null` means the endpoint is disabled (no access except superusers). An empty string `""` means public access (no restrictions).

| Rule | Description |
|------|-------------|
| `listRule` | Controls who can list/search records |
| `viewRule` | Controls who can view individual records |
| `createRule` | Controls who can create records |
| `updateRule` | Controls who can update records |
| `deleteRule` | Controls who can delete records |

Rules can reference the authenticated user via `@request.auth.*`:
- `@request.auth.id != ""` -- any authenticated user
- `@request.auth.id = id` -- only the record owner
- `@request.auth.role = "admin"` -- users with admin role

#### Schema Fields

Each field in the `fields` array has this structure:

```json
{
    "name": "fieldName",
    "type": "text",
    "required": false,
    "system": false,
    "hidden": false,
    "presentable": false,
    "primaryKey": false,
    "...options": "..."
}
```

**Field types and their options:**

| Type | Options |
|------|---------|
| `text` | `min`, `max`, `pattern` (regex) |
| `number` | `min`, `max`, `noDecimal` |
| `bool` | (none) |
| `email` | `exceptDomains`, `onlyDomains` |
| `url` | `exceptDomains`, `onlyDomains` |
| `date` | `min`, `max` |
| `select` | `values` (array), `maxSelect` |
| `file` | `maxSelect`, `maxSize`, `mimeTypes`, `thumbs`, `protected` |
| `relation` | `collectionId`, `cascadeDelete`, `minSelect`, `maxSelect` |
| `json` | `maxSize` |
| `editor` | `maxSize`, `convertURLs` |
| `autodate` | `onCreate`, `onUpdate` |
| `password` | `min`, `max`, `pattern` |

#### Indexes

Indexes are raw SQL CREATE INDEX statements:

```json
{
    "indexes": [
        "CREATE INDEX idx_name ON collectionName (fieldName)",
        "CREATE UNIQUE INDEX idx_unique_email ON users (email)"
    ]
}
```

#### Auth Collection Extra Fields

For `type: "auth"` collections, additional configuration:

```json
{
    "authRule": "",
    "manageRule": null,
    "authAlert": {
        "enabled": false,
        "emailTemplate": {
            "subject": "Login from a new location",
            "body": "..."
        }
    },
    "oauth2": {
        "enabled": false,
        "mappedFields": {
            "id": "",
            "name": "",
            "username": "",
            "avatarURL": ""
        },
        "providers": [
            {
                "name": "google",
                "clientId": "",
                "clientSecret": "",
                "authURL": "",
                "tokenURL": "",
                "userInfoURL": "",
                "displayName": "",
                "pkce": null
            }
        ]
    },
    "passwordAuth": {
        "enabled": true,
        "identityFields": ["email"]
    },
    "mfa": {
        "enabled": false,
        "duration": 0,
        "rule": ""
    },
    "otp": {
        "enabled": false,
        "duration": 0,
        "length": 8,
        "emailTemplate": {
            "subject": "OTP for ...",
            "body": "..."
        }
    },
    "authToken": {
        "duration": 1209600,
        "secret": ""
    },
    "passwordResetToken": {
        "duration": 1800,
        "secret": ""
    },
    "emailChangeToken": {
        "duration": 1800,
        "secret": ""
    },
    "verificationToken": {
        "duration": 604800,
        "secret": ""
    },
    "fileToken": {
        "duration": 120,
        "secret": ""
    }
}
```

| Auth Field | Description |
|------------|-------------|
| `authRule` | Additional filter rule applied to all auth actions |
| `manageRule` | Rule allowing users to manage (CRUD) other auth records in the same collection |
| `passwordAuth.identityFields` | Fields used for password identity lookup (e.g., `["email"]`, `["email", "username"]`) |
| `mfa.duration` | Duration (seconds) of the MFA session |
| `mfa.rule` | Rule to determine when MFA is required |
| `otp.length` | Length of generated OTP codes |
| Token durations | Duration in seconds for various token types |
| Token secrets | Custom secrets for JWT signing (auto-generated if empty) |

#### View Collection Extra Fields

For `type: "view"` collections:

```json
{
    "viewQuery": "SELECT id, title, created FROM posts WHERE status = 'published'"
}
```

The `viewQuery` is a SQL SELECT statement that defines the view. The `fields` are auto-populated from the query result columns.

**Response (200):** Created collection object.

**Errors:**
- 400: Validation failure
- 401: Unauthorized
- 403: Not a superuser

---

### 4.4 Update Collection

```
PATCH /api/collections/{collectionIdOrName}
```

**Auth:** Superuser required.

**Body:** Same fields as Create (all fields optional for partial update).

**Response (200):** Updated collection object.

**Errors:**
- 400: Validation failure
- 401: Unauthorized
- 403: Not a superuser
- 404: Collection not found

---

### 4.5 Delete Collection

```
DELETE /api/collections/{collectionIdOrName}
```

**Auth:** Superuser required.

**Response (204):** Empty body.

**Errors:**
- 400: Cannot delete -- collection is referenced by other collections
- 401: Unauthorized
- 403: Not a superuser
- 404: Collection not found

---

### 4.6 Truncate Collection

```
DELETE /api/collections/{collectionIdOrName}/truncate
```

**Auth:** Superuser required.

Deletes all records in the collection without deleting the collection itself.

**Response (204):** Empty body.

**Errors:**
- 400: Failure
- 401: Unauthorized
- 403: Not a superuser
- 404: Collection not found

---

### 4.7 Import Collections

```
PUT /api/collections/import
```

**Auth:** Superuser required.

**Body Parameters:**

```json
{
    "collections": [
        {
            "name": "posts",
            "type": "base",
            "fields": [...],
            "...": "..."
        }
    ],
    "deleteMissing": false
}
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `collections` | Array | Yes | Array of collection objects to import |
| `deleteMissing` | Boolean | No | If true, delete existing collections not present in the import |

**Response (204):** Empty body.

**Errors:**
- 400: Validation failure
- 401: Unauthorized
- 403: Not a superuser

---

### 4.8 Get Collection Scaffolds

```
GET /api/collections/meta/scaffolds
```

**Auth:** Superuser required.

Returns template structures for each collection type with default field configurations.

**Response (200):** Object with keys `auth`, `base`, `view`, each containing a default collection template.

**Errors:**
- 401: Unauthorized
- 403: Not a superuser

---

## 5. Settings API

All settings endpoints require **superuser** authentication.

### 5.1 List Settings

```
GET /api/settings
```

**Auth:** Superuser required.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `fields` | String | Field selection |

**Response (200):**

```json
{
    "meta": {
        "appName": "My App",
        "appURL": "https://example.com",
        "senderName": "Support",
        "senderAddress": "support@example.com",
        "hideControls": false
    },
    "logs": {
        "maxDays": 7,
        "minLevel": 0,
        "logIP": true,
        "logAuthId": true
    },
    "smtp": {
        "enabled": false,
        "host": "",
        "port": 587,
        "username": "",
        "password": "",
        "tls": true,
        "authMethod": "PLAIN",
        "localName": ""
    },
    "s3": {
        "enabled": false,
        "bucket": "",
        "region": "",
        "endpoint": "",
        "accessKey": "",
        "secret": "",
        "forcePathStyle": false
    },
    "backups": {
        "cron": "",
        "cronMaxKeep": 3,
        "s3": {
            "enabled": false,
            "bucket": "",
            "region": "",
            "endpoint": "",
            "accessKey": "",
            "secret": "",
            "forcePathStyle": false
        }
    },
    "batch": {
        "enabled": true,
        "maxRequests": 50,
        "timeout": 3,
        "maxBodySize": 0
    },
    "rateLimits": {
        "enabled": false,
        "rules": [
            {
                "label": "rule_label",
                "audience": "",
                "duration": 1,
                "maxRequests": 100
            }
        ]
    },
    "trustedProxy": {
        "headers": [],
        "useLeftmostIP": false
    }
}
```

**Errors:**
- 401: Unauthorized
- 403: Not a superuser

---

### 5.2 Update Settings

```
PATCH /api/settings
```

**Auth:** Superuser required.

**Body:** Partial settings object. Only include the settings you want to update.

#### Meta Settings

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `meta.appName` | String | Yes | Application name |
| `meta.appURL` | String | Yes | Application URL |
| `meta.senderName` | String | Yes | Email sender display name |
| `meta.senderAddress` | String | Yes | Email sender address |
| `meta.hideControls` | Boolean | No | Hide admin UI controls |

#### Log Settings

| Parameter | Type | Description |
|-----------|------|-------------|
| `logs.maxDays` | Number | Log retention in days |
| `logs.minLevel` | Number | Minimum log level: -4 (DEBUG), 0 (INFO), 4 (WARN), 8 (ERROR) |
| `logs.logIP` | Boolean | Log IP addresses |
| `logs.logAuthId` | Boolean | Log auth record IDs |

#### SMTP Settings

| Parameter | Type | Description |
|-----------|------|-------------|
| `smtp.enabled` | Boolean | Enable SMTP |
| `smtp.host` | String | SMTP host (required if enabled) |
| `smtp.port` | Number | SMTP port (required if enabled) |
| `smtp.username` | String | SMTP username |
| `smtp.password` | String | SMTP password |
| `smtp.tls` | Boolean | Enable TLS |
| `smtp.authMethod` | String | "PLAIN" or "LOGIN" |
| `smtp.localName` | String | Local hostname for HELO/EHLO |

#### S3 Settings

| Parameter | Type | Description |
|-----------|------|-------------|
| `s3.enabled` | Boolean | Enable S3 storage |
| `s3.bucket` | String | Bucket name (required if enabled) |
| `s3.region` | String | AWS region (required if enabled) |
| `s3.endpoint` | String | S3 endpoint URL (required if enabled) |
| `s3.accessKey` | String | Access key (required if enabled) |
| `s3.secret` | String | Secret key (required if enabled) |
| `s3.forcePathStyle` | Boolean | Use path-style addressing |

#### Backup Settings

| Parameter | Type | Description |
|-----------|------|-------------|
| `backups.cron` | String | Cron expression for scheduled backups |
| `backups.cronMaxKeep` | Number | Maximum number of backups to retain |
| `backups.s3` | Object | S3 configuration for backup storage (same structure as `s3`) |

#### Batch Settings

| Parameter | Type | Description |
|-----------|------|-------------|
| `batch.enabled` | Boolean | Enable batch API |
| `batch.maxRequests` | Number | Max requests per batch (required if enabled) |
| `batch.timeout` | Number | Timeout in seconds (required if enabled) |
| `batch.maxBodySize` | Number | Max body size in bytes |

#### Rate Limit Settings

| Parameter | Type | Description |
|-----------|------|-------------|
| `rateLimits.enabled` | Boolean | Enable rate limiting |
| `rateLimits.rules` | Array | Array of rate limit rule objects |

Each rate limit rule:

| Field | Type | Description |
|-------|------|-------------|
| `label` | String | Rule identifier/label |
| `audience` | String | Target audience |
| `duration` | Number | Time window in seconds |
| `maxRequests` | Number | Max requests in the time window |

#### Trusted Proxy Settings

| Parameter | Type | Description |
|-----------|------|-------------|
| `trustedProxy.headers` | Array\<String\> | Trusted proxy headers |
| `trustedProxy.useLeftmostIP` | Boolean | Use leftmost IP from proxy header |

**Response (200):** Updated settings object.

**Errors:**
- 400: Validation failure
- 401: Unauthorized
- 403: Not a superuser

---

### 5.3 Test S3 Connection

```
POST /api/settings/test/s3
```

**Auth:** Superuser required.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `filesystem` | String | Yes | `"storage"` or `"backups"` |

**Response (204):** Empty body on success.

**Errors:**
- 400: S3 initialization failure
- 401: Unauthorized

---

### 5.4 Send Test Email

```
POST /api/settings/test/email
```

**Auth:** Superuser required.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `email` | String | Yes | Recipient email address |
| `template` | String | Yes | `"verification"`, `"password-reset"`, or `"email-change"` |
| `collection` | String | No | Auth collection name/ID (defaults to `_superusers`) |

**Response (204):** Empty body on success.

**Errors:**
- 400: Email send failure
- 401: Unauthorized

---

### 5.5 Generate Apple OAuth2 Client Secret

```
POST /api/settings/apple/generate-client-secret
```

**Auth:** Superuser required.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `clientId` | String | Yes | Apple App Service ID |
| `teamId` | String | Yes | 10-character Apple developer team ID |
| `keyId` | String | Yes | 10-character key identifier |
| `privateKey` | String | Yes | Private key for Sign in with Apple |
| `duration` | Number | Yes | Token validity in seconds (max ~15,777,000 / ~6 months) |

**Response (200):**

```json
{
    "secret": "eyJhbGciOiJFUzI1NiIs..."
}
```

**Errors:**
- 400: Generation failure
- 401: Unauthorized

---

## 6. Logs API

All logs endpoints require **superuser** authentication.

### 6.1 List Logs

```
GET /api/logs
```

**Auth:** Superuser required.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | Number | 1 | Page number |
| `perPage` | Number | 30 | Items per page |
| `sort` | String | | Sort expression. Supported fields: `@random`, `rowid`, `id`, `created`, `updated`, `level`, `message`, `data.*` |
| `filter` | String | | Filter expression. Filterable fields: `rowid`, `id`, `created`, `updated`, `level`, `message`, `data.*` |
| `fields` | String | | Field selection |

**Response (200):**

```json
{
    "page": 1,
    "perPage": 30,
    "totalItems": 100,
    "totalPages": 4,
    "items": [
        {
            "id": "log_id_string",
            "created": "2023-01-01 00:00:00.000Z",
            "updated": "2023-01-01 00:00:00.000Z",
            "level": 0,
            "message": "GET /api/health",
            "data": {
                "type": "request",
                "auth": "superuser",
                "method": "GET",
                "url": "/api/health",
                "referer": "",
                "remoteIP": "127.0.0.1",
                "userIP": "127.0.0.1",
                "userAgent": "Mozilla/5.0 ...",
                "status": 200,
                "execTime": 0.523
            }
        }
    ]
}
```

**Log Level Values:**

| Level | Name |
|-------|------|
| -4 | DEBUG |
| 0 | INFO |
| 4 | WARN |
| 8 | ERROR |

**Log Data Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `data.type` | String | Log entry type (e.g., "request") |
| `data.auth` | String | Auth context identifier |
| `data.method` | String | HTTP method |
| `data.url` | String | Request URL |
| `data.referer` | String | Referrer URL |
| `data.remoteIP` | String | Remote IP address |
| `data.userIP` | String | User IP address (from proxy headers) |
| `data.userAgent` | String | User agent string |
| `data.status` | Number | HTTP response status code |
| `data.execTime` | Number | Execution time in milliseconds |

**Errors:**
- 400: Invalid filter syntax
- 401: Unauthorized
- 403: Not a superuser

---

### 6.2 View Log

```
GET /api/logs/{id}
```

**Auth:** Superuser required.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | String | Log entry ID |

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `fields` | String | Field selection |

**Response (200):** Single log object (same structure as list items).

**Errors:**
- 401: Unauthorized
- 403: Not a superuser
- 404: Log not found

---

### 6.3 Log Statistics

```
GET /api/logs/stats
```

**Auth:** Superuser required.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `filter` | String | Filter expression (same syntax and fields as list) |
| `fields` | String | Field selection |

**Response (200):**

```json
[
    {
        "total": 15,
        "date": "2023-01-01 00:00:00.000Z"
    },
    {
        "total": 23,
        "date": "2023-01-01 01:00:00.000Z"
    }
]
```

Returns hourly aggregated counts of log entries. Each entry represents one hour.

**Errors:**
- 400: Invalid filter
- 401: Unauthorized
- 403: Not a superuser

---

## 7. Files API

### 7.1 Download / Serve File

```
GET /api/files/{collectionIdOrName}/{recordId}/{filename}
```

**Auth:** Public for non-protected files. Protected files require a valid file token.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `collectionIdOrName` | String | Collection ID or name |
| `recordId` | String | Record ID containing the file |
| `filename` | String | Filename (as stored, including random suffix) |

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `thumb` | String | Thumbnail specification (see below) |
| `token` | String | File access token for protected files |
| `download` | Boolean | If truthy (`1`, `t`, `true`), serves with `Content-Disposition: attachment` |

**Thumbnail Formats:**

| Format | Description |
|--------|-------------|
| `WxH` | Resize and center-crop to exact dimensions |
| `WxHt` | Resize and crop from the top |
| `WxHb` | Resize and crop from the bottom |
| `WxHf` | Fit within dimensions without cropping (may be smaller) |
| `0xH` | Resize to height, preserve aspect ratio |
| `Wx0` | Resize to width, preserve aspect ratio |

Thumbnails are only generated for images: jpg, png, gif (first frame only), and partially webp.

Example: `?thumb=100x100f`

**Response (200):** Raw file content with appropriate `Content-Type` header.

**Errors:**
- 400: Filesystem initialization failure
- 404: File not found

---

### 7.2 Generate Protected File Token

```
POST /api/files/token
```

**Auth:** Required (any authenticated user -- superuser or auth record).

**Headers:**
```
Authorization: TOKEN
```

**Response (200):**

```json
{
    "token": "eyJhbGciOiJIUzI1NiJ9..."
}
```

The token is short-lived (approximately 2 minutes by default, configurable via `fileToken.duration` on auth collections).

**Errors:**
- 400: Failed to generate token

### 7.3 File Storage Details

- Uploaded files are sanitized and suffixed with 10 random alphanumeric characters (e.g., `document_a4kR7wB3pQ.pdf`)
- Default storage path: `pb_data/storage/{collectionId}/{recordId}/`
- S3 storage uses the same path structure as object keys
- Default max file size: ~5MB per file (configurable per file field in collection schema)
- The `file` field type supports `mimeTypes` restriction (array of allowed MIME types)
- The `file` field type supports `thumbs` option (array of thumbnail presets like `["100x100", "200x200f"]`)

---

## 8. Health API

### 8.1 Health Check

```
GET /api/health
```

Also supports: `HEAD /api/health`

**Auth:** Public.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `fields` | String | Field selection |

**Response (200):**

```json
{
    "status": 200,
    "message": "API is healthy.",
    "data": {
        "canBackup": true
    }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | Number | HTTP status code |
| `message` | String | Health status message |
| `data.canBackup` | Boolean | Whether the server can perform backups |

---

## 9. Backups API

All backup endpoints require **superuser** authentication.

### 9.1 List Backups

```
GET /api/backups
```

**Auth:** Superuser required.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `fields` | String | Field selection |

**Response (200):**

```json
[
    {
        "key": "pb_backup_20230519162514.zip",
        "modified": "2023-05-19 16:25:57.542Z",
        "size": 251316185
    }
]
```

Note: This returns a plain array, NOT paginated.

| Field | Type | Description |
|-------|------|-------------|
| `key` | String | Backup filename |
| `modified` | String | Last modification timestamp |
| `size` | Number | File size in bytes |

**Errors:**
- 400: Failed to load backups filesystem
- 401: Unauthorized
- 403: Not a superuser

---

### 9.2 Create Backup

```
POST /api/backups
```

**Auth:** Superuser required.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | String | No | Backup filename (must match `[a-z0-9_-].zip`); auto-generated if omitted |

**Response (204):** Empty body.

**Errors:**
- 400: Another backup/restore process is already running
- 401: Unauthorized
- 403: Not a superuser

---

### 9.3 Upload Backup

```
POST /api/backups/upload
```

**Auth:** Superuser required.

**Headers:**
```
Content-Type: multipart/form-data
```

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file` | File | Yes | ZIP file with `application/zip` MIME type |

**Response (204):** Empty body.

**Errors:**
- 400: Invalid MIME type or processing failure
- 401: Unauthorized
- 403: Not a superuser

---

### 9.4 Download Backup

```
GET /api/backups/{key}
```

**Auth:** Superuser file token required (via query parameter).

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | String | Backup filename |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `token` | String | Yes | Superuser file token |

**Response (200):** Raw ZIP file content.

**Errors:**
- 400: Filesystem initialization failure
- 404: Backup not found

---

### 9.5 Delete Backup

```
DELETE /api/backups/{key}
```

**Auth:** Superuser required.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | String | Backup filename to delete |

**Response (204):** Empty body.

**Errors:**
- 400: Another backup/restore process is running
- 401: Unauthorized
- 403: Not a superuser

---

### 9.6 Restore Backup

```
POST /api/backups/{key}/restore
```

**Auth:** Superuser required.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | String | Backup filename to restore |

**Response (204):** Empty body. The PocketBase process restarts after restore.

**Errors:**
- 400: Another backup/restore process is running
- 401: Unauthorized
- 403: Not a superuser

---

## 10. Realtime / SSE API

### 10.1 Establish SSE Connection

```
GET /api/realtime
```

**Auth:** Public (authorization happens during the first Set Subscriptions call).

Establishes a Server-Sent Events (SSE) connection. Immediately sends a `PB_CONNECT` event containing the client ID.

**SSE Event: PB_CONNECT**

```
event: PB_CONNECT
data: {"clientId": "abc123"}
```

The server disconnects inactive clients after 5 minutes without messages. Clients should implement automatic reconnection.

---

### 10.2 Set Subscriptions

```
POST /api/realtime
```

**Auth:** Optional. When an `Authorization` header is provided, it authorizes the SSE connection.

**Body Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `clientId` | String | Yes | Client ID received from the PB_CONNECT event |
| `subscriptions` | Array\<String\> | No | Array of topic strings to subscribe to |

**Subscription Patterns:**

| Pattern | Description |
|---------|-------------|
| `collectionIdOrName/*` | Subscribe to all changes in a collection |
| `collectionIdOrName/recordId` | Subscribe to changes for a specific record |
| (empty array) | Unsubscribe from all topics |

**Access Control:**
- Collection-wide subscriptions (`/*`) require the collection's `listRule` to be satisfied
- Single-record subscriptions require the collection's `viewRule` to be satisfied

**Options:** The `options` parameter can include serialized JSON with `expand`, `fields`, and other query parameters that apply to the SSE events.

**Response (204):** Empty body on success.

**Errors:**
- 400: Validation error
- 403: Authorization mismatch
- 404: Invalid client ID

---

### 10.3 SSE Record Change Events

When a subscribed record changes, the server sends an SSE event:

```
event: collectionName/recordId
data: {
    "action": "create",
    "record": {
        "id": "...",
        "collectionId": "...",
        "collectionName": "...",
        "created": "...",
        "updated": "...",
        "...": "..."
    }
}
```

**Action Values:**

| Action | Description |
|--------|-------------|
| `create` | A new record was created |
| `update` | An existing record was updated |
| `delete` | A record was deleted |

The `record` field contains the full record data (subject to `expand` and `fields` options set during subscription).

---

## 11. Batch API

### 11.1 Batch Request

```
POST /api/batch
```

**Auth:** Inherits from the main request's `Authorization` header. All sub-requests share the same auth state.

**Headers:**
- `Content-Type: application/json` (without files)
- `Content-Type: multipart/form-data` (with files)

**Body Parameters (JSON):**

```json
{
    "requests": [
        {
            "method": "POST",
            "url": "/api/collections/posts/records",
            "headers": {"X-Custom": "value"},
            "body": {
                "title": "Hello",
                "content": "World"
            }
        },
        {
            "method": "PATCH",
            "url": "/api/collections/posts/records/RECORD_ID?expand=author",
            "body": {
                "title": "Updated"
            }
        },
        {
            "method": "DELETE",
            "url": "/api/collections/posts/records/RECORD_ID"
        }
    ]
}
```

**Sub-request object:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `method` | String | Yes | HTTP method: `POST`, `PATCH`, `PUT`, `DELETE` |
| `url` | String | Yes | API path with optional query parameters |
| `headers` | Object | No | Custom headers (except `Authorization`) |
| `body` | Object | No | Request body |

**Supported operations:**
- `POST` -- Create record
- `PATCH` -- Update record
- `PUT` -- Upsert record (create if not exists, update if exists)
- `DELETE` -- Delete record

**Multipart file upload in batch:**

When uploading files in batch requests, use `multipart/form-data`:
- `@jsonPayload` -- contains the serialized JSON of the `requests` array
- `requests.N.fieldName` or `requests[N].fieldName` -- file attachments for request N

Example:
```
@jsonPayload = [{"method":"POST","url":"/api/collections/posts/records","body":{"title":"test"}}]
requests.0.document = (file binary)
requests[0].image = (file binary)
```

**All operations are transactional** -- if any sub-request fails, the entire batch is rolled back.

**Response (200):**

```json
[
    {
        "status": 200,
        "body": {
            "id": "...",
            "collectionId": "...",
            "title": "Hello"
        }
    },
    {
        "status": 200,
        "body": {
            "id": "...",
            "collectionId": "...",
            "title": "Updated"
        }
    },
    {
        "status": 204,
        "body": null
    }
]
```

**Error Response (400):**

When any sub-request fails, the entire batch fails and returns error details:

```json
{
    "status": 400,
    "message": "Batch transaction failed.",
    "data": {
        "response": [
            {
                "status": 200,
                "body": {}
            },
            {
                "status": 400,
                "body": {
                    "status": 400,
                    "message": "Failed to create record.",
                    "data": {
                        "title": {
                            "code": "validation_required",
                            "message": "Missing required value."
                        }
                    }
                }
            }
        ]
    }
}
```

**Errors:**
- 400: Batch transaction failed (details in `data.response`)
- 403: Batch requests not enabled in settings

---

## 12. Query Syntax Reference

### 12.1 Filter Syntax

Filters use the format: `OPERAND OPERATOR OPERAND`

Multiple conditions are combined with `&&` (AND) and `||` (OR). Parentheses `()` control grouping. Single-line comments start with `//`.

#### Operators

| Operator | Description | Auto-wraps with `%` |
|----------|-------------|---------------------|
| `=` | Equal | No |
| `!=` | Not equal | No |
| `>` | Greater than | No |
| `>=` | Greater than or equal | No |
| `<` | Less than | No |
| `<=` | Less than or equal | No |
| `~` | Like / contains | Yes |
| `!~` | Not like / not contains | Yes |

#### "Any" Operators (for multi-value fields / relations)

By default, operators on multi-value fields (relations, select with maxSelect > 1, etc.) require ALL values to match. Prefix with `?` for "any" (at least one) match:

| Operator | Description | Auto-wraps with `%` |
|----------|-------------|---------------------|
| `?=` | Any equal | No |
| `?!=` | Any not equal | No |
| `?>` | Any greater than | No |
| `?>=` | Any greater than or equal | No |
| `?<` | Any less than | No |
| `?<=` | Any less than or equal | No |
| `?~` | Any like / contains | Yes |
| `?!~` | Any not like / not contains | Yes |

#### Operand Types

| Type | Example | Description |
|------|---------|-------------|
| Field name | `title` | Schema field name |
| String literal | `'hello'` or `"hello"` | Quoted string |
| Number | `42`, `3.14` | Numeric literal |
| Boolean | `true`, `false` | Boolean literal |
| Null | `null` | Null check |
| Macro | `@now`, `@request.auth.id` | Special variables |

#### Available Macros

| Macro | Description |
|-------|-------------|
| `@now` | Current datetime |
| `@request.auth.id` | ID of the authenticated user |
| `@request.auth.*` | Any field of the authenticated user |
| `@request.body.*` | Request body field (in rules only) |
| `@request.query.*` | Query parameter (in rules only) |
| `@request.headers.*` | Request header (in rules only) |
| `@collection.collectionName.*` | Cross-collection field reference (superuser only in API filters) |

#### Filter Examples

```
# Simple equality
?filter=(title='Hello World')

# Numeric comparison
?filter=(price > 100)

# Contains / like (auto-wrapped with %)
?filter=(title ~ 'partial')

# Multiple conditions with AND
?filter=(status='active' && created > '2023-01-01')

# OR conditions
?filter=(role='admin' || role='editor')

# Nested grouping
?filter=((status='active' || status='pending') && priority >= 3)

# Null check
?filter=(deletedAt = null)

# Boolean
?filter=(published = true)

# Date comparison with @now macro
?filter=(expires > @now)

# Auth-aware filter (only own records)
?filter=(author = @request.auth.id)

# Relation field traversal
?filter=(author.role = 'admin')

# Multi-relation "any" match
?filter=(tags ?~ 'important')

# Cross-collection reference (superuser only)
?filter=(@collection.categories.id ?= categoryId)

# Comments in filter
?filter=(
    // only active records
    status = 'active'
    && created > '2023-01-01'
)
```

---

### 12.2 Expand Syntax

Relations can be expanded (eagerly loaded) using the `expand` query parameter.

**Format:** Comma-separated list of relation field names. Supports up to 6 levels of nested expansion using dot notation.

```
?expand=author
?expand=author,category
?expand=author.company,tags
?expand=author.company.address
```

**Behavior:**
- Only relations the authenticated user has `viewRule` access to will be expanded
- Expanded records appear under the `expand` key in the response
- Single relations expand to an object; multi-relations expand to an array
- Missing or unauthorized expansions are silently omitted

**Response with expand:**

```json
{
    "id": "record_id",
    "title": "Test Post",
    "author": "user_id",
    "expand": {
        "author": {
            "id": "user_id",
            "username": "john",
            "email": "john@example.com",
            "expand": {
                "company": {
                    "id": "company_id",
                    "name": "Acme Inc."
                }
            }
        }
    }
}
```

---

### 12.3 Fields Syntax

The `fields` query parameter selects which fields to include in the response.

**Format:** Comma-separated list of field names. Supports `*` wildcard and dot notation for nested fields.

```
?fields=id,title,created
?fields=*,expand.author.name
?fields=id,title,description:excerpt(200,true)
```

**Special features:**

| Feature | Syntax | Description |
|---------|--------|-------------|
| Wildcard | `*` | All fields at the current depth |
| Nested | `expand.relField.name` | Specific nested field |
| Excerpt | `field:excerpt(maxLength, withEllipsis?)` | Truncate text field |

**Excerpt modifier:**
- `field:excerpt(200)` -- truncate to 200 characters
- `field:excerpt(200, true)` -- truncate to 200 characters with ellipsis ("...")

---

### 12.4 Sort Syntax

The `sort` query parameter orders results.

**Format:** Comma-separated list of field names with optional direction prefix.

| Prefix | Direction |
|--------|-----------|
| `+` or (none) | Ascending (ASC) |
| `-` | Descending (DESC) |

```
?sort=-created
?sort=-created,title
?sort=+title,-updated
?sort=@random
```

**Special sort values:**

| Value | Description |
|-------|-------------|
| `@random` | Random order |
| `@rowid` | Internal row ID order |

Any schema field can be used for sorting. For relation fields, sorting is by the stored ID value.

---

## Appendix A: Complete Endpoint Summary

### Records

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/collections/{collection}/records` | Rule-based | List/search records |
| GET | `/api/collections/{collection}/records/{id}` | Rule-based | View record |
| POST | `/api/collections/{collection}/records` | Rule-based | Create record |
| PATCH | `/api/collections/{collection}/records/{id}` | Rule-based | Update record |
| DELETE | `/api/collections/{collection}/records/{id}` | Rule-based | Delete record |

### Auth

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/collections/{collection}/auth-methods` | Public | List auth methods |
| POST | `/api/collections/{collection}/auth-with-password` | Public | Authenticate with password |
| POST | `/api/collections/{collection}/auth-with-oauth2` | Public | Authenticate with OAuth2 |
| POST | `/api/collections/{collection}/request-otp` | Public | Request OTP |
| POST | `/api/collections/{collection}/auth-with-otp` | Public | Authenticate with OTP |
| POST | `/api/collections/{collection}/auth-refresh` | Record token | Refresh auth token |
| POST | `/api/collections/{collection}/request-verification` | Public | Request email verification |
| POST | `/api/collections/{collection}/confirm-verification` | Public | Confirm email verification |
| POST | `/api/collections/{collection}/request-password-reset` | Public | Request password reset |
| POST | `/api/collections/{collection}/confirm-password-reset` | Public | Confirm password reset |
| POST | `/api/collections/{collection}/request-email-change` | Record token | Request email change |
| POST | `/api/collections/{collection}/confirm-email-change` | Public | Confirm email change |
| POST | `/api/collections/{collection}/impersonate/{id}` | Superuser | Impersonate a record |

### Collections

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/collections` | Superuser | List collections |
| GET | `/api/collections/{collection}` | Superuser | View collection |
| POST | `/api/collections` | Superuser | Create collection |
| PATCH | `/api/collections/{collection}` | Superuser | Update collection |
| DELETE | `/api/collections/{collection}` | Superuser | Delete collection |
| DELETE | `/api/collections/{collection}/truncate` | Superuser | Truncate collection |
| PUT | `/api/collections/import` | Superuser | Import collections |
| GET | `/api/collections/meta/scaffolds` | Superuser | Get collection scaffolds |

### Settings

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/settings` | Superuser | List settings |
| PATCH | `/api/settings` | Superuser | Update settings |
| POST | `/api/settings/test/s3` | Superuser | Test S3 connection |
| POST | `/api/settings/test/email` | Superuser | Send test email |
| POST | `/api/settings/apple/generate-client-secret` | Superuser | Generate Apple client secret |

### Logs

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/logs` | Superuser | List logs |
| GET | `/api/logs/{id}` | Superuser | View log |
| GET | `/api/logs/stats` | Superuser | Log statistics |

### Files

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/files/{collection}/{record}/{filename}` | Public/Token | Download file |
| POST | `/api/files/token` | Any auth | Generate file token |

### Health

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | Public | Health check |

### Backups

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/backups` | Superuser | List backups |
| POST | `/api/backups` | Superuser | Create backup |
| POST | `/api/backups/upload` | Superuser | Upload backup |
| GET | `/api/backups/{key}` | Superuser token | Download backup |
| DELETE | `/api/backups/{key}` | Superuser | Delete backup |
| POST | `/api/backups/{key}/restore` | Superuser | Restore backup |

### Realtime

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/realtime` | Public | Establish SSE connection |
| POST | `/api/realtime` | Optional | Set subscriptions |

### Batch

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/batch` | Inherited | Execute batch operations |

---

## Appendix B: Collection API Rules Reference

API rules are filter expressions evaluated against each request to determine access. They use the same filter syntax as query parameters, plus additional macros.

### Rule Values

| Value | Behavior |
|-------|----------|
| `null` | Endpoint is disabled (only superusers can access) |
| `""` (empty string) | Public access (no restrictions) |
| `"expression"` | Access granted if expression evaluates to true |

### Rule-Only Macros

These macros are available only in API rules (not in query filter parameters):

| Macro | Description |
|-------|-------------|
| `@request.body.fieldName` | Value of a field in the request body |
| `@request.query.paramName` | Value of a URL query parameter |
| `@request.headers.headerName` | Value of a request header |
| `@request.auth.id` | ID of the currently authenticated user |
| `@request.auth.collectionId` | Collection ID of the authenticated user |
| `@request.auth.collectionName` | Collection name of the authenticated user |
| `@request.auth.verified` | Whether the authenticated user's email is verified |
| `@request.auth.*` | Any field on the authenticated user's record |

### Rule Examples

```
// Only authenticated users
@request.auth.id != ""

// Only the record owner
@request.auth.id = id

// Only verified users
@request.auth.verified = true

// Only users with a specific role
@request.auth.role = "admin"

// Owner or admin
@request.auth.id = id || @request.auth.role = "admin"

// Restrict what can be set on create
@request.body.status = "draft"

// Only allow editing certain fields (by checking body matches current)
@request.body.role = role
```
