# Production Deployment

This guide covers a practical production setup for PPBase.

## 1) Preflight checklist

- PostgreSQL is externalized and backed up.
- `PPBASE_JWT_SECRET` is explicitly set (do not rely on auto-generated secret for production).
- CORS origins are restricted (`PPBASE_ORIGINS` not `*`).
- SMTP is configured and validated (`POST /api/settings/test/email`).
- Storage backend is validated (local disk or S3/R2).
- Reverse proxy (TLS termination) is in front of PPBase.
- PostgreSQL 16 or 17 is reachable from the PPBase process.
- PPBase is installed from the official wheel for the deployment platform, so
  the verified PostgreSQL client payload is present.

Docker is not required in production. The `ppbase db` Docker helper is only a
local-development convenience; PPBase and native backup/restore work with a
normal reachable PostgreSQL service.

PyPI exposes only the `ppbase` project/version. On Linux glibc 2.28+
(x86_64/ARM64) and macOS 15+ (Intel/Apple Silicon), `pip install ppbase`
selects the matching wheel and installs its private `pg_dump`, `pg_restore`,
`psql`, shared libraries, licenses and provenance. Alpine/musl, Windows and
macOS 14 or older are outside the supported release matrix and fail explicitly
on the current release instead of silently installing an old universal wheel.

## 2) Recommended runtime config

Example env:

```bash
export PPBASE_DATABASE_URL='postgresql+asyncpg://...'
export PPBASE_HOST='0.0.0.0'
export PPBASE_PORT='8090'
export PPBASE_JWT_SECRET='replace-with-strong-secret'
export PPBASE_ORIGINS='https://app.example.com,https://admin.example.com'
export PPBASE_DATA_DIR='/var/lib/ppbase'
export PPBASE_BACKUP_ROOT='/var/lib/ppbase-backups'
export PPBASE_BACKUP_CONTROL_DIR='/var/lib/ppbase-backup-control'
export PPBASE_MIGRATIONS_DIR='/srv/ppbase/pb_migrations'
export PPBASE_APPLY_MIGRATIONS_ON_START='true'
export PPBASE_GENERATE_MIGRATIONS='false'
export PPBASE_MIGRATION_LOCK_TIMEOUT='60'
```

Start:

```bash
python -m ppbase serve \
  --db "$PPBASE_DATABASE_URL" \
  --host 0.0.0.0 \
  --port 8090 \
  --dir "$PPBASE_DATA_DIR" \
  --migrationsDir "$PPBASE_MIGRATIONS_DIR"
```

For a deployment built directly from a source clone, build the Admin UI once
after clone and rebuild it after frontend changes before restarting PPBase:

```bash
cd admin-ui
npm ci
npm run build
```

The build publishes assets into `ppbase/admin/dist`; installed wheels use their
packaged assets.

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
- Startup order is interrupted destructive-restore recovery → bootstrap hooks
  → internal schema/stable `users` collection → application migrations →
  realtime/serve hooks → HTTP traffic.
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

Use the signed native workflow documented in
[Native Backup & Restore](./native-backup-restore.md). Backup and restore use
`PPBASE_DATABASE_URL` with the same role that runs `serve`; no separate
provisioning is required. Restore readiness is stricter than backup readiness:
the runtime must be a superuser or own the database and `public`, and the live
server must expose its reusable restart command.

Both commands below are optional and independent. `init postgres` creates one
fresh project database, its runtime role and safe filesystem roots. `backup
provision` adds one least-privilege dump role to a fresh or existing application
database:

```bash
# Optional fresh-project onboarding (runtime role only)
PPBASE_POSTGRES_BOOTSTRAP_DATABASE_URL='postgresql+asyncpg://...' \
  ppbase init postgres --plan --name myapp --output-env /etc/ppbase/myapp.env
PPBASE_POSTGRES_BOOTSTRAP_DATABASE_URL='postgresql+asyncpg://...' \
  ppbase init postgres --execute --name myapp --output-env /etc/ppbase/myapp.env

# Optional dump-role hardening (fresh or existing database)
ppbase backup provision --plan \
  --db "$PPBASE_DATABASE_URL"

PPBASE_BACKUP_BOOTSTRAP_DATABASE_URL='postgresql+asyncpg://bootstrap:...@db/postgres' \
  ppbase backup provision --execute \
  --db "$PPBASE_DATABASE_URL" \
  --output-env /etc/ppbase/backup.env
```

