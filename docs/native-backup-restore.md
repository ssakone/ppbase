# Native backup and restore

PPBase follows PocketBase's backup surface while preserving its PostgreSQL
implementation. Backups are ordinary ZIP files stored under:

```text
<pb_data>/backups/
```

The Dashboard supports create, list, download, upload, destructive restore and
delete through the PocketBase-compatible backup API.

## Storage layout

Each backup is one real `.zip` file. With local storage, PPBase also writes the
PocketBase-style metadata sidecar next to it:

```text
pb_data/
  backups/
    ppbase_backup_myapp_20260729T120000Z_abcd1234.zip
    ppbase_backup_myapp_20260729T120000Z_abcd1234.zip.attrs
```

The `.attrs` file is not a second backup and is hidden from API listings. It
records the ZIP MIME type, original filename and MD5 blob attribute used by the
local filesystem driver. Download, restore and delete address the `.zip` key;
PPBase manages the sidecar automatically.

`PPBASE_DATA_DIR` determines the backup location, like PocketBase.

## ZIP format

PPBase creates ZIP archives with DEFLATE at a fast compression level. Every ZIP
contains `backup.json` plus the declared resources:

```text
backup.json
resources/database/schema.json
resources/database/data.copy
resources/files/...
resources/secrets/jwt_secret
```

`backup.json` records:

- the backup format and version;
- the backup ID and creation time;
- the PPBase version and application name;
- the PostgreSQL schema/runtime contract and source summary;
- applied migration filenames and SHA-256 hashes through `schema.json`;
- the complete resource inventory with byte sizes and SHA-256 checksums;
- the local-file reference inventory and JWT-secret mode.

`schema.json` describes the managed PostgreSQL `public` schema. `data.copy`
contains the native streamed `COPY` payload. PPBase uses asyncpg directly; it
does not call or bundle `pg_dump`, `pg_restore` or `psql`.

## Create

Backup creation:

1. acquires the backup write barrier;
2. reconciles managed tables, views and indexes with `_collections`;
3. records and hashes applied migrations;
4. exports `schema.json` and `data.copy` from one PostgreSQL snapshot;
5. copies referenced local files and the project-local JWT secret;
6. builds `backup.json` with resource sizes and SHA-256 checksums;
7. creates a compressed ZIP in a private workspace;
8. publishes the ZIP and `.attrs` atomically under `pb_data/backups`.

If PostgreSQL references a missing or unsafe file below
`<pb_data>/storage`, creation fails without publishing an incomplete ZIP.

## Upload

Upload follows PocketBase's lightweight ingestion behavior:

- the multipart field must be named `file`;
- the original filename must be a safe unique `.zip` basename;
- the payload must be detected as `application/zip` from its bytes;
- the configured upload-size limit is enforced;
- the ZIP and `.attrs` are stored immediately.

Upload does not extract the archive or validate every resource. Full validation
is deferred until restore. This keeps upload compatible and avoids doing the
same expensive work twice.

A PocketBase SQLite backup containing `data.db` is not a PPBase PostgreSQL
backup. It can be stored as a ZIP, but restore rejects it explicitly.

## Restore

Restore is destructive; it is not an import or merge.

Before changing the active target, PPBase extracts the selected ZIP into a
private workspace and verifies:

- the exact `backup.json` structure and supported format version;
- the exact ZIP member inventory;
- every declared resource size and SHA-256 checksum;
- ZIP path safety, duplicate members and decompression limits;
- the PostgreSQL schema contract;
- every archived applied-migration hash against the local migration files;
- the local-file reference inventory.

After preflight succeeds, PPBase blocks writes, stages local files, rebuilds the
managed PostgreSQL `public` schema in one transaction, restores `data.copy`,
records a restore marker and restarts. Startup recovery verifies the committed
database and files before normal hooks and newer migrations run.

Restore replaces:

- managed PostgreSQL state, including `_collections`, records, migration
  history and `_superusers`;
- `pb_data/storage`;
- the project-local `pb_data/.jwt_secret` resource.

Restore preserves the backup collection in `pb_data/backups`. A target-only
superuser disappears when `_superusers` is restored; sign in with a superuser
from the backup, or create one afterward if the restored database has none.

During a destructive restore, PPBase writes a temporary
`pb_data/.ppbase-restore.json` journal and stages replacement files under a
temporary `pb_data/.ppbase-restore/` directory. It locks the existing
`pb_data` and `pb_data/backups` directories directly, so no permanent control
directory or lock-file namespace is created. Recovery removes the temporary
journal and workspace after the database and files are finalized.

## Dashboard and API

The PocketBase-compatible endpoints are:

| Method | Endpoint | Result |
|---|---|---|
| `GET` | `/api/backups` | Array of `{ key, size, modified }`. |
| `POST` | `/api/backups` | Create a backup; `204 No Content`. |
| `POST` | `/api/backups/upload` | Store a ZIP; `204 No Content`. |
| `GET` | `/api/backups/{key}` | Download the ZIP. |
| `DELETE` | `/api/backups/{key}` | Delete ZIP and sidecar; `204 No Content`. |
| `POST` | `/api/backups/{key}/restore` | PocketBase-compatible asynchronous restore; `204`. |

PPBase additionally exposes
`POST /api/backups/{key}/restore-destructive` for the Dashboard. It returns
`202` after the destructive restore commits and restart is scheduled, allowing
the UI to surface actionable preflight errors before that point.

## Readiness

Run the doctor against the same database and data directory as `serve`:

```bash
python -m ppbase backup doctor \
  --json \
  --db "$PPBASE_DATABASE_URL" \
  --dir "$PPBASE_DATA_DIR" \
  --server http://127.0.0.1:8090
```

`backupReady` covers creation. `restoreReady` additionally requires a reusable
restart command and a successful database restore preflight.

## Large uploads

PPBase accepts uploads up to `PPBASE_BACKUP_MAX_UPLOAD_BYTES` (20 GiB by
default), but proxies and public tunnels may impose smaller limits or buffer the
entire request. For a direct encrypted path to a loopback-bound server, use an
SSH local forward:

```bash
ssh -N -L 18090:127.0.0.1:8090 deploy@server-address
```

Then open `http://127.0.0.1:18090/_/`. Closing the SSH command closes the
forward.
