# PPBase vs PocketBase: Missing Features Gap Analysis

## Summary

This document compares PocketBase's feature set against PPBase's current implementation (v0.1.0, Phase 1+) across operational categories: Settings, Logs, Event Hooks, Job Scheduling, Email, Storage, Thumbnails, Migrations, SMTP, Rate Limiting, and Backups.

**Overall assessment:** PPBase has solid foundations for core CRUD, collections, and admin auth. However, several production-critical operational features are either completely missing or only partially implemented.

---

## 1. Settings API

**PocketBase:** Full settings API at `GET/PATCH /api/settings` with sections for `meta`, `logs`, `smtp`, `s3`, `backups`, `batch`, `rateLimits`, `trustedProxy`. Settings are stored in DB, passwords are auto-redacted in responses, and superuser-only access is enforced. Includes utility endpoints: `POST /api/settings/test/s3` (S3 connection test), `POST /api/settings/test/email` (test email), `POST /api/settings/apple/generate-client-secret`.

**PPBase:** Has `GET/PATCH /api/settings` endpoints (`ppbase/api/settings.py`). Stores settings in `_params` table with correct default structure matching PocketBase (meta, logs, smtp, s3, backups, batch, rateLimits, trustedProxy). Uses admin-only access. Deep-merge on PATCH update works correctly.

**Gap Rating: 🟡 Medium**

| Sub-feature | Status | Notes |
|---|---|---|
| GET/PATCH /api/settings | Implemented | Working, correct structure |
| Password redaction in responses | Missing | SMTP password, S3 secret returned as plaintext |
| POST /api/settings/test/s3 | Missing | No S3 connection test endpoint |
| POST /api/settings/test/email | Missing | No test email endpoint |
| Apple client secret generation | Missing | No Apple OAuth2 support |
| Settings validation | Partial | No validation on PATCH body (any JSON accepted) |
| Settings actually consumed by services | Partial | Settings stored in DB but most services read from `config.py` env vars, not from DB settings |

**Key issue:** The settings API stores values in the DB, but most PPBase services (mail, storage, auth) read configuration from `Settings` (pydantic-settings/env vars) via `config.py`, not from the DB settings row. This means changing settings via the API has no actual effect on behavior. PocketBase uses its DB-stored settings as the runtime config.

---

## 2. Request Logs API

**PocketBase:** Full logging system with `GET /api/logs` (paginated, filterable, sortable), `GET /api/logs/:id` (single log), `GET /api/logs/stats` (hourly aggregated statistics). Logs capture request method, URL, status, execution time, auth info, remote IP, user agent. Configurable retention (maxDays), minimum level, IP/auth logging toggles.

**PPBase:** No logs API endpoints exist. No request logging system. No logs table in the database. The router (`ppbase/api/router.py`) has no logs router. Standard Python `logging` module is used for internal debugging only.

**Gap Rating: 🔴 Critical**

| Sub-feature | Status |
|---|---|
| Request logging middleware | Missing |
| Logs storage (DB table) | Missing |
| GET /api/logs (list) | Missing |
| GET /api/logs/:id (view) | Missing |
| GET /api/logs/stats (statistics) | Missing |
| Log retention/cleanup | Missing |
| Filter/sort on logs | Missing |

**Impact:** No visibility into API usage, no ability to debug issues in production, no audit trail. This is a fundamental operational gap.

---

## 3. Event Hooks System

