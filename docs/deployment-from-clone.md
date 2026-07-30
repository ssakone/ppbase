# Deployment from a source clone

This runbook covers a PPBase deployment that imports the framework directly
from a Git checkout, including first boot, upgrades and a native restore
rehearsal.

## 1. Clone and install

```bash
git clone <ppbase-repository> /srv/ppbase
cd /srv/ppbase

python3 -m venv .venv
.venv/bin/pip install -e .

cd admin-ui
npm ci
npm run build
cd ..
```

For a separate consumer application, create its virtual environment there and
install the PPBase checkout in editable mode:

```bash
cd /srv/myapp
python3 -m venv .venv
.venv/bin/pip install -e /srv/ppbase
.venv/bin/pip install -e .
```

Confirm which checkout the application loads:

```bash
.venv/bin/python -c "import ppbase; print(ppbase.__file__)"
```

## 2. Configure and start

```bash
export PPBASE_DATABASE_URL='postgresql+asyncpg://user:password@127.0.0.1/app'
export PPBASE_DATA_DIR='./pb_data'
export PPBASE_MIGRATIONS_DIR='./pb_migrations'
```

Apply application migrations and create the first superuser when needed:

```bash
.venv/bin/python -m ppbase migrate up \
  --db "$PPBASE_DATABASE_URL" \
  --dir "$PPBASE_MIGRATIONS_DIR"

.venv/bin/python -m ppbase create-admin \
  --db "$PPBASE_DATABASE_URL" \
  --email admin@example.com \
  --password '<strong-password>'
```

Start PPBase:

```bash
.venv/bin/python -m ppbase serve \
  --db "$PPBASE_DATABASE_URL" \
  --dir "$PPBASE_DATA_DIR" \
  --migrationsDir "$PPBASE_MIGRATIONS_DIR" \
  --host 127.0.0.1 \
  --port 8090
```

`serve` creates `pb_data/backups` automatically; ZIPs and their metadata
sidecars live below the data directory, matching PocketBase's local layout.
No separate backup-control directory is required.

## 3. Verify backup and restore readiness

With the server running:

```bash
.venv/bin/python -m ppbase backup doctor \
  --json \
  --db "$PPBASE_DATABASE_URL" \
  --dir "$PPBASE_DATA_DIR" \
  --server http://127.0.0.1:8090
```

Confirm `backupReady: true` and `restoreReady: true`.

## 4. Move a backup to a fresh target

1. Create and download a ZIP from the source Dashboard.
2. Start the fresh target and create a temporary target superuser if needed to
   access its Dashboard.
3. Upload the source ZIP on the target.
4. Confirm the destructive restore and wait for PPBase to restart.
5. Sign in with a superuser contained in the source archive.

Restore replaces the managed database, local file storage, `_superusers` and
the project-local JWT secret. The temporary target superuser therefore
disappears unless it also existed in the source archive. The selected ZIP
remains in the target's `pb_data/backups`. PPBase uses only temporary recovery
state directly below `pb_data` while the restore is in progress.

After restore, verify:

- `GET /api/health` succeeds;
- the expected source superuser can sign in;
- expected records and uploaded files are present;
- `backup doctor` is ready;
- a new backup can be created and downloaded from the restored target.

## 5. Upload a large ZIP without a public intermediary

Reverse proxies, tunnels and CDNs may limit or buffer large request bodies. A
temporary SSH local forward connects the local browser directly to PPBase:

```bash
ssh -N -L 18090:127.0.0.1:8090 deploy@server-address
```

Keep the command running, open `http://127.0.0.1:18090/_/`, then upload and
restore from the Dashboard.

## 6. Update the deployment

```bash
cd /srv/ppbase
git pull --ff-only
.venv/bin/pip install -e .

cd admin-ui
npm ci
npm run build
```

For a separate consumer application, apply its migrations from its own
checkout, restart the service, run `backup doctor`, and perform the normal
health checks.
