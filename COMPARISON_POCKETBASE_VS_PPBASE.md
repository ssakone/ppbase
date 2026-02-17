# PocketBase vs PPBase — Comprehensive Comparison Report

> Analysis date: 2026-02-17
> Method: 4 parallel Opus 4.6 agents cross-referencing official PocketBase docs against PPBase source code
> PPBase version: v0.1.0 (Phase 1+)

---

## Executive Summary

| Domain | Coverage | Grade |
|--------|----------|-------|
| Records CRUD | ~95% | A |
| Collections CRUD | ~90% | A- |
| Auth Flows | ~58% | C+ |
| Filter/Rule Syntax | ~64% | C+ |
| Realtime SSE | ~63% | C+ |
| Field Types | ~90% | A- |
| File Handling | ~50% | D+ |
| Operational Features | ~20% | F |
| **Overall Compatibility** | **~70%** | **C+** |

**Verdict:** PPBase is production-ready for basic CRUD applications. It is **not yet production-ready** for applications requiring: auth flows (OTP, MFA, email change), file security (protected files, thumbnails), operational observability (logs, rate limiting, backups), or extensibility (hooks).

---

## 1. API Endpoints

**20/26 endpoints implemented (~83%)**

### Records CRUD — ✅ 5/5

All 5 CRUD endpoints fully implemented with full query parameter support:
- `GET /api/collections/{c}/records` — filter, sort, pagination, expand, fields, skipTotal ✅
- `GET /api/collections/{c}/records/{id}` — expand, fields, viewRule enforcement ✅
- `POST /api/collections/{c}/records` — JSON + multipart, createRule, file upload ✅
- `PATCH /api/collections/{c}/records/{id}` — partial update, updateRule ✅
- `DELETE /api/collections/{c}/records/{id}` — deleteRule, cascade ✅

**Minor gaps:** `:excerpt()` field modifier, `@collection.*` cross-collection filter, `POST /api/batch` (transactional batch ops).

### Auth Record Endpoints — ⚠️ 8/12 (~65%)

| Endpoint | Status |
|----------|--------|
| `GET auth-methods` | ✅ (missing MFA/OTP sections) |
| `POST auth-with-password` | ✅ |
| `POST auth-with-oauth2` | ✅ |
| `POST auth-refresh` | ✅ |
| `POST request-verification` | ✅ |
| `POST confirm-verification` | ✅ |
| `POST request-password-reset` | ✅ |
| `POST confirm-password-reset` | ✅ |
| `POST request-otp` | ❌ Missing |
| `POST auth-with-otp` | ❌ Missing |
| `POST request-email-change` | ❌ Missing |
| `POST confirm-email-change` | ❌ Missing |
| `POST impersonate/{id}` | ❌ Missing |

### Collections CRUD — ✅ 5/5 + import + truncate (method mismatch)

All collection endpoints work. Two issues:
- `DELETE /api/collections/{c}/truncate` → PPBase uses **POST** (HTTP method mismatch)
- `GET /api/collections/meta/scaffolds` → ❌ Missing (used by Dashboard)
- `fields` and `skipTotal` query params not wired on collection list endpoint

---

## 2. Authentication Flows

**36/62 features implemented (~58%)**

### ✅ Fully Working
- **Password auth** — bcrypt, configurable identity fields, per-collection token secrets
- **OAuth2** — 5 providers (Google, GitHub, GitLab, Discord, Facebook), PKCE S256, `_externalAuths` linking, mapped fields
- **Email verification** — request + confirm, anti-enumeration (always 204)
- **Password reset** — request + confirm, token_key rotation
- **Auth refresh** — stateless JWT, per-collection duration
- **Token isolation** — forged tokens with unknown collectionId rejected with 404

### ❌ Missing
| Feature | Impact |
|---------|--------|
| **OTP auth** (`_otps` table exists, no API) | Blocks MFA implementation |
| **MFA flow** (`_mfas` table exists, no 2FA challenge) | Cannot require 2nd factor |
| **Email change** (request + confirm) | Users cannot update email |
| **Impersonation** | No superuser-as-user debug flow |
| **3+ more OAuth2 providers** (Microsoft, Apple, Twitter, Spotify, Twitch…) | Limited provider choice |
| **OAuth2 redirect page** (`/api/oauth2-redirect`) | SDK popup flow broken |
| **`passwordAuth.enabled` enforcement** | Cannot disable password login per-collection |
| **`manageRule` enforcement** | Cannot allow one user to manage another |

---

## 3. Realtime SSE

**15/24 features implemented (~63%)**

### ✅ Working
- SSE connection with `PB_CONNECT` event + `clientId`
- `id:` field in SSE events (required by PocketBase SDK)
- PostgreSQL LISTEN/NOTIFY via direct asyncpg connection (bypasses SQLAlchemy wrapper)
- Collection-wide (`collection/*`) and single-record (`collection/id`) subscriptions
- Create/update/delete events broadcast with correct `{action, record}` payload
- SSE event name = subscription topic (critical for SDK compatibility)
- Keepalive to prevent idle disconnect