**PocketBase:** Comprehensive event hook system with 70+ hooks across categories:
- **App hooks:** OnBootstrap, OnServe, OnSettingsReload, OnBackupCreate, OnBackupRestore, OnTerminate
- **Mailer hooks:** OnMailerSend, OnMailerRecordAuthAlertSend, OnMailerRecordPasswordResetSend, OnMailerRecordVerificationSend, OnMailerRecordEmailChangeSend, OnMailerRecordOTPSend
- **Realtime hooks:** OnRealtimeConnectRequest, OnRealtimeSubscribeRequest, OnRealtimeMessageSend
- **Record model hooks:** OnRecordEnrich, OnRecordValidate, OnRecordCreate/Update/Delete (with Execute, AfterSuccess, AfterError variants)
- **Collection model hooks:** OnCollectionValidate, OnCollectionCreate/Update/Delete (with Execute, AfterSuccess, AfterError variants)
- **Request hooks:** OnRecordsListRequest, OnRecordViewRequest, OnRecordCreateRequest, OnRecordUpdateRequest, OnRecordDeleteRequest, plus auth request hooks (password, OAuth2, OTP, verification, password reset, email change)
- **Batch hooks:** OnBatchRequest
- **File hooks:** OnFileDownloadRequest, OnFileTokenRequest
- **Collection request hooks:** OnCollectionsListRequest, OnCollectionViewRequest, OnCollectionCreateRequest, etc.
- **Settings hooks:** OnSettingsListRequest, OnSettingsUpdateRequest
- **Base model hooks:** Generic OnModel* hooks for all DB models

Each hook supports: handler registration with IDs and priority, Bind/BindFunc/Unbind/UnbindAll, event chain via e.Next(), collection-specific filtering.

**PPBase:** No event hook system exists. Services are direct function calls with no interception points. No middleware-based hook alternatives.

**Gap Rating: 🟠 High**

| Sub-feature | Status |
|---|---|
| Hook registration system | Missing |
| App lifecycle hooks | Missing |
| Record CRUD hooks (model + request) | Missing |
| Collection CRUD hooks | Missing |
| Mailer hooks | Missing |
| Realtime hooks | Missing |
| File/settings hooks | Missing |
| Priority/ordering | Missing |
| Collection-specific filtering | Missing |

**Impact:** Users cannot extend PPBase behavior without modifying source code. No way to add custom validation, transform data, send notifications on record changes, implement business logic triggers, or integrate with external services.

**Python equivalent design note:** In a Python/FastAPI context, hooks could be implemented as: (a) FastAPI middleware + dependency injection, (b) signal/event system (like Django signals or blinker), or (c) a custom hook registry. The PocketBase Go hook system with e.Next() maps naturally to a middleware chain pattern.

---

## 4. Job Scheduling (Cron)

**PocketBase:** Built-in cron job scheduler via `app.Cron()`. Features:
- Register jobs with ID, cron expression, and handler function
- Each job runs in its own goroutine
- Supports numeric list, steps, ranges, and macros in cron expressions
- Can remove jobs by ID
- System jobs (log cleanup, auto backups) are built-in
- Jobs viewable and manually triggerable from Dashboard > Settings > Crons

**PPBase:** No job scheduling system exists. No cron functionality. No background task runner.

**Gap Rating: 🟠 High**

| Sub-feature | Status |
|---|---|
| Cron scheduler engine | Missing |
| Job registration API | Missing |
| System jobs (log cleanup) | Missing |
| System jobs (auto backups) | Missing |
| Dashboard cron management | Missing |
| Manual job triggering | Missing |

**Impact:** No automated maintenance tasks (log cleanup, backup rotation), no ability to schedule recurring operations. Users would need external cron or Celery for any scheduled work.

---

## 5. Email Sending

**PocketBase:** Dual-mode email system: `sendmail` fallback + configurable SMTP. Features:
- `app.NewMailClient().Send(message)` factory for custom emails
- Full message support: From, To, CC, BCC, Subject, HTML body, attachments, custom headers
- Template-based system emails (verification, password reset, email change, OTP, auth alert)
- Templates customizable via Dashboard > Collections > Edit collection > Options
- Mailer hooks for intercepting/customizing all email types
- Test email endpoint: `POST /api/settings/test/email`
- SMTP settings stored in DB and configurable via API

**PPBase:** Basic email service exists (`ppbase/services/mail_service.py`). Features:
- SMTP sending via `smtplib.SMTP` with STARTTLS
- Fallback: logs token to stdout when SMTP not configured
- Two email types: verification and password reset
- Hardcoded plain-text templates (no HTML)
- SMTP config from env vars only (not from DB settings)

