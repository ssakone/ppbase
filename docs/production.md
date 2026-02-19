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

- Keep migrations in VCS under `pb_migrations/` (or your custom `--migrationsDir`).
- Use `PPBASE_AUTO_MIGRATE=true` only if your deployment process expects startup-time migration.
- For controlled rollout:
  1. run `python -m ppbase migrate status`
  2. run `python -m ppbase migrate up`
  3. start/restart app instances

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
