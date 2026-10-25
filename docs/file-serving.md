# File downloads like PocketBase

`GET` and `HEAD /api/files/{collectionIdOrName}/{recordId}/{filename}` serve a
record file the way PocketBase does (`apis/file.go` `download`,
`tools/filesystem` `Serve`). The rules that PocketBase v0.22.34 and the
current release (v0.40.6) share always apply. Where the two versions
differ, PPBase follows **v0.40.6 by default** and **v0.22.34 in the
PocketBase 0.22 compatibility mode** (table below). Rules:
`ppbase/storage/serving.py` and `ppbase/storage/thumbs.py`; route:
`ppbase/api/files.py`.

## The PocketBase 0.22 mode

The mode is the switch of the PocketBase 0.22 error compatibility
(`app.state.error_compat == "0.22"`, set by `pb.set_error_compat("0.22")`
or `PPBASE_ERROR_COMPAT=0.22` where that mode is available):
`ppbase.storage.serving.pocketbase_compat` reads the same attribute, so one
switch gives a 0.22 client both the 0.22 errors and the 0.22 file
responses. Any other value is the default.

## What is served (both versions)

- **Content type**: the one recorded when the file was stored. A local
  PocketBase storage keeps it in a JSON side-car `<file>.attrs` (gocloud
  `fileblob`; v0.40.6 `tools/filesystem/internal/fileblob/attrs.go` l.28-87),
  which PocketBase backups contain. PPBase reads it for local files. A side-car
  that cannot be decoded is a `404`, as in PocketBase (how it is decoded
  depends on the version, below). A file without side-car (PPBase's own
  uploads) keeps PPBase's extension-based type.
- **`Content-Disposition`**: `inline` for PocketBase's safe types (images,
  audio, video, PDF), `attachment` otherwise, or when `?download` parses as a
  Go boolean true (`1`, `t`, `T`, `TRUE`, `true`, `True`; first value). Any
  other value (`yes`, a file name) is ignored (v0.22.34 `filesystem.go`
  l.367-409, v0.40.6 l.521-578). The disposition follows the stored type,
  before the extension overrides. The name is the served name: the stored
  name, or `<size>_<name>` for a thumbnail.
- Headers: `Content-Security-Policy: default-src 'none'; media-src 'self';
  style-src 'unsafe-inline'; sandbox`, `Cache-Control: max-age=2592000,
  stale-while-revalidate=86400` (v0.22.34 l.420-425, v0.40.6 l.589-594),
  `X-Content-Type-Options: nosniff`, `X-XSS-Protection: 1; mode=block`
  (global in PocketBase: v0.22.34 `apis/base.go` l.53, v0.40.6
  `apis/middlewares.go` l.293-294); no `X-Frame-Options` (v0.22.34
  `apis/file.go` l.206, v0.40.6 l.205). `Last-Modified`, `Content-Length`,
  `Accept-Ranges` and single byte ranges apply to originals and thumbnails.
- `HEAD` returns the same headers without a body (v0.22.34 `apis/file.go`
  l.40; v0.40.6 `GET` patterns also match `HEAD`).
- **Thumbnails** (v0.22.34 `apis/file.go` l.162-193, v0.40.6 l.164-196):
  `?thumb` (first value) must equal `100x100` (always allowed) or one of the
  field's `thumbs`, exactly (no trimming, no case folding), and the stored
  type must be an image (`image/png`, `image/jpg`, `image/jpeg`,
  `image/gif`, plus `image/webp` in v0.40.6). PPBase serves
  `thumbs_<file>/<size>_<file>` when it exists, PocketBase's own included,
  byte for byte. Otherwise it generates it with imaging's geometry (`WxH`
  center crop, `t`/`b` top/bottom crop, `f` fit without upscaling,
  `0xH`/`Wx0` ratio kept, rounded half up), JPEG EXIF orientation applied,
  first frame only, at most `CPU + 2` generations at a time with a 60 s
  wait, one generation at a time per thumbnail path (PocketBase's
  `thumbGenPending` singleflight, v0.22.34 l.252-275, v0.40.6 l.219-242),
  and publishes it with a `fileblob` side-car, so that PocketBase can serve
  it too. Any failure serves the original. A thumbnail is never followed
  through a symlink nor written outside the record directory; a side-car or
  `thumbs_` directory is never served as a file.