**Gap Rating: 🟡 Medium**

| Sub-feature | Status | Notes |
|---|---|---|
| SMTP sending | Implemented | Basic, synchronous (blocking) |
| Dev fallback (log to stdout) | Implemented | Works |
| Verification email | Implemented | Plain text only |
| Password reset email | Implemented | Plain text only |
| Email change confirmation | Missing | |
| OTP email | Missing | |
| Auth alert email | Missing | |
| HTML email templates | Missing | Only plain text |
| Customizable templates | Missing | Hardcoded strings |
| Attachments/CC/BCC | Missing | |
| Test email endpoint | Missing | |
| SMTP config from DB settings | Missing | Uses env vars only |
| Async email sending | Missing | Uses synchronous smtplib |
| sendmail fallback | Missing | Only SMTP or log |

**Impact:** Basic email works for development. Production use limited by: no HTML templates, no customizable templates via UI, blocking SMTP calls, and SMTP config not connected to the Settings API.

---

## 6. S3/Storage

**PocketBase:** Full S3-compatible storage integration:
- Toggle between local filesystem and S3 via settings
- Separate S3 config for file storage and backups
- S3 connection test endpoint
- File serving with proper content-type headers
- File token authentication for protected files
- Settings configurable via API (bucket, region, endpoint, accessKey, secret, forcePathStyle)

**PPBase:** Local-only file storage (`ppbase/services/file_storage.py`). Features:
- Files stored at `{data_dir}/storage/{collection_id}/{record_id}/{filename}`
- Save, delete, and delete-all operations
- File serving via `GET /api/files/{collection}/{record}/{filename}` (`ppbase/api/files.py`)
- Config has S3 fields in `config.py` but they are unused

**Gap Rating: 🟠 High**

| Sub-feature | Status | Notes |
|---|---|---|
| Local file storage | Implemented | Working |
| File serving endpoint | Implemented | Basic FileResponse |
| S3 storage backend | Missing | Config fields exist but no implementation |
| S3/local toggle via settings | Missing | |
| S3 connection test | Missing | |
| File token authentication | Missing | All files publicly accessible |
| Thumbnail query parameter | Missing | (see Thumbnails section) |
| Content-type detection | Partial | Relies on FileResponse default |
| Protected file access (rules) | Missing | No access control on file serving |

**Impact:** File storage works locally for development. Production deployments requiring S3 (most cloud deployments) are blocked. No file access control means all uploaded files are publicly accessible regardless of collection rules.

---

## 7. Image Thumbnails

**PocketBase:** Automatic image thumbnail generation via query parameter:
- `GET /api/files/{collection}/{record}/{filename}?thumb=100x100` - fit
- `GET /api/files/{collection}/{record}/{filename}?thumb=100x100t` - top crop
- `GET /api/files/{collection}/{record}/{filename}?thumb=100x100b` - bottom crop
- `GET /api/files/{collection}/{record}/{filename}?thumb=100x100f` - forced resize
- Thumbnails are cached on disk
- Works with both local and S3 storage

**PPBase:** No thumbnail support. Files are served as-is with no transformation.

**Gap Rating: 🟡 Medium**

| Sub-feature | Status |
|---|---|
| Thumbnail generation | Missing |
| Thumbnail caching | Missing |
| Multiple resize modes | Missing |
| thumb query parameter | Missing |

**Impact:** Frontend applications must handle image resizing client-side or via external CDN. Not a blocker but significant for image-heavy applications.

---

## 8. Migrations System

**PocketBase:** Full migration system with:
- `migrate create "name"` CLI command to create blank migration files
- `migrate up` / `migrate down [number]` CLI commands
- `migrate collections` snapshot command
- `migrate history-sync` to clean orphaned entries
- Auto-migration on serve (creates migration files when collections change in Dashboard)
- `Automigrate` config option (typically dev-only)
- Migrations stored as Go files with `init()` + `m.Register(up, down)`
- `_migrations` table tracks applied state
- Migrations can execute raw SQL, modify settings, create collections, etc.

