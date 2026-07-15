# Production Deployment

This guide covers a practical production setup for PPBase.

## 1) Preflight checklist

- PostgreSQL is externalized and backed up.
- `PPBASE_JWT_SECRET` is explicitly set (do not rely on auto-generated secret for production).
- CORS origins are restricted (`PPBASE_ORIGINS` not `*`).
- SMTP is configured and validated (`POST /api/settings/test/email`).
- Storage backend is validated (local disk or S3/R2).
- Reverse proxy (TLS termination) is in front of PPBase.

## 2) Recommended runtime config

Example env:

```bash
export PPBASE_DATABASE_URL='postgresql+asyncpg://...'
export PPBASE_HOST='0.0.0.0'
export PPBASE_PORT='8090'
export PPBASE_JWT_SECRET='replace-with-strong-secret'
export PPBASE_ORIGINS='https://app.example.com,https://admin.example.com'
export PPBASE_DATA_DIR='/var/lib/ppbase'
export PPBASE_MIGRATIONS_DIR='/srv/ppbase/pb_migrations'
export PPBASE_APPLY_MIGRATIONS_ON_START='true'
export PPBASE_GENERATE_MIGRATIONS='false'
export PPBASE_MIGRATION_LOCK_TIMEOUT='60'
```

Start:

```bash
python -m ppbase serve --host 0.0.0.0 --port 8090 --dir /var/lib/ppbase --migrationsDir /srv/ppbase/pb_migrations
```

## 3) Reverse proxy and TLS

Run PPBase behind Nginx/Caddy/Traefik and terminate TLS there.

Requirements:

- Forward `Authorization` header unchanged.
- Keep SSE streaming enabled for `/api/realtime` (no buffering).
- Preserve client IP headers if you rely on `trustedProxy` rate-limit settings.

## 4) Workers and scaling notes

Current behavior to know before horizontal scaling:

- Realtime sessions are in-process memory (`SubscriptionManager`).
- API rate limit buckets are in-process memory.

Implications:

- Multiple workers/instances can cause non-shared realtime session state.
- Rate limiting is enforced per-process, not globally.

Recommended now:

- Prefer single app worker per deployment unit for predictable realtime behavior.
- If scaling out, use sticky sessions at the proxy and validate realtime behavior under load.

## 5) Migrations and rollout

- PPBase supplies the runner; the consuming backend owns and versions
  `pb_migrations/` (or its custom `--migrationsDir`). The ignored directory in
  the PPBase framework checkout is not packaged and is unrelated to a
  consumer application's migration history.
- After pre-database bootstrap hooks, startup order is internal schema → stable
  `users` collection → application migrations → realtime/serve hooks → HTTP
  traffic.
- PostgreSQL metadata, DDL, and `_migrations` history are atomic per file. If
  migration N fails, N is rolled back and startup stops, while already committed
  migrations 1..N-1 remain applied.
- `MigrationApp` helpers, `execute_sql()`, and the exposed `app.engine`/session
  facades are bound to that transaction. Code that deliberately constructs a
  separate SQLAlchemy engine or external database connection is outside this
  guarantee. Root transaction SQL (`COMMIT`, `END`, `ROLLBACK`, `ABORT`,
  `BEGIN`, `START TRANSACTION`, and `PREPARE TRANSACTION`) is rejected on all
  three public SQL surfaces; migration code must leave transaction ownership to
  the runner.
- A database-scoped advisory lock serializes runners and Dashboard producers.
  Set `PPBASE_MIGRATION_LOCK_TIMEOUT` long enough for the largest expected
  rollout; the timeout also includes waiting for a pool connection. Collection
  schema mutations take this lock even when migration-file generation is
  disabled; `generate_migrations` controls only file creation.
- Use direct PostgreSQL connections or PgBouncer **session pooling** for
  migrations. PgBouncer transaction/statement pooling is unsupported because
  PPBase intentionally holds a session advisory lock across per-file commits.
- PostgreSQL and the migration directory do not form a distributed transaction.
  Generated files are published first as durable intent. A crash can leave a
  pending file that is applied later; an ambiguous COMMIT keeps the file and is
  reconciled from `_migrations` when possible. Run `migrate status` before any
  manual retry, especially before repeating a relative `migrate down 1`.
- Recovery from a generated file assumes the producer and later runner see the
  same durable migration directory. Do not enable Dashboard generation across
  instances with unrelated local filesystems.
- A compatible parent-view query change uses PostgreSQL `CREATE OR REPLACE VIEW`
  so child views remain present. An incompatible output-column change is
  rejected while dependent views exist; update/delete those dependents first.
- Disable `PPBASE_GENERATE_MIGRATIONS` in production; generate and review files
  during development.
- For controlled rollout:
  1. run `python -m ppbase migrate status`
  2. run `python -m ppbase migrate up`
  3. start/restart app instances

`PPBASE_AUTO_MIGRATE` remains as a compatibility fallback when the two explicit
application/generation settings are unset.

`migrate status` is read-only and does not initialize a blank database.
`migrate snapshot` is an extend-mode schema baseline with a deliberately no-op
rollback; it is not a substitute for the PostgreSQL backup below.

## 6) Backups

Minimum backup scope:

- PostgreSQL database dump/snapshot.
- `data_dir` only if using local file storage.
- If using S3/R2, ensure bucket lifecycle/versioning policy matches your recovery target.

## 7) Health and smoke checks

After deploy, verify:

- `GET /api/health`
- Admin UI loads at `/_/`
- Auth login/refresh works
- Realtime subscription works (`/api/realtime`)
- File upload/download works (`/api/files/...`)

## 8) Security hardening

- Restrict admin setup and rotate bootstrap/setup links quickly.
- Use strong secrets for JWT and OAuth/SMTP credentials.
- Restrict network access to PostgreSQL.
- Run regular dependency updates and image patching.

## 9) Logging and observability

- Persist process logs (stdout/stderr) with rotation.
- Monitor HTTP `4xx/5xx` rates.
- Track DB connectivity and pool saturation.
- Alert on repeated auth failures and SMTP/storage errors.