- Missing record, unreferenced name or absent stored file: `404`.
- Header values are sent like Go's header writer: CR and LF become spaces
  and the value is trimmed (`net/http` `Header.writeSubset`). Other control
  characters, which uvicorn refuses, also become spaces, and a lone
  surrogate becomes U+FFFD: only a crafted `.attrs` or a hook can hold any.
- A protected file needs a file token (the first `?token` value, untrimmed,
  as `c.QueryParam` and `URL.Query().Get` read it); see the open points.

## v0.22.34 and v0.40.6 differences

| Point | v0.22.34 (0.22 mode) | v0.40.6 (default) |
| --- | --- | --- |
| `Content-Disposition` name | as is: `inline; filename=photo_x.png` (`filesystem.go` l.418; also v0.23.0 to v0.39.x) | `strconv.Quote`: `inline; filename="photo_x.png"` (l.587, since v0.40.0, commit b259534d) |
| Extension overrides | `.svg`, `.css`, on the served name (l.380-383, l.412-415) | adds `.js`, `.mjs` (`text/javascript`), `.xlsx`, `.docx`, `.pptx` (Office types), on the served key (l.534-549, l.581-586) |
| `Cross-Origin-Opener-Policy` | not sent | `same-origin` (`apis/middlewares.go` l.296) |
| webp `?thumb` | original served (`apis/file.go` l.26) | PNG thumbnail recorded `image/png` (l.22, `filesystem.go` l.706-721) |
| Thumbnail format | from the thumbnail name, `imaging.FormatFromFilename` (jpg/jpeg, png, gif, tif/tiff, bmp, case-insensitive, else PNG; imaging v1.6.2 `io.go` l.117-156; `filesystem.go` l.511-516), recorded with the original's type (l.501-503) | from the original's type: `image/jpeg`, `image/gif` (`image/tiff`, `image/bmp`) keep theirs, anything else (`image/png`, `image/jpg`, webp) is PNG recorded `image/png` (l.700-721) |
| `.attrs` decoding | gocloud v0.40.0 `fileblob`, `json.Decoder` (v1): first value only, last duplicate member wins, names matched case-insensitively (Go `foldName`: `USER.CONTENT_TYPE`, `uſer...`), invalid UTF-8 and lone surrogate escapes become U+FFFD, `md5` from base64 ignoring CR/LF or from an array of bytes | `encoding/json/v2` `UnmarshalRead` (`internal/fileblob/attrs.go` l.4, l.80): the whole input is one value, duplicate names, invalid UTF-8 and invalid surrogate escapes are errors (`404`), names match exactly (others are ignored), `md5` from strict base64 |
| `.attrs` PPBase writes (thumbnails) | `json.Encoder.Encode`: `null` metadata, HTML characters escaped, final newline | `json.MarshalWrite` (l.53): `{}` metadata, no HTML escaping, no final newline |
| Protected file refused | `403 Insufficient permissions to access the file resource.` (`apis/file.go` l.136-138) | `404` (l.132-133) |
| Source size guard | none | `250 << 20` pixels, `PB_THUMBS_MAX_SOURCE_PIXELS` (`filesystem.go` l.649-663) |

In both versions a `null` value leaves a field empty, a `null` metadata
value is `""`, unknown members are ignored, and a wrong type, `NaN` or a
non-base64 `md5` is a `404`. The 0.22 column is checked against the
v0.22.34 binary by the paired probe, crafted side-cars included; the v0.40.6
column comes from the source and the json/v2 documentation (no v0.40.6
binary was run).

Not applied in either mode: the source size guard (PPBase relies on
Pillow's decompression-bomb guard, about 179 million pixels, so `?thumb` on
a larger image serves the original), the
`PB_THUMBS_MAX_WORKERS`/`PB_THUMBS_MAX_WAIT` variables (v0.40.6
`apis/file.go` l.27-41; the defaults apply), the superuser IP allowlist of
protected files (v0.40.6 `apis/file.go` l.118-125) and the protected-file
`403` of v0.22.34 (below).