**PPBase:** Has a migration system (`ppbase/services/migration_runner.py`, `ppbase/services/migration_generator.py`, `ppbase/api/migrations.py`). Features:
- API endpoints: GET /api/migrations (list), POST /api/migrations/apply, POST /api/migrations/revert, GET /api/migrations/status, POST /api/migrations/snapshot
- Auto-apply on startup when `auto_migrate=True`
- Migration files stored in `pb_migrations/` directory
- `_migrations` table tracks applied state
- Snapshot generation from current collection state
- Forward and reverse migration support

**Gap Rating: 🟢 Low**

| Sub-feature | Status | Notes |
|---|---|---|
| Migration list/status API | Implemented | |
| Apply migrations API | Implemented | |
| Revert migrations API | Implemented | |
| Snapshot generation | Implemented | |
| Auto-apply on startup | Implemented | |
| CLI migrate commands | Missing | API-only, no CLI commands |
| Automigrate on Dashboard changes | Missing | No auto-create on collection edit |
| history-sync | Missing | |
| Migration files as Python | Implemented | Python instead of Go |

**Impact:** Core migration functionality works. Missing CLI commands and auto-migration on Dashboard changes are minor gaps.

---

## 9. SMTP Configuration

**PocketBase:** SMTP settings stored in DB, configurable via Settings API:
- enabled, host, port, username, password, tls, authMethod (PLAIN/LOGIN), localName
- Password auto-redacted in API responses
- Test email endpoint to verify SMTP config
- All email sending uses DB-stored SMTP settings

**PPBase:** SMTP configured via environment variables only (`config.py`):
- `PPBASE_SMTP_HOST`, `PPBASE_SMTP_PORT`, `PPBASE_SMTP_USERNAME`, `PPBASE_SMTP_PASSWORD`, `PPBASE_SMTP_FROM`
- Settings API has SMTP section in default structure but it is not consumed by the mail service
- No TLS toggle (hardcoded STARTTLS for non-port-25)
- No authMethod option
- No localName/EHLO option
- No password redaction

**Gap Rating: 🟡 Medium**

| Sub-feature | Status | Notes |
|---|---|---|
| SMTP sending | Implemented | Basic |
| SMTP from env vars | Implemented | |
| SMTP from DB settings | Missing | Settings API structure exists but not consumed |
| Test email endpoint | Missing | |
| TLS toggle | Missing | Hardcoded logic |
| AUTH method selection | Missing | |
| EHLO/localName config | Missing | |
| Password redaction | Missing | |

**Impact:** SMTP works for basic cases but is not configurable at runtime via the admin UI. Requires server restart with new env vars to change SMTP settings.

---

## 10. Rate Limiting

**PocketBase:** Built-in rate limiter configurable via Settings API:
- Enable/disable toggle
- Rule-based system with: label (tag, path, or path prefix), maxRequests, duration, audience
- Default rules for auth, create, batch, and general endpoints
- Configurable from Dashboard > Settings > Application

**PPBase:** No rate limiting implementation. The settings API default structure includes `rateLimits` with `enabled` and `rules` fields, but no middleware or enforcement exists.

**Gap Rating: 🟠 High**

| Sub-feature | Status | Notes |
|---|---|---|
| Rate limiter middleware | Missing | |
| Per-rule configuration | Missing | Settings structure exists but not enforced |
| Label/path matching | Missing | |
| Audience filtering | Missing | |
| Dashboard configuration | Missing | |

**Impact:** No protection against API abuse (brute-force auth attempts, excessive record creation, DDoS). This is a significant production security concern.

---

## 11. Backups

**PocketBase:** Full backup system:
- `POST /api/backups` - create backup (ZIP of pb_data)
- `GET /api/backups` - list available backups
- `POST /api/backups/:filename/restore` - restore from backup
- `DELETE /api/backups/:filename` - delete backup
- Automated backups via cron schedule in settings
- Backup to local storage or S3
- Application goes to read-only mode during backup
- Dashboard UI for backup management

