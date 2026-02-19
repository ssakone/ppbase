# Storage & Assets

PPBase supports two file storage backends:

- `local` (default): files are stored under `data_dir/storage/{collectionId}/{recordId}/{filename}`
- `s3`: files are stored in an S3-compatible bucket (AWS S3, Cloudflare R2, MinIO, ...)

All record file fields continue to use the same API endpoints (`/api/files/...`) regardless of backend.

## Configure storage backend

### 1. From Admin UI settings

Go to `/_/` -> **Settings** -> **S3** and set:

- `endpoint`
- `bucket`
- `region`
- `accessKey`
- `secret`
- `forcePathStyle` (optional)

When bucket/key/secret are set, PPBase switches to S3 runtime storage.
Changes are applied at runtime (no restart required).

### 2. From environment variables

```bash
export PPBASE_STORAGE_BACKEND=s3
export PPBASE_S3_ENDPOINT=https://<accountid>.r2.cloudflarestorage.com
export PPBASE_S3_BUCKET=my-bucket
export PPBASE_S3_REGION=auto
export PPBASE_S3_ACCESS_KEY=<key>
export PPBASE_S3_SECRET_KEY=<secret>
export PPBASE_S3_FORCE_PATH_STYLE=false
```

### 3. From `pb.configure(...)`

```python
from ppbase import pb

pb.configure(
    storage_backend="s3",
    s3_endpoint="https://<accountid>.r2.cloudflarestorage.com",
    s3_bucket="my-bucket",
    s3_region="auto",
    s3_access_key="...",
    s3_secret_key="...",
    s3_force_path_style=False,
)
```

## Runtime precedence

Active backend is resolved in this order:

1. Runtime overrides from persisted admin settings (`/api/settings` -> `s3`).
2. Process settings (`pb.configure(...)`, constructor args, env vars).

If you configured S3/R2 in Admin UI, that runtime setting takes precedence over env defaults for file operations.

## Cloudflare R2 notes

- Endpoint is usually: `https://<accountid>.r2.cloudflarestorage.com`
- Region is commonly `auto`
- Keep `forcePathStyle=False` unless your gateway requires path-style

## File URLs and protected files

After upload, file fields store only the generated filename. Build file URLs using:

```text
/api/files/{collectionIdOrName}/{recordId}/{filename}
```

Protected file fields require a file token:

1. `POST /api/files/token` with auth
2. Append `?token=<fileToken>` to the file URL

## Public static assets (`publicDir`)

This is separate from record file storage.

- `--publicDir ./public` (or `pb.configure(public_dir="./public")`) serves files at `/`
- `/` returns `index.html` when present
- No directory listing is exposed
- Missing files return `404`

Useful for static sites or SPA build output.