## Known differences with PocketBase (both modes)

- A local file without `.attrs`: PocketBase serves `application/octet-stream`
  as an attachment and never makes a thumbnail; PPBase uses the extension.
  Only PPBase's own uploads (and thumbnails generated before this change) lack
  a side-car: PPBase uploads do not write one yet, and deleting a file or a
  record does not remove its `.attrs` (PocketBase's `fileblob` `Delete`
  removes it, v0.40.6 `internal/fileblob/fileblob.go` l.583, and a record
  delete removes the whole record prefix, v0.22.34 `core/base.go`
  l.1164-1172). A storage moved back to PocketBase therefore serves files
  uploaded through PPBase as `application/octet-stream` attachments without
  thumbnails, and keeps orphan side-cars of deleted ones. Both are for the
  upload and delete paths (`ppbase/services/file_storage.py`), not done here.
- S3 storage: the type comes from the extension, not the object metadata, and
  no thumbnail is generated (the original is served).
- Conditional requests (`If-Modified-Since` -> `304`, `If-Match`,
  `If-Unmodified-Since` -> `412`, `If-Range`) and multi-range requests are
  not handled like Go's `http.ServeContent` (v0.22.34 `filesystem.go` l.427,
  v0.40.6 l.596); a `416` keeps PPBase's empty body. PPBase also sends an
  `ETag`. A revalidating client gets `200` and the whole file instead of
  `304`.
- Thumbnail bytes differ (Pillow and Go's imaging encode differently); the
  format, dimensions and anchoring match.
- Singleflight: a request that waited for the same thumbnail serves the one
  just published; if that generation failed it tries again instead of
  sharing the failure (both end with a thumbnail or the original).
- `.attrs` decoding: nesting deeper than Python's recursion limit is refused
  (Go allows 10000 levels); only crafted side-cars nest at all.
- A protected file whose `viewRule` lets guests in is still refused without a
  valid file token: both versions evaluate the rule as a guest (v0.22.34
  `apis/file.go` l.118-138, v0.40.6 l.109-134), PPBase refuses before
  reading the rule.
- Download hooks: `event.served_name` is the thumbnail name for a thumbnail and
  sets the `Content-Disposition` name (and, in the 0.22 mode, the extension
  of the overrides); `event.force_download` starts from the `?download`
  boolean.

## Protected files under the 0.22 mode (not implemented)

v0.22.34 refuses a protected file with `403 Insufficient permissions to
access the file resource.` (`apis/file.go` l.136-138); v0.40.6 with a `404`
(l.132-133), which PPBase keeps in both modes. The minimal change, once the
`0.22` error mode (`ppbase.middleware.error_compat`) is merged, is in
`_serve_file`: route every protected-file refusal through one helper that
returns, when `pocketbase_compat(request.app) == "0.22"`, a `403` with that
message and `data: {}` (tagged with a dedicated error kind, so that the
middleware keeps the message), and `_not_found_error()` otherwise. For the
full behavior of both versions, a missing or invalid token must first
evaluate the `viewRule` as a guest (auth context `None`, request auth `{}`,
as `records.py` builds it) before refusing: a public rule then serves the
file, a `null` rule refuses it.

## Paired probe

`tests/pocketbase_parity/files_probe.py` uploads synthetic files to a
disposable PocketBase 0.22 binary, replaces some side-cars by crafted ones,
copies its storage under PPBase, recreates the records and compares 95
requests (originals, download values, HEAD, ranges, allowed, refused and
generated thumbnails, an absent stored file, crafted side-cars, protected
files, 404s). On v0.22.34 the 0.22 mode reports only the `.attrs`-less
and protected-file differences (9); the default mode adds the documented
v0.40.6 differences (171 in all, 0 unexpected):

```bash
PYTHONPATH=. python tests/pocketbase_parity/files_probe.py \
    --pocketbase /path/to/pocketbase-0.22.34 \
    --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/empty_db \
    --out /tmp/files-probe [--compat default]
```