**PPBase:** No backup system. No backup API endpoints. No automated backup scheduling. The settings API default structure includes `backups` section but it has no implementation.

**Gap Rating: 🟠 High**

| Sub-feature | Status |
|---|---|
| Create backup API | Missing |
| List backups API | Missing |
| Restore backup API | Missing |
| Delete backup API | Missing |
| Cron-scheduled backups | Missing |
| S3 backup storage | Missing |
| Dashboard backup UI | Missing |
| Read-only mode during backup | Missing |

**Impact:** No built-in disaster recovery. Database backups must be managed entirely externally (pg_dump, Docker volume snapshots, etc.). Since PPBase uses PostgreSQL, the backup strategy differs fundamentally from PocketBase's SQLite file-based approach, but the API surface should still be provided.

---

## 12. Additional Production Features

### Trusted Proxy Headers
**PocketBase:** Configurable trusted proxy headers and leftmost-IP extraction.
**PPBase:** No trusted proxy configuration. Missing.
**Gap Rating: 🟡 Medium**

### Batch API
**PocketBase:** `POST /api/batch` for executing multiple operations in a single transaction.
**PPBase:** Not implemented. Settings structure has batch config but no endpoint.
**Gap Rating: 🟡 Medium**

### Settings Encryption
**PocketBase:** Optional encryption of DB-stored settings via `--encryptionEnv` flag.
**PPBase:** Not implemented.
**Gap Rating: 🟡 Medium**

### Auto TLS (Let's Encrypt)
**PocketBase:** Built-in automatic TLS certificate management via domain name on `serve`.
**PPBase:** Not implemented. Expected to use reverse proxy (nginx/caddy) for TLS.
**Gap Rating: 🟢 Low** (standard Python deployment pattern)

---

## Priority Summary

| Feature | Gap Rating | Blocking? | Effort Estimate |
|---|---|---|---|
| **Request Logs API** | 🔴 Critical | Yes - no observability | Medium |
| **Rate Limiting** | 🟠 High | Yes - security risk | Medium |
| **Event Hooks System** | 🟠 High | Yes - no extensibility | Large |
| **S3 Storage Backend** | 🟠 High | Yes - cloud deployment | Medium |
| **Backups** | 🟠 High | Yes - no disaster recovery | Large (different strategy for PG) |
| **Job Scheduling** | 🟠 High | Partial - no automated maintenance | Medium |
| **Email (full)** | 🟡 Medium | No - basic works | Small-Medium |
| **Settings API (functional)** | 🟡 Medium | No - env vars work | Medium |
| **SMTP Configuration** | 🟡 Medium | No - env vars work | Small |
| **Image Thumbnails** | 🟡 Medium | No - client-side workaround | Medium |
| **Batch API** | 🟡 Medium | No | Medium |
| **Trusted Proxy** | 🟡 Medium | No | Small |
| **Settings Encryption** | 🟡 Medium | No | Small |
| **Migrations (CLI)** | 🟢 Low | No - API works | Small |
| **Auto TLS** | 🟢 Low | No - reverse proxy pattern | N/A |

---

## Recommended Phase 2 Priority Order

1. **Request Logs** - Fundamental for debugging and monitoring
2. **Rate Limiting** - Security baseline for production
3. **S3 Storage** - Unblocks cloud deployments
4. **Settings consumed by services** - Connect DB settings to runtime behavior
5. **Event Hooks** - Enables extensibility without source modification
6. **Job Scheduling** - Enables automated log cleanup, backup scheduling
7. **Backups** - PostgreSQL-appropriate backup/restore via pg_dump
8. **Full Email System** - HTML templates, async sending, all email types
9. **Image Thumbnails** - Pillow-based generation with caching
10. **Batch API** - Transaction-wrapped multi-operation endpoint
