# Native backup and restore

PPBase exposes a signed PostgreSQL backup workflow in **Settings → Backups**.
The browser experience follows PocketBase (create, ZIP download, ZIP upload,
restore and delete), while restoration remains PostgreSQL-specific: PPBase
always restores into a new database and a new data directory before activation.

## What the Dashboard can do

- Create a signed local backup.
- Download a standard `.zip` file.
- Upload and inspect a PPBase ZIP from another server.
- Quarantine an unknown Ed25519 signer and approve its exact public key.
- Restore in disaster-recovery or clone mode.
- Apply missing deployed migrations only to the staged database.
- Validate PostgreSQL, files, migrations and JWT state.
- Activate the validated target, restart PPBase and poll the health gate.
- Roll back automatically to the previous database and data directory on
  startup failure or health timeout.
- Delete backups that are not currently in use.
- Configure a five-field UTC cron and automatic-backup retention.

## Canonical storage and transport

The canonical server-side representation remains an immutable backup set below
`PPBASE_BACKUP_ROOT`. The browser receives a standard ZIP generated from that
set. A ZIP contains at least:

```text
manifest.json
manifest.sig
signer.pub
resources/database.dump
resources/files/...
resources/secrets/jwt_secret
```

The Ed25519 private key, trust store, staging plans, activation journal and
PostgreSQL credentials are never exported. ZIP upload is streamed and bounded;
ZIP Slip, duplicate members, special files, symlinks, compression bombs,
unknown manifest versions, invalid signatures and checksum mismatches are
rejected before publication.

PocketBase SQLite archives containing `data.db` are not native PPBase backups.
PPBase only requires PostgreSQL and matching `pg_dump`, `pg_restore`, and
`psql` tools. Docker is optional tooling for local development and integration
tests, not a runtime or backup/restore dependency.

## Required deployment configuration

The Dashboard never receives or persists PostgreSQL credentials. Configure the
following deployment secrets before using native backup and restore:

| Variable | Purpose |
|---|---|
| `PPBASE_BACKUP_DUMP_DATABASE_URL` | Dedicated read-only source login used by `pg_dump`. |
| `PPBASE_BACKUP_CREATOR_DATABASE_URL` | `CREATEDB` login connected to a maintenance database. |
| `PPBASE_BACKUP_RESTORE_DATABASE_URL` | Non-`CREATEDB` login used by `pg_restore`. |
| `PPBASE_BACKUP_TARGET_OWNER` | Dedicated `NOLOGIN` owner for restored objects. |
| `PPBASE_BACKUP_TARGET_ROOT` | Durable `0700` root for isolated restored `data_dir` targets; must not overlap staging, backups or control-plane. |
| `PPBASE_BACKUP_ALLOWED_EXTENSIONS` | Exact `name=version` allowlist; `plpgsql=1.0` is the default. |

Settings → Backups displays compact, non-secret readiness for creation,
restore staging and restart. A ready deployment is summarized as
`All set for backup & restore`; real blockers alone remain expanded, while
secondary diagnostics are available under `Show details`. Readiness only
confirms that deployment inputs are present; every operation performs the
complete PostgreSQL privilege, endpoint, version, locale, extension and
membership preflight again.

An unprovisioned PostgreSQL cluster cannot be safely bootstrapped from an HTTP
Dashboard without giving the web runtime a privileged cluster credential.
PPBase deliberately does not do that. For a completely new project, initialize
the database, runtime role, backup roles and safe default filesystem roots once
from the host:

```bash
PPBASE_POSTGRES_BOOTSTRAP_DATABASE_URL='postgresql+asyncpg://...' \
  ppbase init postgres --plan --name myapp --output-env ./ppbase.env
PPBASE_POSTGRES_BOOTSTRAP_DATABASE_URL='postgresql+asyncpg://...' \
  ppbase init postgres --execute --name myapp --output-env ./ppbase.env
```

For a database and runtime role that already exist, provision only the backup
contract. Pass the exact same application database and `data_dir` used by
`serve`:

```bash
export PPBASE_DATABASE_URL='postgresql+asyncpg://runtime:...@db/myapp'
export PPBASE_DATA_DIR='/var/lib/ppbase/pb_data'

ppbase backup provision --plan \
  --db "$PPBASE_DATABASE_URL" \
  --dir "$PPBASE_DATA_DIR"

PPBASE_BACKUP_BOOTSTRAP_DATABASE_URL='postgresql+asyncpg://bootstrap:...@db/postgres' \
  ppbase backup provision --execute \
  --db "$PPBASE_DATABASE_URL" \
  --dir "$PPBASE_DATA_DIR" \
  --output-env /etc/ppbase/backup.env
```