### ❌ Missing / ⚠️ Partial
| Issue | Severity |
|-------|----------|
| **No auth enforcement on events** — listRule/viewRule not checked before broadcasting | 🔴 Security gap |
| **Subscription replacement semantics** — PPBase adds incrementally, PB replaces atomically | ⚠️ SDK compat issue |
| **`options` query param on subscriptions** — expand/fields per subscription not supported | ⚠️ |
| **Auth consistency check (403)** on subscribe | ⚠️ (TODO in code) |

---

## 4. Filter & Rule Syntax

**41/64 features implemented (~64%)**

### ✅ Fully Working
All core filter operators: `=`, `!=`, `>`, `>=`, `<`, `<=`, `~`, `!~`, `?=`, `?!=`, `?~`, `?!~`, `&&`, `||`, `(...)`, string/number/bool/null literals, `@now`, `@request.auth.*`, `@request.body.*`, `@request.query.*`, `@random`, `@rowid`, `@collection.*` EXISTS subqueries, relation field traversal (single-hop).

### ❌ Missing
| Missing Feature | Impact |
|----------------|--------|
| **Field modifiers** (`:isset`, `:changed`, `:length`, `:each`, `:lower`) | Cannot express many common rules |
| **Datetime macros** (`@yesterday`, `@todayStart`, `@monthStart`, `@yearStart`, etc.) | Date-based rules limited to `@now` only |
| **`@request.context`** | Cannot distinguish oauth2/otp/realtime auth contexts |
| **`@request.method`**, **`@request.headers.*`** | Header/method-based rules impossible |
| **Multi-level relation traversal** (`a.b.c`) | Raises error — only single-hop works |
| **Back-relation filter** (`collection_via_field.x`) | Back-relation queries impossible |
| **`geoDistance()` function** | Location-based filters impossible |
| **`strftime()` function** | Date formatting in filters impossible |
| **Collection aliases** (`:alias` suffix) | Cannot join same collection twice |
| **`manageRule` enforcement** | Always skipped |

---

## 5. Field Types

**14/14 types present, ~90% feature complete**

| Field Type | Status | Key Gap |
|-----------|--------|---------|
| Text | ✅ | Missing `AutogeneratePattern` / `:autogenerate` |
| Editor | ✅ | Missing `convertURLs` option |
| Number | ✅ | — |
| Bool | ✅ | — |
| Email | ✅ | — |
| URL | ✅ | — |
| Date | ✅ | — |
| Autodate | ⚠️ | `onCreate`/`onUpdate` granularity not respected |
| Select | ⚠️ | Missing `+field` prepend modifier |
| File | ⚠️ | Missing maxSize/MIME validation, thumbnails, protected mode |
| Relation | ⚠️ | Missing `minSelect`, FK existence checks, `+field` prepend |
| JSON | ✅ | — (recently fixed `__json` → `_json` typo) |
| Password | ✅ | — |
| GeoPoint | ✅ | — |

**Cross-field gap:** `+field` prepend modifier missing for Select, File, Relation (only `field+` suffix works).

---

## 6. Collection Types

| Type | Status | Details |
|------|--------|---------|
| **Base** | ✅ Full | All CRUD, rules, indexes, dynamic DDL |
| **View** | ✅ Full | CREATE VIEW, validation, read-only enforcement |
| **Auth** | ⚠️ Schema only | System columns + token secrets done; all user-facing flows (registration, login, OAuth2, email verification, password reset, MFA) implemented |

---

## 7. File Handling

**~50% feature complete — significant gaps for production**

| Feature | Status |
|---------|--------|
| Local filesystem storage | ✅ |
| Multipart upload | ✅ |
| `field+` append, `field-` remove | ✅ |
| File serving (`GET /api/files/…`) | ✅ (basic) |
| `+field` prepend | ❌ |
| `maxSize` per-file enforcement | ❌ |
| MIME type validation on upload | ❌ |
| **Thumbnails** (`?thumb=WxH`) | ❌ |
| `?download=1` flag | ❌ |
| **Protected files** (requires auth token) | ❌ (all files publicly accessible) |
| **File token API** (`POST /api/files/token`) | ❌ |
| **S3 storage backend** | ❌ (config scaffold only) |
| Filename format (original name + suffix) | ⚠️ (uses uuid hex, discards original name) |

> ⚠️ **Security gap:** All uploaded files are publicly accessible regardless of collection rules, since there is no protected file access control.

---

## 8. Back-Relations

**Entirely missing — a major PocketBase feature**

