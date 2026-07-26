# Native backup and restore

PPBase exposes a signed PostgreSQL backup workflow in **Settings → Backups**.
The browser experience follows PocketBase (create, ZIP download, ZIP upload,
restore and delete). Restore is a **destructive in-place** operation: after the
archive is fully verified, PPBase replaces the active database and local file
storage with the backup's contents and restarts.

Backup and restore run against `PPBASE_DATABASE_URL` with the same role that
runs `serve`; `ppbase init postgres` is not required. The backup engine is
native — it streams the live schema and data over the PostgreSQL wire protocol
with `COPY` using the same asyncpg driver as the server. No `pg_dump`,
`pg_restore` or `psql` binary is used or bundled at any point. Backup needs read
access to `public`. Destructive restore additionally requires a superuser
runtime or ownership of the active database and `public`, plus a reusable
restart command recorded by `serve`. `backup doctor` reports these two readiness
states separately. The onboarding command is optional (see
[Optional hardening](#optional-hardening)).

## What the Dashboard can do

- Create a signed local backup.
- Download a standard `.zip` file.
- Upload and inspect a PPBase ZIP from another server.
- Quarantine an unknown Ed25519 signer and approve its exact public key.
- Restore a verified backup destructively into the active target and restart.
- Delete backups that are not currently in use.
- Configure a five-field UTC cron and automatic-backup retention.

## Canonical storage and transport

The canonical server-side representation is an immutable backup set below
`PPBASE_BACKUP_ROOT`. The browser receives a standard ZIP generated from that
set. A ZIP contains at least:

```text
manifest.json
manifest.sig
signer.pub
resources/database/schema.json
resources/database/data.copy
resources/files/...
resources/secrets/jwt_secret
```

The database is captured as a native pair: `schema.json` records the managed
`public` schema contract (`format_version = 2`, `contract_version = 1`,
`system_schema_version = 1`, plus applied migration filenames and SHA-256
hashes) and `data.copy` holds the streamed `COPY` payload. This is the only
supported archive layout; restore accepts exactly this format version and
rejects any other before touching the active target.

The Ed25519 private key, trust store and PostgreSQL credentials are never
exported. ZIP upload is streamed and bounded; ZIP Slip, duplicate members,
special files, symlinks, compression bombs, unknown manifest versions, invalid
signatures and checksum mismatches are rejected before publication.

PocketBase SQLite archives containing `data.db` are not native PPBase backups.
Because the backup engine speaks the PostgreSQL wire protocol directly with the
same asyncpg driver as the server, no external PostgreSQL client binaries are
required. Docker, root privileges and first-run downloads are not runtime or
backup/restore dependencies.

## Creating a backup

Backup connects with the runtime role (`PPBASE_DATABASE_URL`). Both superuser and
non-superuser roles are accepted; an elevated runtime role is reported with the
non-blocking `runtime_superuser` warning and is never rejected or
altered.

Default directories (`./pb_backups`, `./pb_backup_control`, and the temporary
work area) are created automatically when missing. Creation checks that enough
disk space is available for the native `COPY` export plus business files.

Every local file referenced by PostgreSQL must exist below the active
`<data_dir>/storage`. If a referenced file is missing or unsafe, creation stops
with `backup_integrity_failed` (reporting the offending path), removes the
partial set, and publishes no backup instead of an incomplete archive.

PPBase application objects are confined to PostgreSQL's `public` schema. Native
backups capture only `public`, and restore rejects an archive whose schema
contract targets another application schema, or contains any unmanaged object,
before any mutation. Schemas outside `public` are neither backed up nor
replaced; do not store PPBase application state in them.

## Restoring a backup

Restore is destructive and applies to the active database and local file
storage. The Dashboard flow is:

1. Select a backup (or upload one) and open the restore dialog.
2. If the archive was signed by an unknown external Ed25519 identity, approve
   that exact key. Restore stays blocked until the signer is trusted and the
   integrity check passes.
3. Retype the exact ZIP filename to confirm.
4. Restore and restart.

What the server does:

1. **Verify everything first.** The archive's structure, checksums, signature,
   signer trust and schema contract (format/contract/system-schema version plus
   migration hashes) are validated before any mutation. An invalid or
   version-mismatched archive is rejected and the active database is left
   untouched.
2. **Enter read-only maintenance.** A write barrier blocks application writes
   for the duration of the restore.
3. **Prepare restored files** in a temporary directory alongside the active
   `data_dir`.
4. **Swap local storage** and the project-local `.jwt_secret` into place while
   retaining the previous files for immediate pre-commit recovery.
5. **Replace the database atomically.** Public application objects are dropped
   and rebuilt from `resources/database/schema.json`, then repopulated by
   streaming `resources/database/data.copy` back in with `COPY`, in one
   transaction that writes the restore commit marker.
6. **Schedule the mandatory restart** through the memorized `serve` command
   while the write fence and old files remain retained.
7. **Recover on startup before hooks.** PPBase matches the database marker to
   the signed file-reference inventory, finalizes the file swap, then performs
   normal bootstrap and applies any newer migrations.

Before the PostgreSQL commit, an error rolls back the database and puts the
previous files back. After commit, an unexpected validation or restart failure
stays fail-closed in maintenance mode; startup recovery must verify the matching
signed inventory before it can discard the previous files. There is no durable
user-selectable rollback after successful recovery — the restored target is the
live target. Sessions signed with the backup's JWT secret remain valid;
sessions from the replaced instance do not.

## SDK-compatible endpoints

The HTTP surface mirrors PocketBase so existing tooling keeps working:

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/backups` | Create a signed backup. |
| `POST` | `/api/backups/upload` | Upload and inspect a backup ZIP. |
| `GET` | `/api/backups` | List backups. |
| `GET` | `/api/backups/{id}` | Inspect (or download with a file token). |
| `DELETE` | `/api/backups/{id}` | Delete a backup not in use. |
| `POST` | `/api/backups/{id}/restore` | SDK-compatible restore. |
| `POST` | `/api/backups/{id}/restore-destructive` | Destructive in-place restore (returns `202`). |

## Control-plane filesystem

`PPBASE_BACKUP_ROOT` (default `./pb_backups`) and
`PPBASE_BACKUP_CONTROL_DIR` (default `./pb_backup_control`) are created on
`serve` with the same directory policy as `PPBASE_DATA_DIR`: ownership and
permissions follow the service user and its `umask`. Internal backup sets,
identity material, trust state, operation locks and the in-flight restore
journal keep their own restricted permissions; the Ed25519 private key remains
mode `0600`.

Keep these roots distinct and non-overlapping:

```text
PPBASE_BACKUP_ROOT          canonical immutable backup sets
PPBASE_BACKUP_CONTROL_DIR   identity, trust, locks and the restore journal
PPBASE_DATA_DIR             the active target (database files + storage)
```

## Restart contract

`python -m ppbase serve` records a reusable argument vector in
`PPBASE_RESTART_CMD`; the DSN is removed from that vector and supplied only in a
scoped `PPBASE_DATABASE_URL` environment override. After a successful restore,
PPBase restarts through that command.

- Local foreground and PPBase daemon modes use `os.execvpe`, preserving the
  process identity expected by a supervisor.
- systemd should use `Restart=on-failure` or `Restart=always` and start PPBase
  through `python -m ppbase serve ...`.
- A Docker container must run PPBase as PID 1 (or through an init that forwards
  signals) and use an appropriate restart policy. Replacing only one process in
  a multi-worker deployment is unsupported.

Automatic restart is a restore prerequisite. If no reusable command is
configured, the Dashboard blocks restore and the server rejects it before any
mutation. `python -m ppbase serve` configures the command automatically. If
restart scheduling or `exec` fails after the database commit, PPBase remains
read-only until the process is restarted and startup recovery succeeds.

## Automatic backups

Settings → Backups accepts a standard five-field **UTC** cron expression and a
maximum number of automatic backups to retain. Empty cron disables automation;
`cronMaxKeep=0` disables pruning. Automatic runs reuse the same signed engine,
write barrier and cross-worker single-flight lock as manual runs.

Native backup archives are canonical local sets. Backup-specific S3 archive
storage is not implemented; native creation requires the business-file backend
to be local. When the configured storage backend is not local, the Dashboard
blocks native create/restore and explains that external object storage is not
captured.

## Optional hardening

Backup and restore use `PPBASE_DATABASE_URL`; the command below is optional.
`init postgres` creates a fresh database/runtime role and safe default
filesystem roots. Its execute mode requires an ephemeral PostgreSQL superuser
DSN; it is never persisted.

For a completely new project, initialize the database, runtime role and safe
default filesystem roots once from the host:

```bash
PPBASE_POSTGRES_BOOTSTRAP_DATABASE_URL='postgresql+asyncpg://...' \
  ppbase init postgres --plan --name myapp --output-env ./ppbase.env
PPBASE_POSTGRES_BOOTSTRAP_DATABASE_URL='postgresql+asyncpg://...' \
  ppbase init postgres --execute --name myapp --output-env ./ppbase.env
```

The mode-`0600` init output contains only `PPBASE_DATABASE_URL`. Because the
native engine backs up and restores with the runtime role over the wire, no
separate dump role or credential file is needed.

After restart, inspect the two independent readiness results from an environment
that also has the runtime database variables. The command exits nonzero only
when backup creation is blocked; `restoreReady` and the human summary identify
restore-only ownership or restart blockers:

```bash
ppbase backup doctor \
  --db "$PPBASE_DATABASE_URL" \
  --dir "$PPBASE_DATA_DIR" \
  --server http://127.0.0.1:8090
```

When PPBase is deployed from a source clone, build the Admin UI once after clone
and rebuild it after frontend changes before restarting the service:

```bash
cd admin-ui
npm ci
npm run build
```

Installed packages use their packaged Admin UI assets.