`backup provision` and `backup doctor` accept the same runtime target
overrides as `serve`: `--db "$PPBASE_DATABASE_URL" --dir "$PPBASE_DATA_DIR"`.
These options select the application database and data directory only;
`backup provision --execute` still requires the separate ephemeral bootstrap
credential and an explicit `--output-env` sink.

The bootstrap DSN is ephemeral and is never written to the output file, logs,
Dashboard or activation journal. The output file is created exclusively with
mode `0600` and contains only the dedicated limited credentials that must then
be supplied through the deployment environment, a secret manager or that
private file. Configure the service manager to load it on every start (for
example, systemd `EnvironmentFile=/etc/ppbase/backup.env`) and restart PPBase;
sourcing it in an unrelated shell does not update an already running process.
Never commit this file. `--bootstrap-dsn-file` accepts a mode-`0600` file when
an environment variable is unsuitable. Existing manually provisioned roles
remain supported; unsafe name/attribute collisions are refused rather than
repaired.

After restart, validate the live process from an environment that also has the
runtime database and backup variables:

```bash
ppbase backup doctor \
  --db "$PPBASE_DATABASE_URL" \
  --dir "$PPBASE_DATA_DIR" \
  --server http://127.0.0.1:8090
```

`--server` asks the live PPBase process whether activation restart is actually
configured. Standalone doctor runs mark this check as SKIP/WARN and do not
produce a false Not-ready result.

For compatibility with an existing deployment, the application runtime may be
a PostgreSQL superuser under any role name. PPBase never changes that role's
name, password or attributes and reports the stable non-blocking warning
`legacy_runtime_superuser`. Provisioning, doctor, backup, staging restore,
activation and subsequent backups all use the same exception. The dump,
creator, restore and target-owner roles remain subject to the strict limited
contract below, and every unrelated contract violation remains blocking.

The database and `data_dir` form one runtime target and must always match. In
particular, every local file referenced by PostgreSQL must exist below the
selected `<data_dir>/storage`. If the wrong directory is supplied or a file is
missing/unsafe, creation stops with `backup_integrity_failed`, removes the
partial set, and publishes no backup. Pointing PPBase at another directory is
not a repair; restore or import the intended files into the matching target.

Table and sequence read access is maintained with PostgreSQL default
privileges. PostgreSQL does not expose equivalent default privileges for large
objects, so onboarding grants all existing large objects explicitly and doctor
requires the idempotent init/provision command to be rerun after a new large
object is introduced.

`provision --plan` uses only the runtime connection and a read-only repeatable
read transaction. `provision --execute` takes a PostgreSQL advisory lock and is
idempotent; it never drops a role or database and never rotates an existing
password. The explicit `--local` mode is limited to loopback TCP/Unix sockets,
prints a permanent development warning and is never selected automatically.

### PostgreSQL role contract

