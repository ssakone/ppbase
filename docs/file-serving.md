# File downloads like PocketBase

`GET` and `HEAD /api/files/{collectionIdOrName}/{recordId}/{filename}` serve a
record file the way PocketBase does (`apis/file.go` `download`,
`tools/filesystem` `Serve`). PPBase applies the rules that PocketBase v0.22.34
and the current release (v0.40.6) share; the points where the two versions
differ are listed below and are not applied. Rules: `ppbase/storage/serving.py`
and `ppbase/storage/thumbs.py`; route: `ppbase/api/files.py`.

## What is served

- **Content type**: the one recorded when the file was stored. A local
  PocketBase storage keeps it in a JSON side-car `<file>.attrs` (gocloud
  `fileblob`; v0.40.6 `tools/filesystem/internal/fileblob/attrs.go` l.28-87),
  which PocketBase backups contain. PPBase reads it for local files. A side-car
  that cannot be decoded is a `404`, as in PocketBase. A file without side-car
  (PPBase's own uploads) keeps PPBase's extension-based type.
- **`Content-Disposition`**: `inline; filename=<name>` for PocketBase's safe
  types (images, audio, video, PDF), `attachment; filename=<name>` otherwise,
  or when `?download` parses as a Go boolean true (`1`, `t`, `T`, `TRUE`,
  `true`, `True`). Any other value (`yes`, a file name) is ignored
  (v0.22.34 `filesystem.go` l.367-418). The name is the served name: the
  stored name, or `<size>_<name>` for a thumbnail.
- `.svg` and `.css` are served as `image/svg+xml` and `text/css` (l.380-383,
  l.414-416).
- Headers: `Content-Security-Policy: default-src 'none'; media-src 'self';
  style-src 'unsafe-inline'; sandbox`, `Cache-Control: max-age=2592000,
  stale-while-revalidate=86400` (l.420-425), `X-Content-Type-Options:
  nosniff`, `X-XSS-Protection: 1; mode=block` (global in PocketBase,
  `apis/base.go` l.53); no `X-Frame-Options` (`apis/file.go` l.206).
  `Last-Modified`, `Content-Length`, `Accept-Ranges` and single byte ranges
  apply to originals and thumbnails alike.
- `HEAD` returns the same headers without a body (`apis/file.go` l.40).
- **Thumbnails** (`apis/file.go` l.162-193): `?thumb` must equal `100x100`
  (always allowed) or one of the field's `thumbs`, exactly (no trimming, no
  case folding), and the stored type must be an image (`image/png`,
  `image/jpg`, `image/jpeg`, `image/gif`; see webp below). PPBase serves
  `thumbs_<file>/<size>_<file>` when it exists, PocketBase's own included,
  byte for byte. Otherwise it generates it with imaging's geometry (`WxH`
  center crop, `t`/`b` top/bottom crop, `f` fit without upscaling, `0xH`/`Wx0`
  ratio kept, rounded half up), JPEG EXIF orientation applied, first frame
  only, at most `CPU + 2` generations at a time with a 60 s wait, and
  publishes it with a `fileblob` side-car (the original's type), so that
  PocketBase could serve it too. Any failure serves the original. A thumbnail
  is never followed through a symlink nor written outside the record
  directory; a side-car or `thumbs_` directory is never served as a file.
- Missing record, unreferenced name or absent stored file: `404`.

## v0.22.34 and v0.40.6 differences (not applied)

| Point | v0.22.34 | v0.40.6 | PPBase |
| --- | --- | --- | --- |
| webp thumbnail | original served (`apis/file.go` l.26) | PNG thumbnail (l.22, `filesystem.go` l.706-721) | webp thumbnail (unchanged) |
| Thumbnail format | from the thumbnail name (`filesystem.go` l.511-516) | from the original type (l.706-721) | from the decoded image: same result for png/jpeg/gif named after their type |
| Non-token served name | `filename=<raw>` (l.418) | `filename="<strconv.Quote>"` (l.587) | v0.22.34 bytes for token names (every generated name), Go-quoted otherwise |
| Extension overrides | `.svg`, `.css` | adds `.js`, `.mjs`, `.xlsx`, `.docx`, `.pptx` (l.534-549) | `.svg`, `.css` |
| Protected file refused | `403 Insufficient permissions to access the file resource.` (l.136-138) | `404` (l.132-133) | `404` |
| Source size guard | none | 250 Mpx, `PB_THUMBS_MAX_SOURCE_PIXELS` (l.649-663) | Pillow's decompression-bomb guard |
| `Cross-Origin-Opener-Policy` | not sent | `same-origin` (`apis/middlewares.go` l.296) | not sent |

## Known differences with PocketBase

- A local file without `.attrs`: PocketBase serves `application/octet-stream`
  as an attachment and never makes a thumbnail; PPBase uses the extension.
  Only PPBase's own uploads (and thumbnails generated before this change) lack
  a side-car; PPBase uploads do not write one yet.
- S3 storage: the type comes from the extension, not the object metadata, and
  no thumbnail is generated (the original is served).
- Conditional requests (`If-Modified-Since` -> `304`, `If-Match`,
  `If-Unmodified-Since` -> `412`) and multi-range requests are not handled
  like Go's `http.ServeContent`; a `416` keeps PPBase's empty body. PPBase
  also sends an `ETag`.
- Thumbnail bytes differ (Pillow and Go's imaging encode differently); the
  format, dimensions and anchoring match.
- A protected file whose `viewRule` lets guests in is still refused without a
  file token (PocketBase evaluates the rule as a guest in both versions).
- Download hooks: `event.served_name` is the thumbnail name for a thumbnail and
  sets the `Content-Disposition` name; `event.force_download` starts from the
  `?download` boolean.

## Paired probe

`tests/pocketbase_parity/files_probe.py` uploads synthetic files to a
disposable PocketBase 0.22 binary, copies its storage under PPBase, recreates
the records and compares 73 requests (originals, download values, HEAD,
ranges, allowed/refused/generated thumbnails, an absent stored file, 404s).
On v0.22.34 it reports only the documented `.attrs`-less and webp
differences:

```bash
PYTHONPATH=. python tests/pocketbase_parity/files_probe.py \
    --pocketbase /path/to/pocketbase-0.22.34 \
    --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/empty_db \
    --out /tmp/files-probe
```