| Feature | Status |
|---------|--------|
| `expand=comments_via_post` | ❌ |
| `filter=comments_via_post.text ~ "hello"` | ❌ |
| `sort=comments_via_post.created` | ❌ |
| View API rule check on expanded records | ❌ |

---

## 9. Operational / Production Features

**~20% coverage — the biggest gap for production use**

| Feature | Rating | Status |
|---------|--------|--------|
| **Request Logs API** | 🔴 Critical | ❌ No logging whatsoever (`GET /api/logs`, `/api/logs/stats`) |
| **Rate Limiting** | 🟠 High | ❌ Settings structure exists, no enforcement |
| **Event Hooks** (70+ hooks) | 🟠 High | ❌ Zero hooks — no extensibility |
| **S3 Storage** | 🟠 High | ❌ Config fields exist, no implementation |
| **Backups** | 🟠 High | ❌ No backup/restore API |
| **Job Scheduling** | 🟠 High | ❌ No cron/scheduler |
| **Settings → Services connection** | 🟡 Medium | ❌ DB settings not consumed by services (env vars only) |
| **Email system (full)** | 🟡 Medium | ⚠️ Basic SMTP works; no HTML templates, no async, no OTP/alert emails |
| **Image Thumbnails** | 🟡 Medium | ❌ No thumbnail generation |
| **Batch API** | 🟡 Medium | ❌ `POST /api/batch` not implemented |
| **SMTP from DB settings** | 🟡 Medium | ❌ Env vars only |
| **Trusted Proxy** | 🟡 Medium | ❌ |
| **Password redaction in settings** | 🟡 Medium | ❌ Plaintext in API responses |
| **Migrations CLI** | 🟢 Low | ⚠️ API works, CLI commands missing |
| **Auto TLS** | 🟢 Low | ❌ (use reverse proxy — standard Python pattern) |

---

## 10. PPBase Advantages Over PocketBase

| Feature | Notes |
|---------|-------|
| **PostgreSQL backend** | Full ACID, native JSONB, better concurrency, horizontal scale |
| **LISTEN/NOTIFY realtime** | More reliable than SQLite-based triggers |
| **Python ecosystem** | Full access to pip packages, FastAPI middleware, SQLAlchemy |
| **Async-first** | FastAPI + asyncpg handles more concurrent connections |
| **Custom endpoint** | `GET /api/collections/meta/tables` for SQL editor autocomplete |
| **No single binary limitation** | Deploy as standard Python service |

---

## 11. Recommended Implementation Roadmap

### Phase 2A — Security & Observability (do first)
1. **Request Logs** — middleware + DB table + `GET /api/logs` + stats
2. **Rate Limiting** — FastAPI middleware consuming DB settings rules
3. **Protected Files** — File token API + access control on file serving
4. **Realtime auth enforcement** — check listRule/viewRule before sending SSE events
5. **Fix truncate HTTP method** — `POST` → `DELETE`

### Phase 2B — Auth Completion
6. **OTP auth** — `request-otp` + `auth-with-otp` + `_otps` table usage
7. **MFA flow** — 401+mfaId challenge, second factor verification
8. **Email change** — request + confirm endpoints
9. **Impersonation** — superuser-only non-refreshable token
10. **`manageRule` enforcement**

### Phase 2C — Filter & Rules Power
11. **Filter field modifiers** (`:isset`, `:changed`, `:length`, `:each`)
12. **Datetime macros** (`@yesterday`, `@todayStart`, etc.)
13. **`@request.context`**, **`@request.headers.*`**
14. **Multi-level relation traversal** (`a.b.c` in filters)
15. **Back-relations** (`_via_` in expand/filter/sort)

### Phase 2D — Storage & Operations
16. **S3 storage backend** — boto3/aiobotocore
17. **Image thumbnails** — Pillow + disk cache
18. **Batch API** — transactional multi-op endpoint
19. **Backups** — pg_dump wrapper + restore + API
20. **Job Scheduler** — APScheduler or Celery Beat for log cleanup, backup rotation
21. **Settings → Services connection** — reload runtime config from DB on PATCH

### Phase 2E — Polish
22. **Event Hooks system** — FastAPI-compatible hook registry (blinker or custom)
23. **`+field` prepend modifier** — Select, File, Relation
24. **TextField autogenerate**
25. **More OAuth2 providers** — Microsoft, Apple, Twitter, Spotify
26. **Relation FK existence checks**
27. **File maxSize/MIME validation**
28. **Subscription replacement semantics** (atomic replace on POST)

---

## Appendix: File Locations

Individual detailed reports:
- `comparison_api.md` — Records & Collections endpoint-by-endpoint analysis
- `comparison_auth.md` — Auth flows, Realtime SSE, Filter syntax
- `comparison_fields.md` — Field types, collection types, files, relations
- `comparison_gaps.md` — Operational features gap analysis with effort estimates
