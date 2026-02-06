# PocketBase Comprehensive Feature Reference

> This document serves as the authoritative reference for reimplementing PocketBase in Python.
> Based on PocketBase v0.36.2 documentation from https://pocketbase.io/docs/

---

## Table of Contents

1. [Overview and Architecture](#1-overview-and-architecture)
2. [Collections](#2-collections)
3. [Field Types](#3-field-types)
4. [Records API (CRUD)](#4-records-api-crud)
5. [Filtering, Sorting, and Pagination](#5-filtering-sorting-and-pagination)
6. [Authentication](#6-authentication)
7. [API Rules and Permissions](#7-api-rules-and-permissions)
8. [File Handling](#8-file-handling)
9. [Realtime (SSE) Subscriptions](#9-realtime-sse-subscriptions)
10. [Collections Management API](#10-collections-management-api)
11. [Settings API](#11-settings-api)
12. [Logs API](#12-logs-api)
13. [Backups API](#13-backups-api)
14. [Health Check API](#14-health-check-api)
15. [Batch Operations](#15-batch-operations)
16. [Admin / Superuser Dashboard](#16-admin--superuser-dashboard)

---

## 1. Overview and Architecture

### What PocketBase Is

PocketBase is an open source backend consisting of:

- **Embedded database** (SQLite) with realtime subscriptions
- **Built-in auth management** with multiple auth methods
- **Convenient dashboard UI** for administration
- **Simple REST-ish API** for client applications

It runs as a single binary (~11-12MB) and can function both as a **standalone application** and as a **Go framework** for extension.

### Default Web Routes

| Route                          | Purpose                                  |
| ------------------------------ | ---------------------------------------- |
| `http://127.0.0.1:8090`       | Static content served from `pb_public/`  |
| `http://127.0.0.1:8090/_/`    | Superuser dashboard UI                   |
| `http://127.0.0.1:8090/api/`  | REST API                                 |

### Directory Structure

PocketBase creates and manages two directories:

- **`pb_data/`** - Application database, uploaded files, and internal data
- **`pb_migrations/`** - JavaScript migration files for collection schema changes

### Startup Command

```bash
./pocketbase serve
```

### Extension Points

- **Go** - Framework-level customization (event hooks, custom routes, middleware)
- **JavaScript** - Scripting and migrations

---

## 2. Collections

Collections are the fundamental data structure in PocketBase. Each collection maps to a SQLite table.

### 2.1 Collection Types

#### Base Collection
- The **default** collection type
- Used for storing general application data (articles, products, posts, etc.)
- Backed by an auto-generated SQLite table based on collection name and fields
- Supports full CRUD operations and realtime events

#### Auth Collection
- Extends the Base collection with **user management and authentication** capabilities
- Has special system fields: `email`, `emailVisibility`, `verified`, `password`, `tokenKey`
- `password` and `tokenKey` are non-editable system fields but are configurable
- Multiple Auth collections can coexist with separate login and management endpoints
- Enables diverse access control strategies (e.g., separate `users` and `admins` auth collections)

#### View Collection
- A **read-only** collection populated by a SQL `SELECT` statement
- Used for aggregations, joins, and custom queries
- Example: `SELECT posts.id, posts.name, count(comments.id) as totalComments FROM posts LEFT JOIN comments`
- Fields are **auto-populated** from the SQL query
- Does **NOT** support realtime events (no create/update/delete operations)
- Does **NOT** support create/update/delete API actions

### 2.2 System Fields (Present on ALL Records)

| Field     | Type     | Description                                      |
| --------- | -------- | ------------------------------------------------ |
| `id`      | String   | 15-character unique identifier, auto-generated   |
| `created` | DateTime | Auto-set on record creation (RFC 3339 format)    |
| `updated` | DateTime | Auto-set on record update (RFC 3339 format)      |

### 2.3 Additional System Fields for Auth Collections

| Field              | Type    | Description                                          |
| ------------------ | ------- | ---------------------------------------------------- |
| `email`            | String  | User email address                                   |
| `emailVisibility`  | Boolean | Whether email is visible via the API                  |
| `verified`         | Boolean | Whether the user's email has been verified            |
| `password`         | String  | Hashed password (never returned in API responses)     |
| `tokenKey`         | String  | Internal key used for token validation                |

### 2.4 Collection Configuration Properties

| Property       | Type    | Description                                        |
| -------------- | ------- | -------------------------------------------------- |
| `id`           | String  | 15-character unique ID (auto-generated or custom)  |
| `name`         | String  | Unique collection name (maps to DB table name)     |
| `type`         | String  | `"base"`, `"auth"`, or `"view"`                    |
| `system`       | Boolean | If true, prevents rename/deletion                  |
| `fields`       | Array   | Array of field definition objects                  |
| `indexes`      | Array   | Array of raw SQL CREATE INDEX statements           |
| `listRule`     | String? | Filter expression for list access (null = locked)  |
| `viewRule`     | String? | Filter expression for view access (null = locked)  |
| `createRule`   | String? | Filter expression for create access (null = locked)|
| `updateRule`   | String? | Filter expression for update access (null = locked)|
| `deleteRule`   | String? | Filter expression for delete access (null = locked)|

### 2.5 Additional Auth Collection Configuration

| Property                       | Type    | Description                                              |
| ------------------------------ | ------- | -------------------------------------------------------- |
| `authRule`                     | String? | Constraint applied post-authentication                   |
| `manageRule`                   | String? | Admin-level permissions that bypass standard rules       |
| `passwordAuth.enabled`         | Boolean | Enable/disable password authentication                   |
| `passwordAuth.identityFields`  | Array   | Fields usable as identity (default: `["email"]`)         |
| `oauth2.enabled`               | Boolean | Enable/disable OAuth2 authentication                     |
| `oauth2.mappedFields`          | Object  | Field mappings from OAuth2 provider data                 |
| `oauth2.providers`             | Array   | Array of OAuth2 provider configurations                  |
| `mfa.enabled`                  | Boolean | Enable/disable multi-factor authentication               |
| `mfa.duration`                 | Number  | MFA validity duration                                    |
| `otp.enabled`                  | Boolean | Enable/disable one-time password auth                    |
| `otp.duration`                 | Number  | OTP validity duration                                    |
| `otp.length`                   | Number  | OTP code length                                          |
| `authToken.duration`           | Number  | Auth token validity duration (seconds)                   |
| `authToken.secret`             | String  | Auth token signing secret                                |
| `passwordResetToken.duration`  | Number  | Password reset token duration                            |
| `passwordResetToken.secret`    | String  | Password reset token signing secret                      |
| `emailChangeToken.duration`    | Number  | Email change token duration                              |
| `emailChangeToken.secret`      | String  | Email change token signing secret                        |
| `verificationToken.duration`   | Number  | Verification token duration                              |
| `verificationToken.secret`     | String  | Verification token signing secret                        |
| `fileToken.duration`           | Number  | File token duration                                      |
| `fileToken.secret`             | String  | File token signing secret                                |
| `verificationTemplate`         | Object  | Email template for verification emails                   |
| `resetPasswordTemplate`        | Object  | Email template for password reset emails                 |
| `confirmEmailChangeTemplate`   | Object  | Email template for email change confirmation             |

### 2.6 View Collection Specific

| Property    | Type   | Description                                   |
| ----------- | ------ | --------------------------------------------- |
| `viewQuery` | String | SQL SELECT statement defining the view schema |

### 2.7 Access Control Patterns

- **Role-based**: Attach `select` fields with predefined options like "employee", "staff", "admin"
- **Relation-based**: Use relation fields for ownership patterns with nested lookups
- **Managed**: Special `manageRule` allowing users to manage other users' data
- **Mixed**: Combine approaches using parentheses and `&&`/`||` operators

### 2.8 Collection Management Methods

Collections can be managed via:
1. **Dashboard UI** (visual interface at `/_/`)
2. **Web APIs** (superuser-only REST endpoints)
3. **Programmatic migrations** (Go or JavaScript files in `pb_migrations/`)

---

## 3. Field Types

All fields (except JSONField) are **non-nullable** with zero-value defaults.

### 3.1 Complete Field Type Reference

#### BoolField
- **Stores**: Boolean values
- **Default**: `false`
- **Options**: `required`

#### NumberField
- **Stores**: Float64 numeric values
- **Default**: `0`
- **Options**: `required`, `min`, `max`, `noDecimal`
- **Modifiers**: Supports `+` (increment) and `-` (decrement) modifiers on update

#### TextField
- **Stores**: String values
- **Default**: `""` (empty string)
- **Options**: `required`, `min` (length), `max` (length), `pattern` (regex), `autogeneratePattern`
- **Modifiers**: `:autogenerate` modifier available for auto-generating values on create

#### EmailField
- **Stores**: Email addresses (validated format)
- **Default**: `""` (empty string)
- **Options**: `required`, `exceptDomains`, `onlyDomains`

#### URLField
- **Stores**: URL strings (validated format)
- **Default**: `""` (empty string)
- **Options**: `required`, `exceptDomains`, `onlyDomains`

#### EditorField
- **Stores**: HTML-formatted rich text
- **Default**: `""` (empty string)
- **Options**: `required`, `maxSize`, `convertURLs`

#### DateField
- **Stores**: Datetime in RFC 3339 format (`Y-m-d H:i:s.uZ`)
- **Default**: `""` (empty string)
- **Options**: `required`, `min`, `max`
- **Note**: All dates follow RFC 3339 format. Date filtering requires full datetime strings; daily queries need range comparisons like `created >= '2024-11-19 00:00:00.000Z'`

#### AutodateField
- **Stores**: Datetime automatically set on create and/or update
- **Default**: Auto-set
- **Options**: `onCreate`, `onUpdate`
- **Note**: Used for the system `created` and `updated` fields

#### SelectField
- **Stores**: Single or multiple predefined options
- **Default**: `""` (single) or `[]` (multiple)
- **Options**: `required`, `maxSelect`, `values` (array of allowed options)
- **Modifiers**: `+` (add options) and `-` (remove options) modifiers on update

#### FileField
- **Stores**: File references (filenames stored, actual files in storage)
- **Default**: `""` (single) or `[]` (multiple)
- **Options**: `required`, `maxSelect`, `maxSize`, `mimeTypes`, `thumbs`, `protected`
- **Modifiers**: `+` (append files) and `-` (remove files) modifiers on update
- **Default max size**: ~5MB per file (configurable)

#### RelationField
- **Stores**: References to records in other collections
- **Default**: `""` (single) or `[]` (multiple)
- **Options**: `required`, `collectionId`, `cascadeDelete`, `minSelect`, `maxSelect`
- **Modifiers**: `+` (add relations) and `-` (remove relations) modifiers on update
- **Note**: Supports auto-expansion up to 6 levels deep via the `expand` query parameter

#### JSONField
- **Stores**: Arbitrary serialized JSON data
- **Default**: `null` (this is the ONLY nullable field type)
- **Options**: `required`, `maxSize`
- **Note**: Can store objects, arrays, strings, numbers, booleans, or null

#### GeoPointField
- **Stores**: Geographic coordinates as `{"lon": 0, "lat": 0}`
- **Default**: `{"lon": 0, "lat": 0}` (Null Island)
- **Options**: `required`
- **Note**: Can be used with the `geoDistance()` filter function

#### PasswordField (Auth collections only)
- **Stores**: Bcrypt hashed passwords
- **Options**: `required`, `min`, `max`, `cost` (bcrypt cost factor)
- **Note**: Never returned in API responses

### 3.2 Field Definition Object Structure

```json
{
  "id": "unique_field_id",
  "name": "fieldName",
  "type": "text",
  "required": false,
  "hidden": false,
  "presentable": false,
  // ... type-specific options
}
```

| Property      | Type    | Description                                        |
| ------------- | ------- | -------------------------------------------------- |
| `id`          | String  | Unique field identifier                            |
| `name`        | String  | Field name (column name in DB)                     |
| `type`        | String  | Field type identifier                              |
| `required`    | Boolean | Whether the field is required                      |
| `hidden`      | Boolean | Whether the field is hidden from API responses     |
| `presentable` | Boolean | Whether the field is used as display value in UI   |

### 3.3 Field Modifiers for Update Operations

For multi-value fields (Select, File, Relation), PocketBase supports modifier suffixes:

| Modifier | Usage                     | Effect                                    |
| -------- | ------------------------- | ----------------------------------------- |
| `+`      | `"fieldName+": [values]`  | Append/add values to existing             |
| `-`      | `"fieldName-": [values]`  | Remove specific values from existing      |

For NumberField:

| Modifier | Usage                   | Effect                        |
| -------- | ----------------------- | ----------------------------- |
| `+`      | `"fieldName+": 5`       | Increment by value            |
| `-`      | `"fieldName-": 3`       | Decrement by value            |

---

## 4. Records API (CRUD)

### 4.1 List / Search Records

```
GET /api/collections/{collectionIdOrName}/records
```

**Query Parameters:**

| Parameter   | Type    | Default | Description                                         |
| ----------- | ------- | ------- | --------------------------------------------------- |
| `page`      | Number  | 1       | Pagination page number                              |
| `perPage`   | Number  | 30      | Records per page                                    |
| `sort`      | String  | ""      | Comma-separated sort fields with +/- prefix         |
| `filter`    | String  | ""      | Filter expression                                   |
| `expand`    | String  | ""      | Comma-separated relation fields to auto-expand      |
| `fields`    | String  | ""      | Comma-separated fields to return                    |
| `skipTotal` | Boolean | false   | If true, skip total count query for performance     |

**Response (200):**
```json
{
  "page": 1,
  "perPage": 30,
  "totalItems": 100,
  "totalPages": 4,
  "items": [
    {
      "id": "RECORD_ID",
      "collectionId": "COLLECTION_ID",
      "collectionName": "collectionName",
      "created": "2022-01-01 00:00:00.000Z",
      "updated": "2022-01-01 00:00:00.000Z",
      // ... custom fields
    }
  ]
}
```

**Note:** When `skipTotal` is true, `totalItems` and `totalPages` are returned as `-1`.

### 4.2 View Record

```
GET /api/collections/{collectionIdOrName}/records/{recordId}
```

**Query Parameters:**

| Parameter | Type   | Description                                    |
| --------- | ------ | ---------------------------------------------- |
| `expand`  | String | Comma-separated relation fields to auto-expand |
| `fields`  | String | Comma-separated fields to return               |

**Response (200):**
```json
{
  "id": "RECORD_ID",
  "collectionId": "COLLECTION_ID",
  "collectionName": "collectionName",
  "created": "2022-01-01 00:00:00.000Z",
  "updated": "2022-01-01 00:00:00.000Z",
  // ... custom fields
  "expand": {
    "relationField": { /* expanded record */ }
  }
}
```

### 4.3 Create Record

```
POST /api/collections/{collectionIdOrName}/records
```

**Content Types:** `application/json` or `multipart/form-data` (required for file uploads)

**Body Parameters:**

| Parameter         | Type   | Description                                  |
| ----------------- | ------ | -------------------------------------------- |
| `id`              | String | Optional 15-character custom ID              |
| Schema fields     | Mixed  | Values for collection schema fields          |
| `password`        | String | Required for auth collections                |
| `passwordConfirm` | String | Required for auth collections                |

**Query Parameters:** `expand`, `fields`

**Response (200):** Created record object with all fields.

**Error Response (400):**
```json
{
  "status": 400,
  "message": "Failed to create record.",
  "data": {
    "fieldName": {
      "code": "validation_error_code",
      "message": "Human-readable error message."
    }
  }
}
```

### 4.4 Update Record

```
PATCH /api/collections/{collectionIdOrName}/records/{recordId}
```

**Content Types:** `application/json` or `multipart/form-data`

**Body Parameters:**

| Parameter         | Type   | Description                                      |
| ----------------- | ------ | ------------------------------------------------ |
| Schema fields     | Mixed  | Partial field updates (only changed fields)      |
| `oldPassword`     | String | Required when changing auth record password      |
| `password`        | String | New password for auth records                    |
| `passwordConfirm` | String | Confirmation for new password                    |

**Query Parameters:** `expand`, `fields`

**Response (200):** Updated record object.

**Note:** Supports field modifiers (`+`, `-`) for multi-value and number fields.

### 4.5 Delete Record

```
DELETE /api/collections/{collectionIdOrName}/records/{recordId}
```

**Response:**
- `204` - No Content (success)
- `400` - Bad request
- `403` - Forbidden
- `404` - Not found

### 4.6 Record Response Shape

Every record includes these system fields:

```json
{
  "id": "15_CHAR_ID",
  "collectionId": "COLLECTION_ID",
  "collectionName": "collectionName",
  "created": "2022-01-01 00:00:00.000Z",
  "updated": "2022-01-01 00:00:00.000Z"
}
```

Auth collection records additionally include:
```json
{
  "email": "user@example.com",
  "emailVisibility": true,
  "verified": true,
  "username": "username"
}
```

**Note:** `password` and `tokenKey` are NEVER returned in API responses.

---

## 5. Filtering, Sorting, and Pagination

### 5.1 Filter Syntax

Filters are string expressions evaluated against each record.

#### Comparison Operators

| Operator | Description                    | Example                        |
| -------- | ------------------------------ | ------------------------------ |
| `=`      | Equal                          | `status = "active"`            |
| `!=`     | Not equal                      | `status != "deleted"`          |
| `>`      | Greater than                   | `created > "2023-01-01"`       |
| `>=`     | Greater than or equal          | `count >= 10`                  |
| `<`      | Less than                      | `price < 100`                  |
| `<=`     | Less than or equal             | `price <= 99.99`               |
| `~`      | Like/Contains (auto-wraps with `%`) | `title ~ "hello"`        |
| `!~`     | NOT Like/Contains              | `title !~ "spam"`              |

#### Any/At-Least-One-Of Operators (prefix with `?`)

These apply "at least one" logic for array-like values or nested fields from multi-record sources.

| Operator | Description                            |
| -------- | -------------------------------------- |
| `?=`     | Any equal                              |
| `?!=`    | Any not equal                          |
| `?>`     | Any greater than                       |
| `?>=`    | Any greater than or equal              |
| `?<`     | Any less than                          |
| `?<=`    | Any less than or equal                 |
| `?~`     | Any like/contains                      |
| `?!~`    | Any not like/contains                  |

#### Logical Operators

| Operator | Description |
| -------- | ----------- |
| `&&`     | AND         |
| `\|\|`   | OR          |
| `()`     | Grouping    |
| `//`     | Comment     |

#### Example Filter Expressions

```
# Simple equality
status = "active"

# Combined conditions
(title ~ "abc" && created > "2022-01-01")

# Authenticated user check
@request.auth.id != ""

# Complex with relations
@request.auth.id != "" && (status = "active" || status = "pending")

# Multi-relation membership
@request.auth.id != "" && allowed_users.id ?= @request.auth.id

# Wildcard pattern matching
title ~ "Lorem%"
```

### 5.2 Sorting

**Format:** `?sort=-created,id`

| Prefix | Direction  |
| ------ | ---------- |
| `-`    | Descending |
| `+`    | Ascending (default if omitted) |

**Special Sort Fields:**

| Field     | Description                    |
| --------- | ------------------------------ |
| `@random` | Random ordering                |
| `@rowid`  | SQLite internal row ID         |

Multiple sort fields are comma-separated: `?sort=-created,+title,id`

### 5.3 Pagination

**Parameters:**

| Parameter   | Type    | Default | Description                                     |
| ----------- | ------- | ------- | ----------------------------------------------- |
| `page`      | Number  | 1       | Current page number (1-based)                   |
| `perPage`   | Number  | 30      | Items per page                                  |
| `skipTotal` | Boolean | false   | Skip total count for performance optimization   |

**Response includes:**
```json
{
  "page": 1,
  "perPage": 30,
  "totalItems": 100,
  "totalPages": 4,
  "items": []
}
```

When `skipTotal` is `true`, `totalItems` and `totalPages` are returned as `-1`.

### 5.4 Field Selection

**Parameter:** `?fields=id,title,created`

- Comma-separated list of fields to return
- Supports wildcard `*` for all fields at a depth level
- Supports `:excerpt(maxLength, withEllipsis?)` modifier for text truncation
  - Example: `?fields=*,description:excerpt(200,true)`

### 5.5 Relation Expansion

**Parameter:** `?expand=author,comments`

- Auto-populates related records in an `expand` object within responses
- Supports nested expansion up to **6 levels deep**: `?expand=author.organization,comments.user`
- Respects the authenticated user's **View API rule** permissions on the related collection
- Returns expanded records nested under the `expand` key of the parent record

---

## 6. Authentication

### 6.1 Authentication Model

PocketBase uses a **stateless authentication** system:

- Authentication is via `Authorization: YOUR_AUTH_TOKEN` header
- Tokens are **JWTs** signed with **HS256** algorithm
- Tokens are **NOT stored server-side** (truly stateless)
- "Logout" is simply discarding the local token (`pb.authStore.clear()`)
- No traditional sessions exist

### 6.2 Auth Token Structure

Tokens are standard JWTs containing:
- User/record identification
- Expiration timestamp
- Collection reference
- Signed with the collection's configured `authToken.secret`

### 6.3 Authentication Methods

#### 6.3.1 Password Authentication

**Endpoint:** `POST /api/collections/{collectionIdOrName}/auth-with-password`

**Body:**
```json
{
  "identity": "user@example.com",
  "password": "secretpassword",
  "identityField": ""
}
```

| Parameter       | Type   | Required | Description                                |
| --------------- | ------ | -------- | ------------------------------------------ |
| `identity`      | String | Yes      | Username, email, or value of identity field |
| `password`      | String | Yes      | Account password                           |
| `identityField` | String | No       | Specific field to use for identity lookup  |

**Response (200):**
```json
{
  "token": "JWT_TOKEN",
  "record": {
    "id": "RECORD_ID",
    "email": "user@example.com",
    // ... other fields
  }
}
```

**Requirements:**
- The "Identity/Password" auth option must be enabled on the collection
- Default identity field is `email`, but any unique field with a UNIQUE index works
- Username can also be used as identity

#### 6.3.2 OAuth2 Authentication

**Endpoint:** `POST /api/collections/{collectionIdOrName}/auth-with-oauth2`

**Body:**
```json
{
  "provider": "google",
  "code": "AUTH_CODE",
  "codeVerifier": "CODE_VERIFIER",
  "redirectUrl": "https://yourdomain.com/api/oauth2-redirect",
  "createData": {}
}
```

| Parameter       | Type   | Required | Description                               |
| --------------- | ------ | -------- | ----------------------------------------- |
| `provider`      | String | Yes      | OAuth2 provider name                      |
| `code`          | String | Yes      | Authorization code from provider          |
| `codeVerifier`  | String | Yes      | PKCE code verifier                        |
| `redirectUrl`   | String | Yes      | Redirect URL matching provider config     |
| `createData`    | Object | No       | Additional data for new account creation  |

**Supported Providers:** Google, GitHub, Microsoft, and others (configurable per collection)

**Two implementation approaches:**
1. **All-in-one (recommended):** Single SDK call handles popup flow; uses redirect URL `https://yourdomain.com/api/oauth2-redirect`
2. **Manual code exchange:** Custom flow with separate links page and redirect handler

**Note:** Apple OAuth2 requires the redirect handler to accept POST requests.

#### 6.3.3 One-Time Password (OTP) Authentication

**Step 1 - Request OTP:**

```
POST /api/collections/{collectionIdOrName}/request-otp
```

**Body:** `{ "email": "user@example.com" }`

**Response:** `{ "otpId": "OTP_RECORD_ID" }`

**Note:** Returns `otpId` even for non-existent emails as enumeration protection.

**Step 2 - Authenticate with OTP:**

```
POST /api/collections/{collectionIdOrName}/auth-with-otp
```

**Body:** `{ "otpId": "OTP_RECORD_ID", "password": "OTP_CODE" }`

**Response:** Standard auth token + record data.

**Behavior:**
- Automatically marks user email as `verified` upon success
- OTP codes are numeric (0-9 digits), configurable length
- OTP alone is considered less secure; recommended for use with MFA
- OTP records are stored in `_mfas` system collection

#### 6.3.4 Multi-Factor Authentication (MFA)

MFA requires two different authentication methods in sequence:

**Flow:**
1. User authenticates with Method A (e.g., password)
2. Server returns `401` response with `mfaId` in response body
3. User authenticates with Method B (e.g., OTP), passing `mfaId` as parameter
4. Server returns standard auth token + record data

**Example flow:** Email/password + OTP

**MFA records** are stored in the `_mfas` system collection.

### 6.4 Token Refresh

**Endpoint:** `POST /api/collections/{collectionIdOrName}/auth-refresh`

**Headers:** `Authorization: YOUR_AUTH_TOKEN` (required)

**Response (200):**
```json
{
  "token": "NEW_JWT_TOKEN",
  "record": { /* updated record data */ }
}
```

**Note:** The old token remains valid until its own expiration. PocketBase does NOT invalidate previous tokens on refresh.

### 6.5 Email Verification

**Request Verification:**
```
POST /api/collections/{collectionIdOrName}/request-verification
Body: { "email": "user@example.com" }
Response: 204
```

**Confirm Verification:**
```
POST /api/collections/{collectionIdOrName}/confirm-verification
Body: { "token": "VERIFICATION_TOKEN" }
Response: 204
```

### 6.6 Password Reset

**Request Reset:**
```
POST /api/collections/{collectionIdOrName}/request-password-reset
Body: { "email": "user@example.com" }
Response: 204
```

**Confirm Reset:**
```
POST /api/collections/{collectionIdOrName}/confirm-password-reset
Body: {
  "token": "RESET_TOKEN",
  "password": "newPassword",
  "passwordConfirm": "newPassword"
}
Response: 204
```

### 6.7 Email Change

**Request Email Change (requires auth):**
```
POST /api/collections/{collectionIdOrName}/request-email-change
Headers: Authorization: YOUR_AUTH_TOKEN
Body: { "newEmail": "new@example.com" }
Response: 204
```

**Confirm Email Change:**
```
POST /api/collections/{collectionIdOrName}/confirm-email-change
Body: {
  "token": "EMAIL_CHANGE_TOKEN",
  "password": "currentPassword"
}
Response: 204
```

### 6.8 List Auth Methods

**Endpoint:** `GET /api/collections/{collectionIdOrName}/auth-methods`

Returns a public list of all enabled authentication methods for the collection, including OAuth2 provider details (name, display name, auth URL, etc.).

### 6.9 Superuser Accounts

- Superusers are stored in the `_superusers` system collection
- Similar to regular auth records but with two key differences:
  - OAuth2 is NOT supported for superusers
  - Superusers **bypass ALL collection API rules** and can access/modify everything
- Superuser authorization is via the same `Authorization: TOKEN` header mechanism

### 6.10 Impersonation

**Endpoint:** `POST /api/collections/{collectionIdOrName}/impersonate/{id}`

- **Superuser-only** endpoint
- Generates a **non-refreshable** token for authenticating as another user
- Custom duration in seconds
- SDKs create standalone clients to maintain impersonation token state in memory

### 6.11 API Keys (Workaround)

PocketBase does not have traditional API keys. The recommended approach:
- Use **superuser impersonate tokens** for server-to-server communication
- Generated via the Impersonate endpoint or dashboard dropdown
- **Token invalidation**: Change the superuser password or update the shared auth token secret in `_superusers` collection options

### 6.12 Token Verification

No dedicated token verification endpoint exists. Use `authRefresh()` to:
- Validate an existing token
- Get updated user data
- Receive a new token with extended expiration
- Previous tokens remain valid until natural expiration

---

## 7. API Rules and Permissions

### 7.1 Rule Types

Each collection has **five rules** corresponding to API actions:

| Rule         | API Action                  | Violation Response |
| ------------ | --------------------------- | ------------------ |
| `listRule`   | List/search records         | `200` with empty items |
| `viewRule`   | View individual record      | `404`              |
| `createRule` | Create new record           | `400`              |
| `updateRule` | Update existing record      | `404`              |
| `deleteRule` | Delete existing record      | `404`              |

Auth collections additionally have:
- `manageRule` - Allows one user to manage another user's data (e.g., changing passwords)

### 7.2 Rule States

| State             | Value         | Behavior                                                    |
| ----------------- | ------------- | ----------------------------------------------------------- |
| **Locked**        | `null`        | Only superusers can perform the action. Returns `403`.      |
| **Empty string**  | `""`          | **Anyone** (authenticated or not) can perform the action.   |
| **Filter string** | `"expression"` | Only users satisfying the filter expression can proceed.   |

**Critical:** API Rules are **completely ignored** when the action is performed by an authorized superuser.

### 7.3 Available Filter Fields in Rules

#### Collection Schema Fields
- Direct access to all fields in the collection
- Supports nested relations: `someRelField.status != "pending"`

#### @request.* Fields

| Field                    | Type   | Description                                           |
| ------------------------ | ------ | ----------------------------------------------------- |
| `@request.context`       | String | Rule usage context: `default`, `oauth2`, `otp`, `password`, `realtime`, `protectedFile` |
| `@request.method`        | String | HTTP method: `GET`, `POST`, `PATCH`, `DELETE`         |
| `@request.headers.*`     | String | Request headers (normalized: lowercase, `-` to `_`)   |
| `@request.query.*`       | String | Query parameters (all as strings)                     |
| `@request.auth.*`        | Mixed  | Current authenticated user's data fields              |
| `@request.body.*`        | Mixed  | Submitted request body parameters (excludes files)    |

#### @collection.* Fields

- Access non-directly-related collections that share common field values
- Supports aliasing with `:alias` suffix for multiple joins on the same collection
- Example: `@collection.posts.author = @request.auth.id`
- Alias example: `@collection.users:authorAlias.id = author && @collection.users:editorAlias.id = editor`

### 7.4 Special Modifiers for Rules

| Modifier    | Purpose                                                    | Example                                       |
| ----------- | ---------------------------------------------------------- | --------------------------------------------- |
| `:isset`    | Check if client submitted a specific field                 | `@request.body.role:isset = false`            |
| `:changed`  | Check if client submitted AND changed a field value        | `@request.body.role:changed = false`          |
| `:length`   | Check array field item count (multi-file/select/relation)  | `@request.body.documents:length <= 5`         |
| `:each`     | Apply condition to each item in array field                | `@request.body.tags:each ~ "prefix%"`         |
| `:lower`    | Case-insensitive comparison using SQLite LOWER()           | `title:lower = "hello"`                       |

### 7.5 DateTime Macros

All UTC-based:

| Macro         | Description                |
| ------------- | -------------------------- |
| `@now`        | Current datetime           |
| `@second`     | Current second (0-59)      |
| `@minute`     | Current minute (0-59)      |
| `@hour`       | Current hour (0-23)        |
| `@weekday`    | Current weekday (0-6)      |
| `@day`        | Current day (1-31)         |
| `@month`      | Current month (1-12)       |
| `@year`       | Current year               |
| `@yesterday`  | Yesterday's date           |
| `@tomorrow`   | Tomorrow's date            |
| `@todayStart` | Start of today             |
| `@todayEnd`   | End of today               |
| `@monthStart` | Start of current month     |
| `@monthEnd`   | End of current month       |
| `@yearStart`  | Start of current year      |
| `@yearEnd`    | End of current year        |

### 7.6 Advanced Filter Functions

#### geoDistance(lonA, latA, lonB, latB)
- Calculates the **Haversine distance** between two geographic points
- Returns distance in **kilometres**
- Example: `geoDistance(location.lon, location.lat, -122.4194, 37.7749) <= 50`

#### strftime(format, [time-value, modifiers...])
- Formats dates according to SQLite strftime specifications
- Supports up to 8 modifiers
- Example: `strftime('%m', created) = '06'`

### 7.7 Default Constraint Behavior

- Field expressions with array-like values or nested fields from multi-record sources apply a **match-all** constraint by default
- Use `?` prefix operators for "any/at-least-one-of" logic
- Example: `tags ?= "important"` (at least one tag equals "important")

### 7.8 Practical Rule Examples

```
# Allow anyone (including unauthenticated)
""

# Only authenticated users
@request.auth.id != ""

# Only the record owner
@request.auth.id = user

# Only verified users
@request.auth.id != "" && @request.auth.verified = true

# Role-based access
@request.auth.role = "admin"

# Prevent field modification
@request.body.role:isset = false || @request.body.role:changed = false

# Multi-relation membership check
@request.auth.id != "" && allowed_users.id ?= @request.auth.id

# Complex combined rule
@request.auth.id != "" && (status = "active" || status = "pending")
```

---

## 8. File Handling

### 8.1 File Upload

**Prerequisites:**
- Add a `file` field type to your collection
- Files are stored with their sanitized original names plus a random 10-character suffix
  - Example: `test.png` becomes `test_52iwbgds7l.png`

**Upload via API:**
- Use `multipart/form-data` content type for the POST/PATCH request
- Include file(s) as form data with the field name matching the file field name

**Default size limit:** ~5MB per file (configurable per field)

**Appending files (multi-file fields):**
- Use the `+` modifier: `"documents+": [newFile1, newFile2]`
- This adds files alongside existing ones rather than replacing

### 8.2 File Deletion

**Delete all files from a field:**
```json
{ "documents": [] }
```
or
```json
{ "documents": "" }
```

**Delete specific files:**
```json
{ "documents-": ["file1.pdf", "file2.txt"] }
```

Works with both JSON and FormData request formats.

### 8.3 File URL Format

**Standard URL:**
```
http://127.0.0.1:8090/api/files/{collectionIdOrName}/{recordId}/{filename}
```

**With download flag (forces browser download instead of preview):**
```
http://127.0.0.1:8090/api/files/{collectionIdOrName}/{recordId}/{filename}?download=1
```

### 8.4 Thumbnail Generation

Supported for: **jpg**, **png**, **gif**, and partial **webp** support.

**Query parameter:** `?thumb=WxH`

| Format   | Example     | Behavior                          |
| -------- | ----------- | --------------------------------- |
| `WxH`    | `100x300`   | Crop centered                     |
| `WxHt`   | `100x300t`  | Crop from top                     |
| `WxHb`   | `100x300b`  | Crop from bottom                  |
| `WxHf`   | `100x300f`  | Fit without cropping (contain)    |
| `0xH`    | `0x300`     | Resize to height, maintain ratio  |
| `Wx0`    | `100x0`     | Resize to width, maintain ratio   |

**Note:** Thumb sizes must be pre-configured in the file field's `thumbs` option. Returns the original file if the requested thumb size is not configured or the file is not an image.

### 8.5 Protected Files

- Mark file fields as **"Protected"** in the Dashboard
- Protected files require:
  1. Valid authentication
  2. Compliance with the collection's View API rule
  3. A short-lived **file token** (valid ~2 minutes)

**Generating file tokens:**
```javascript
const fileToken = await pb.files.getToken();
```

**Using file tokens in URLs:**
```
/api/files/{collection}/{record}/{filename}?token=FILE_TOKEN
```

### 8.6 Storage Options

#### Local Filesystem (Default)
- Files stored in `pb_data/storage/` directory
- Fast and easy to backup
- Recommended for most use cases

#### S3-Compatible Storage
- External storage support for:
  - AWS S3
  - MinIO
  - Wasabi
  - DigitalOcean Spaces
  - Vultr Object Storage
  - Any S3-compatible provider
- Configured via Dashboard > Settings > Files storage
- Can be tested via `POST /api/settings/test/s3`

---

## 9. Realtime (SSE) Subscriptions

### 9.1 Connection Architecture

PocketBase Realtime uses **Server-Sent Events (SSE)** as the transport mechanism.

**Two-step process:**
1. Establish an SSE connection
2. Submit client subscriptions via a separate POST request

### 9.2 Step 1: Establish SSE Connection

```
GET /api/realtime
```

**Behavior:**
- Server immediately responds with a `PB_CONNECT` SSE event
- The `PB_CONNECT` event contains a unique **client ID**
- Connection has a **5-minute idle timeout** - if no messages arrive within this window, the server sends a disconnect signal
- Clients automatically re-establish connections if still active (e.g., browser tab remains open)

**Critical:** Authorization occurs during the subscription call (Step 2), NOT during connection establishment.

### 9.3 Step 2: Subscribe to Topics

```
POST /api/realtime
```

**Body:**
```json
{
  "clientId": "SSE_CLIENT_ID",
  "subscriptions": [
    "collectionName/*",
    "collectionName/RECORD_ID"
  ]
}
```

| Parameter       | Type   | Required | Description                                      |
| --------------- | ------ | -------- | ------------------------------------------------ |
| `clientId`      | String | Yes      | The SSE client connection ID from `PB_CONNECT`   |
| `subscriptions` | Array  | No       | List of subscription topics; empty = unsubscribe all |

**Subscription Topic Formats:**

| Format                              | Description                    |
| ----------------------------------- | ------------------------------ |
| `COLLECTION_ID_OR_NAME/*`           | All records in a collection    |
| `COLLECTION_ID_OR_NAME/RECORD_ID`   | Specific single record         |

**Options (appended as serialized JSON):**
```
COLLECTION_NAME/RECORD_ID?options={"query":{"abc":"123"},"headers":{"x-token":"..."}}
```

### 9.4 Authorization for Realtime

- The `Authorization` header on the **POST subscription request** establishes the auth context
- Single record subscriptions use the collection's **ViewRule**
- Collection-wide subscriptions use the collection's **ListRule**
- The auth context is associated with the SSE connection for the duration

### 9.5 Event Types

SSE events are emitted for three record operations:

| Event    | Description                    |
| -------- | ------------------------------ |
| `create` | A new record was created       |
| `update` | An existing record was updated |
| `delete` | A record was deleted           |

Each event includes the **action type** and the **full record data**.

### 9.6 Special SSE Events

| Event           | Description                                  |
| --------------- | -------------------------------------------- |
| `PB_CONNECT`    | Sent immediately on connection, contains client ID |

### 9.7 Unsubscribe

- Send a POST with empty `subscriptions` array to unsubscribe from everything
- SDK methods: `unsubscribe('RECORD_ID')`, `unsubscribe('*')`, or `unsubscribe()` (all)
- Each new subscription POST **replaces** the previous subscription set entirely

### 9.8 Error Responses

| Code | Description                                           |
| ---- | ----------------------------------------------------- |
| 204  | Subscription success                                  |
| 400  | Validation failure (e.g., missing clientId)           |
| 403  | Authorization mismatch between connection and request |
| 404  | Invalid or missing client ID                          |

### 9.9 View Collections and Realtime

View collections do **NOT** support realtime events because they have no create/update/delete operations.

---

## 10. Collections Management API

All collection management endpoints require **superuser authorization**.

### 10.1 List Collections

```
GET /api/collections
```

**Query Parameters:** `page`, `perPage`, `sort`, `filter`, `fields`, `skipTotal`

**Sortable fields:** `@random`, `id`, `created`, `updated`, `name`, `type`, `system`

**Filterable fields:** `id`, `created`, `updated`, `name`, `type`, `system`

**Response:** Paginated list of collection objects.

### 10.2 View Collection

```
GET /api/collections/{collectionIdOrName}
```

**Query Parameters:** `fields`

**Response:** Single collection object with complete schema.

### 10.3 Create Collection

```
POST /api/collections
```

**Body:** Full collection definition (see Section 2.4 for all properties).

**Required fields:**
- `name` (unique)
- `fields` (for base/auth; auto-populated for views)
- `viewQuery` (for view collections)

### 10.4 Update Collection

```
PATCH /api/collections/{collectionIdOrName}
```

**Body:** Partial collection updates.

### 10.5 Delete Collection

```
DELETE /api/collections/{collectionIdOrName}
```

**Response:** `204` on success; `400` if referenced by other collections.

### 10.6 Truncate Collection

```
DELETE /api/collections/{collectionIdOrName}/truncate
```

Deletes **all records** within a collection, including related files and cascade-delete relations.

**Response:** `204` on success.

### 10.7 Import Collections

```
PUT /api/collections/import
```

**Body:**
```json
{
  "collections": [ /* array of collection objects */ ],
  "deleteMissing": false
}
```

| Parameter       | Type    | Description                                              |
| --------------- | ------- | -------------------------------------------------------- |
| `collections`   | Array   | Array of full collection definition objects              |
| `deleteMissing` | Boolean | If true, removes collections and fields not in the import |

**Response:** `204` on success.

### 10.8 Collection Scaffolds

```
GET /api/collections/meta/scaffolds
```

Returns template definitions for each collection type (`"auth"`, `"base"`, `"view"`) with default field schemas, system fields, indexes, and configurations. Used by the Dashboard UI for creating new collections.

---

## 11. Settings API

All settings endpoints require **superuser authorization**.

### 11.1 List Settings

```
GET /api/settings
```

Returns all application settings. Secret fields are redacted as `"******"`.

### 11.2 Update Settings

```
PATCH /api/settings
```

Bulk updates settings. Accepts partial updates.

### 11.3 Available Settings Categories

#### meta (Application Metadata)
| Setting         | Description                          |
| --------------- | ------------------------------------ |
| `appName`       | Application display name             |
| `appURL`        | Application base URL                 |
| `senderName`    | Email sender name                    |
| `senderAddress` | Email sender address                 |
| `hideControls`  | Hide certain UI controls             |

#### logs (Logging Configuration)
| Setting     | Description                          |
| ----------- | ------------------------------------ |
| `maxDays`   | Log retention period in days         |
| `minLevel`  | Minimum log level to record          |
| `logIP`     | Whether to log IP addresses          |
| `logAuthId` | Whether to log auth record IDs       |

#### backups (Backup Configuration)
| Setting        | Description                          |
| -------------- | ------------------------------------ |
| `cron`         | Cron expression for scheduled backups|
| `cronMaxKeep`  | Maximum backups to retain            |
| S3 config      | S3 storage for backups               |

#### smtp (Email Configuration)
| Setting       | Description                          |
| ------------- | ------------------------------------ |
| `host`        | SMTP server hostname                 |
| `port`        | SMTP server port                     |
| `username`    | SMTP authentication username         |
| `password`    | SMTP authentication password         |
| `tls`         | TLS enabled                          |
| `authMethod`  | Authentication method                |
| `localName`   | Local hostname for EHLO              |

#### s3 (File Storage Configuration)
| Setting          | Description                          |
| ---------------- | ------------------------------------ |
| `bucket`         | S3 bucket name                       |
| `region`         | S3 region                            |
| `endpoint`       | S3 endpoint URL                      |
| `accessKey`      | S3 access key                        |
| `secret`         | S3 secret key                        |
| `forcePathStyle` | Use path-style S3 URLs               |

#### batch (Batch Request Configuration)
| Setting        | Description                          |
| -------------- | ------------------------------------ |
| `enabled`      | Whether batch operations are enabled |
| `maxRequests`  | Maximum requests per batch           |
| `timeout`      | Batch operation timeout              |
| `maxBodySize`  | Maximum body size                    |

#### rateLimits (Rate Limiting)
| Setting   | Description                                  |
| --------- | -------------------------------------------- |
| `enabled` | Whether rate limiting is active              |
| `rules`   | Array of rules with `label`, `maxRequests`, `duration` |

#### trustedProxy (Proxy Configuration)
| Setting          | Description                              |
| ---------------- | ---------------------------------------- |
| `headers`        | Array of trusted proxy headers           |
| `useLeftmostIP`  | Use leftmost IP from proxy headers       |

### 11.4 Test Endpoints

**Test S3 Connection:**
```
POST /api/settings/test/s3
Body: { "filesystem": "storage" }  // or "backups"
Response: 204
```

**Send Test Email:**
```
POST /api/settings/test/email
Body: {
  "email": "test@example.com",
  "template": "verification",  // or "password-reset", "email-change"
  "collection": "_superusers"  // optional, defaults to _superusers
}
Response: 204
```

**Generate Apple Client Secret:**
```
POST /api/settings/apple/generate-client-secret
Body: {
  "clientId": "...",
  "teamId": "...",
  "keyId": "...",
  "privateKey": "...",
  "duration": 15777000
}
Response: { "secret": "..." }
```

---

## 12. Logs API

All logs endpoints require **superuser authorization**.

### 12.1 List Logs

```
GET /api/logs
```

**Query Parameters:** `page`, `perPage`, `sort`, `filter`, `fields`

**Sortable fields:** `@random`, `rowid`, `id`, `created`, `updated`, `level`, `message`, `data.*`

**Filterable fields:** `rowid`, `id`, `created`, `updated`, `level`, `message`, `data.*`

### 12.2 View Single Log

```
GET /api/logs/{id}
```

### 12.3 Log Statistics

```
GET /api/logs/stats
```

Returns **hourly aggregated** log statistics. Supports `filter` and `fields` query parameters.

### 12.4 Log Structure

```json
{
  "id": "LOG_ID",
  "created": "2023-01-01 00:00:00.000Z",
  "updated": "2023-01-01 00:00:00.000Z",
  "level": 0,
  "message": "Human-readable description",
  "data": {
    "method": "GET",
    "status": 200,
    "url": "/api/collections/example/records",
    "userIP": "127.0.0.1",
    "execTime": 0.5,
    "auth": "authType"
  }
}
```

---

## 13. Backups API

All backup endpoints require **superuser authorization**.

### 13.1 List Backups

```
GET /api/backups
```

Returns all available backup files with metadata (`key`, `modified`, `size`).

### 13.2 Create Backup

```
POST /api/backups
Body: { "name": "optional_name.zip" }  // auto-generated if omitted
Response: 204
```

Name format must match: `[a-z0-9_-].zip`

### 13.3 Upload Backup

```
POST /api/backups/upload
Content-Type: multipart/form-data
Body: file (application/zip)
Response: 204
```

### 13.4 Delete Backup

```
DELETE /api/backups/{key}
Response: 204
```

### 13.5 Restore Backup

```
POST /api/backups/{key}/restore
Response: 204
```

Restores backup and **restarts** the PocketBase process.

### 13.6 Download Backup

```
GET /api/backups/{key}?token=SUPERUSER_FILE_TOKEN
```

Requires a superuser file token as query parameter.

---

## 14. Health Check API

```
GET /api/health
```

or

```
HEAD /api/health
```

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

No authentication required.

---

## 15. Batch Operations

### 15.1 Batch Endpoint

```
POST /api/batch
```

**Prerequisites:** Must be explicitly enabled in Dashboard settings (Settings > Batch).

### 15.2 Features

- Supports **transactional** create/update/upsert/delete in a single request
- All operations succeed or all fail (atomic transaction)
- Accepts both JSON and multipart/form-data (for file uploads)
- File uploads in batch use the pattern: `requests.N.fileField`

### 15.3 Configuration

| Setting        | Description                          |
| -------------- | ------------------------------------ |
| `enabled`      | Enable/disable batch operations      |
| `maxRequests`  | Maximum operations per batch request |
| `timeout`      | Total timeout for batch execution    |
| `maxBodySize`  | Maximum request body size            |

---

## 16. Admin / Superuser Dashboard

### 16.1 Access

- Available at `http://127.0.0.1:8090/_/`
- Requires superuser authentication
- First-time access prompts superuser account creation

### 16.2 Dashboard Capabilities

The admin dashboard provides a visual interface for:

1. **Collection Management**
   - Create, edit, and delete collections
   - Configure collection types (base, auth, view)
   - Define and modify field schemas
   - Set up indexes
   - Configure API rules (list, view, create, update, delete)

2. **Record Management**
   - Browse, search, and filter records in any collection
   - Create, edit, and delete individual records
   - View relation expansions
   - Upload and manage files

3. **Authentication Configuration**
   - Enable/disable auth methods per collection
   - Configure OAuth2 providers (client ID, secret, redirect URLs)
   - Set up MFA and OTP settings
   - Configure token durations and secrets
   - Customize email templates (verification, password reset, email change)

4. **Settings Management**
   - Application metadata (name, URL)
   - SMTP/email configuration
   - File storage (local or S3)
   - Backup scheduling and management
   - Rate limiting configuration
   - Batch operations settings
   - Trusted proxy configuration

5. **Logs Viewer**
   - View API request logs
   - Filter and search logs
   - View log statistics

6. **Backup Management**
   - Create manual backups
   - Schedule automatic backups
   - Download, upload, and restore backups
   - Configure S3 storage for backups

7. **API Preview**
   - View auto-generated API documentation
   - Test API endpoints
   - View collection schemas

### 16.3 System Collections

PocketBase creates and manages several system collections:

| Collection     | Purpose                                          |
| -------------- | ------------------------------------------------ |
| `_superusers`  | Superuser/admin accounts                         |
| `_mfas`        | Multi-factor authentication records              |
| `_otps`        | One-time password records                        |
| `_externalAuths` | External OAuth2 authentication records         |

System collections have `system: true` and cannot be renamed or deleted.

---

## Appendix A: Complete API Endpoint Reference

### Records API
| Method   | Endpoint                                                      | Description                    |
| -------- | ------------------------------------------------------------- | ------------------------------ |
| `GET`    | `/api/collections/{collection}/records`                       | List/search records            |
| `GET`    | `/api/collections/{collection}/records/{id}`                  | View record                    |
| `POST`   | `/api/collections/{collection}/records`                       | Create record                  |
| `PATCH`  | `/api/collections/{collection}/records/{id}`                  | Update record                  |
| `DELETE` | `/api/collections/{collection}/records/{id}`                  | Delete record                  |

### Auth API
| Method   | Endpoint                                                      | Description                    |
| -------- | ------------------------------------------------------------- | ------------------------------ |
| `GET`    | `/api/collections/{collection}/auth-methods`                  | List auth methods              |
| `POST`   | `/api/collections/{collection}/auth-with-password`            | Password authentication        |
| `POST`   | `/api/collections/{collection}/auth-with-oauth2`              | OAuth2 authentication          |
| `POST`   | `/api/collections/{collection}/auth-with-otp`                 | OTP authentication             |
| `POST`   | `/api/collections/{collection}/request-otp`                   | Request OTP code               |
| `POST`   | `/api/collections/{collection}/auth-refresh`                  | Refresh auth token             |
| `POST`   | `/api/collections/{collection}/request-verification`          | Request email verification     |
| `POST`   | `/api/collections/{collection}/confirm-verification`          | Confirm email verification     |
| `POST`   | `/api/collections/{collection}/request-password-reset`        | Request password reset         |
| `POST`   | `/api/collections/{collection}/confirm-password-reset`        | Confirm password reset         |
| `POST`   | `/api/collections/{collection}/request-email-change`          | Request email change           |
| `POST`   | `/api/collections/{collection}/confirm-email-change`          | Confirm email change           |
| `POST`   | `/api/collections/{collection}/impersonate/{id}`              | Impersonate user (superuser)   |

### Collections Management API
| Method   | Endpoint                                                      | Description                    |
| -------- | ------------------------------------------------------------- | ------------------------------ |
| `GET`    | `/api/collections`                                            | List collections               |
| `GET`    | `/api/collections/{collection}`                               | View collection                |
| `POST`   | `/api/collections`                                            | Create collection              |
| `PATCH`  | `/api/collections/{collection}`                               | Update collection              |
| `DELETE` | `/api/collections/{collection}`                               | Delete collection              |
| `DELETE` | `/api/collections/{collection}/truncate`                      | Truncate collection            |
| `PUT`    | `/api/collections/import`                                     | Import collections             |
| `GET`    | `/api/collections/meta/scaffolds`                             | Get collection scaffolds       |

### Files API
| Method   | Endpoint                                                      | Description                    |
| -------- | ------------------------------------------------------------- | ------------------------------ |
| `GET`    | `/api/files/{collection}/{record}/{filename}`                 | Serve/download file            |

### Realtime API
| Method   | Endpoint                                                      | Description                    |
| -------- | ------------------------------------------------------------- | ------------------------------ |
| `GET`    | `/api/realtime`                                               | Establish SSE connection       |
| `POST`   | `/api/realtime`                                               | Set subscriptions              |

### Settings API
| Method   | Endpoint                                                      | Description                    |
| -------- | ------------------------------------------------------------- | ------------------------------ |
| `GET`    | `/api/settings`                                               | List settings                  |
| `PATCH`  | `/api/settings`                                               | Update settings                |
| `POST`   | `/api/settings/test/s3`                                       | Test S3 connection             |
| `POST`   | `/api/settings/test/email`                                    | Send test email                |
| `POST`   | `/api/settings/apple/generate-client-secret`                  | Generate Apple client secret   |

### Logs API
| Method   | Endpoint                                                      | Description                    |
| -------- | ------------------------------------------------------------- | ------------------------------ |
| `GET`    | `/api/logs`                                                   | List logs                      |
| `GET`    | `/api/logs/{id}`                                              | View log                       |
| `GET`    | `/api/logs/stats`                                             | Log statistics                 |

### Backups API
| Method   | Endpoint                                                      | Description                    |
| -------- | ------------------------------------------------------------- | ------------------------------ |
| `GET`    | `/api/backups`                                                | List backups                   |
| `POST`   | `/api/backups`                                                | Create backup                  |
| `POST`   | `/api/backups/upload`                                         | Upload backup                  |
| `DELETE` | `/api/backups/{key}`                                          | Delete backup                  |
| `POST`   | `/api/backups/{key}/restore`                                  | Restore backup                 |
| `GET`    | `/api/backups/{key}`                                          | Download backup                |

### Health API
| Method      | Endpoint                                                   | Description                    |
| ----------- | ---------------------------------------------------------- | ------------------------------ |
| `GET`/`HEAD`| `/api/health`                                              | Health check                   |

### Batch API
| Method   | Endpoint                                                      | Description                    |
| -------- | ------------------------------------------------------------- | ------------------------------ |
| `POST`   | `/api/batch`                                                  | Batch operations               |

---

## Appendix B: Error Response Format

All API errors follow this standard format:

```json
{
  "status": 400,
  "message": "Human-readable error summary.",
  "data": {
    "fieldName": {
      "code": "validation_error_code",
      "message": "Field-specific error message."
    }
  }
}
```

### Common HTTP Status Codes

| Code | Meaning                                                       |
| ---- | ------------------------------------------------------------- |
| 200  | Success (with response body)                                  |
| 204  | Success (no response body)                                    |
| 400  | Bad request / validation error / unsatisfied create rule      |
| 401  | Missing or invalid authorization token                        |
| 403  | Insufficient permissions (locked rule for non-superuser)      |
| 404  | Not found / unsatisfied view/update/delete rule               |
| 429  | Rate limited (e.g., OTP requests)                             |

---

## Appendix C: Date Format

All PocketBase dates use RFC 3339 format:

```
Y-m-d H:i:s.uZ
```

Example: `2024-11-19 14:30:00.000Z`

Date filtering requires full datetime strings. For daily queries, use range comparisons:
```
created >= '2024-11-19 00:00:00.000Z' && created < '2024-11-20 00:00:00.000Z'
```

---

## Appendix D: ID Format

- Record and collection IDs are **15-character** strings
- Auto-generated by PocketBase if not provided
- Custom IDs can be specified on create (must be exactly 15 characters)

---

## Appendix E: Key Implementation Notes for Python Reimplementation

1. **Database**: PocketBase uses embedded SQLite. The Python version should use SQLite with an appropriate async driver (e.g., aiosqlite).

2. **Authentication**: Stateless JWT-based auth using HS256 algorithm. Each auth collection has independent token secrets and durations.

3. **Realtime**: SSE (Server-Sent Events) transport. The Python version needs an SSE implementation that supports the two-step connection + subscription model.

4. **File Storage**: Abstraction layer supporting both local filesystem and S3-compatible storage. Files are stored with sanitized names plus random suffixes.

5. **Filter Parser**: PocketBase has a custom filter expression language that compiles to SQL WHERE clauses. This is a critical component that needs a parser/compiler.

6. **API Rules**: Rules are filter expressions evaluated per-request. They access request context (`@request.*`), collection data, and cross-collection data (`@collection.*`).

7. **Batch Operations**: Transactional batch support wrapping multiple operations in a single database transaction.

8. **Collection Types**: Three types (base, auth, view) with different behaviors and system fields. View collections are SQL views, not tables.

9. **Field Modifiers**: The `+` and `-` modifiers for array fields and number fields are a key feature for partial updates.

10. **Relation Expansion**: Auto-expand of related records up to 6 levels deep, respecting View API rules on related collections.