Both execute commands require an ephemeral PostgreSQL superuser DSN. The init
output is exclusive mode `0600` and contains only
`PPBASE_DATABASE_URL`; the backup-provision output contains only
`PPBASE_BACKUP_DUMP_DATABASE_URL`. Bootstrap credentials are never persisted.
Default PPBase filesystem roots are created safely and need no manual `mkdir` or
`chmod`.

`backup provision --execute` enforces a real database-level boundary for the
dedicated credential. It revokes database privileges inherited from `PUBLIC`
across the PostgreSQL cluster, then grants the dump role only `CONNECT` on the
active application database. It also removes `PUBLIC` write/default privileges
and routine execution in that database's `public` schema before granting the
dump role `USAGE` and `SELECT`. Database/schema owners keep their inherent
owner privileges. On a shared cluster, review the read-only plan and ensure
non-owner service roles do not intentionally depend on permissive `PUBLIC`
ACLs before executing it.

The bootstrap DSN exists only in the execute process and is never written to
the mode-`0600` output. Configure the service manager or secret manager to load
`/etc/ppbase/backup.env` on every PPBase start, then restart the service. A
`source` command in another terminal cannot update an already running server.
Never commit the generated file.

Run PPBase as a dedicated non-root user and keep `PPBASE_BACKUP_CONTROL_DIR`
private (`0700`). Before relying on the workflow, rehearse a full restore on a
staging host: create a backup, restore it destructively, and confirm the server
restarts and comes back healthy.

Run the live readiness check after restart. It reports `backupReady` and
`restoreReady` independently; its process exit status follows backup creation
readiness so restore-only blockers remain visible without disabling backups:

```bash
ppbase backup doctor \
  --db "$PPBASE_DATABASE_URL" \
  --dir "$PPBASE_DATA_DIR" \
  --server http://127.0.0.1:8090
```

Without `--server`, restart capability is intentionally reported as
partial/SKIP because a standalone doctor process cannot observe the restart
configuration injected into the server process.

For an existing application, `backup provision` accepts `--db`; `backup doctor`
also accepts `--dir` as a compatibility override matching `serve`. A PostgreSQL
superuser runtime is supported without mutation across backup and restore;
doctor and the Dashboard report the non-blocking `runtime_superuser` warning.
The optional dump role remains read-only and never needs `CREATEDB`.

The selected database and `data_dir` are one inseparable runtime target. If the
database references a local business file that is absent from
`<data_dir>/storage`, backup creation fails safely and does not publish a
partial archive. Do not silence this by pointing at an unrelated directory;
repair or import the matching files.

Keep PPBase application objects in the PostgreSQL `public` schema. Native
archives intentionally contain only `public`, and restore rejects a multi-schema
archive before mutation; unrelated schemas are not backed up or replaced.

Native restore is not an import or merge. After fully verifying the archive,
PPBase blocks writes, replaces the active database and local `data_dir` storage
with the backup's contents, commits a recovery journal and restarts. Before any
hooks or HTTP traffic, startup recovery verifies/finalizes the corresponding
files and then normal startup applies newer migrations. A pre-commit failure
rolls back PostgreSQL and restores the previous files; a post-commit restart
failure remains fenced until PPBase is restarted and recovery succeeds.

Native backups intentionally cover only the application objects in `public`;
PostgreSQL large objects and objects in other schemas are outside this contract.

The native v1 backup engine covers PostgreSQL and local business files. If the
business-file backend is S3/R2, maintain an independent bucket recovery policy;
PPBase does not currently store native backup archives in S3.

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