- Dump login: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS`, `CONNECT`, schema `USAGE`, and read access to all dumped data.
- Target owner: `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS`.
- Creator login: `LOGIN CREATEDB NOINHERIT`, with direct membership in the
  target owner using `SET TRUE, ADMIN FALSE, INHERIT FALSE`.
- Restore login: `LOGIN NOCREATEDB NOINHERIT`, with the same direct owner
  membership.
- Runtime login: distinct from the three roles above, with direct membership in
  the target owner using `SET TRUE, ADMIN FALSE, INHERIT TRUE`. A legacy
  superuser runtime is accepted with the `legacy_runtime_superuser` security
  warning; PPBase does not normalize or rotate it.

No backup role may have `pg_read_server_files`, `pg_write_server_files` or
`pg_execute_server_program`. PPBase uses temporary `0600` passfiles and never
places PostgreSQL passwords in child-process arguments.

## A → B workflow

1. On A, create the backup in Settings → Backups.
2. Download the generated ZIP.
3. On B, upload the ZIP. Its signature is verified immediately, but an unknown
   signer remains quarantined.
4. Inspect and explicitly approve A's Ed25519 fingerprint/public key on B.
5. Select disaster recovery or clone.
6. PPBase creates a sealed plan, a generated PostgreSQL database and an anchored
   isolated `data_dir` below the durable target root. “Staging” is the logical
   validation state, not a disposable filesystem location.
7. It restores the dump and files, applies only missing deployed migrations,
   and validates the complete target.
8. Retype the ZIP filename to confirm activation.
9. PPBase publishes the activation journal and restarts on the new targets.
10. The Dashboard resumes polling with a short scoped token. Success is exposed
    only after startup, migration, database identity, filesystem and JWT checks.

The active database and active `data_dir` are not modified during staging.
Activation keeps both previous targets for rollback; it does not drop or erase
them after success. Restore never merges, upserts, or appends the archive into
the active database/files. It prepares and validates an entire new target, then
switches the database and `data_dir` together at activation.

When PPBase is deployed from a source clone, build the Admin UI once after
clone and rebuild it after frontend changes before restarting the service, so
Settings → Backups serves the current Dashboard:

```bash
cd admin-ui
npm ci
npm run build
```

Installed wheels use their packaged Admin UI assets.

## JWT modes

- `disaster_recovery`: the signed JWT secret is restored and compatible
  sessions remain valid.
- `clone`: a new JWT secret is generated and auth-record/superuser token keys
  and collection purpose-token secrets are rotated. Existing sessions and
  purpose tokens become invalid, while accounts and password hashes remain.

During activation the selected target always reads its private
`data_dir/.jwt_secret`, even if the launching deployment normally supplies
`PPBASE_JWT_SECRET`. Rollback starts as a fresh process and selects the previous
deployment secret or previous project-local secret.

## Restart and rollback contract

`python -m ppbase serve` records a reusable argument vector in
`PPBASE_RESTART_CMD`; the DSN is removed from that vector and supplied only in a
scoped `PPBASE_DATABASE_URL` environment override. Activation also retargets
`PPBASE_BACKUP_DUMP_DATABASE_URL`, so a successful restored server can create
its next backup immediately with the same dedicated dump role.

- Local foreground and PPBase daemon modes use `os.execvpe`, preserving the
  process identity expected by a supervisor.
- systemd should use `Restart=on-failure` or `Restart=always` and start PPBase
  through `python -m ppbase serve ...`.
- A Docker container must run PPBase as PID 1 (or through an init that forwards
  signals) and use an appropriate restart policy. Replacing only one process in
  a multi-worker deployment is unsupported.

The restored process must reach its durable health commit within
`PPBASE_BACKUP_ACTIVATION_HEALTH_TIMEOUT` (120 seconds by default). A daemon
watchdog marks rollback intent and restarts the previous target if startup
hangs. A thrown startup error follows the same rollback path immediately.

## Automatic backups

Settings → Backups accepts a standard five-field **UTC** cron expression and a
maximum number of automatic backups to retain. Empty cron disables automation;
`cronMaxKeep=0` disables pruning. Automatic runs reuse the same signed engine,
write barrier and cross-worker single-flight lock as manual runs.

Native backup archives are canonical local sets in v1. Backup-specific S3
archive storage is not implemented; PPBase does not present the business-file
S3 backend as if it were a safe backup repository. Native v1 creation therefore
requires the business-file backend to be local.

## Control-plane filesystem

Run PPBase as a dedicated non-root user. `PPBASE_BACKUP_CONTROL_DIR` must be a
private `0700` directory owned by that user. Identity, trust, operation locks,
plans and activation state are descriptor-anchored with `dir_fd` and
`O_NOFOLLOW`; detachment or substitution fails closed.

Keep these roots distinct and non-overlapping:

```text
PPBASE_BACKUP_ROOT          canonical immutable sets
PPBASE_BACKUP_CONTROL_DIR   identity, trust, plans and journals
PPBASE_BACKUP_STAGING_ROOT  scratch and legacy recovery only
PPBASE_BACKUP_TARGET_ROOT   durable isolated restore targets
PPBASE_DATA_DIR             current active target
```

An active `data_dir` may be below `PPBASE_BACKUP_TARGET_ROOT` only when the
activation journal proves the exact path and inode. Arbitrary children fail
closed. Abandonment and retention do not delete durable targets.

### Bare-metal and legacy-layout recovery

Precreate the four backup roots as the dedicated PPBase user with mode `0700`,
place canonical backups and targets on storage sized for the full PostgreSQL
dump plus business files, and run `ppbase backup doctor` from the same service
environment before starting PPBase.

Older activations may have an active target at
`PPBASE_BACKUP_STAGING_ROOT/<plan-id>/data`. On the next stopped-process startup,
PPBase verifies the journaled device/inode and performs an idempotent
descriptor-anchored rename to `PPBASE_BACKUP_TARGET_ROOT/<plan-id>/data`, fsyncs
both parents, then rewrites the activation restart paths. A crash after rename
is reconciled by inode on the next startup. This legacy promotion requires the
staging and target roots to be on the same filesystem; if they are not, stop
PPBase, place `PPBASE_BACKUP_TARGET_ROOT` temporarily on that filesystem, start
once to reconcile, and only then plan a separate operator-controlled move.
