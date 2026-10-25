# File fields: the names a write may keep

The files API serves any name that a record references. A record write may
therefore only **keep, remove, reorder or de-duplicate** the names the record
already has; a new file must be **uploaded**. Any other name is refused: an
unknown name, a `.attrs` side-car, the file of another record, a name with a
path separator, or a name the record has just removed. Otherwise a client
allowed by the `updateRule` could reference, then download, an object of the
record directory that it never uploaded. Both PocketBase versions refuse
these names (0.22.34 `forms/record_upsert.go` l.428-439, v0.40.6
`core/field_file.go` l.250-269).

The check covers every way a client can write a file field:

- record create and update: JSON body, multipart text values and
  `@jsonPayload`;
- batch requests;
- names written into `e.data` by an `on_record_create_request` or
  `on_record_update_request` hook (checked after the hooks);
- the `createData` of `auth-with-oauth2`;
- `pb.records()` and `e.records()` writes, in the default mode.

The implementation is `ppbase/services/record_file_fields.py`.

## Default mode (current PocketBase, v0.40.6)

- A refused name answers 400 with the other field errors:
  `{"code": "validation_invalid_file", "message": "Invalid new files: [x].",
  "params": {"invalidFiles": ["x"]}}`. Each name is cut to 250 bytes.
- Too many files: `validation_too_many_files`.
- Multipart: files uploaded under `f` replace the value and keep the names
  sent under `f` with them; files under `f+` are appended and files under `+f`
  are prepended. A single file field keeps the last upload. The `f+`, `+f`
  and `f-` body modifiers apply to the stored value.

## 0.22 mode (PocketBase 0.22.34)

With `pb.set_error_compat("0.22")` (see `error-compat.md`):

- A refused name answers 400 `Failed to load the submitted data due to
  invalid formatting.`. `data` holds only the first refused field in schema
  order: `{"code": "validation_unknown_filenames", "message": "The field
  contains unknown filenames."}`. This check runs after the 403 and 404
  answers, and before the hooks and the relation checks.
- `f+` has no effect on a file field, and `+f` is ignored. The legacy
  `f.<name>` and `f.<index>` keys set to an empty value delete a name.
- Files uploaded under `f` are appended to a multiple field. A single field
  keeps the first upload. Files under `f+` are ignored.
- Too many files: `validation_too_many_values` `Select no more than N.`.
- A single multipart text value of a file field is one raw name, never
  JSON-decoded: `docs='["a","b"]'` is refused. Other keys keep their string,
  and the `docs-` modifier decodes it.

## OAuth2 `createData`

`createData` comes from the client, so its file names are checked like a
record create (a new record has no file):

- Default mode: refused. v0.40.6 creates the record by an internal
  `POST /records` request (`apis/record_auth_with_oauth2.go` l.319,
  l.403-425). PPBase answers with its own OAuth2 error envelope (400
  `Failed to authenticate with OAuth2: ...`, whose message carries the
  `validation_invalid_file` error), like any other create error of this
  route.
- 0.22 mode: like 0.22.34, which ignores the error of `LoadData`
  (`forms/record_oauth2_login.go` l.227). The refused file field and the
  schema fields after it are not loaded. The fields before it and the auth
  fields (`id`, `email`, `password`...) are loaded. The record is created
  and the login succeeds.

PPBase's OAuth2 field mapping stores the provider avatar URL in a file field.
This value comes from the server and is not checked. If `createData`
addresses that field, the mapping is not applied to it.

## Server-side writes

`pb.records()` and `e.records()` follow the app's mode:

- Default mode: checked like v0.40.6 `app.Save`, which validates
  (`core/db.go` l.178-187, `core/record_model.go` l.1427-1433). A refused name
  raises the record validation error.
- 0.22 mode: not checked, like 0.22.34 `dao.SaveRecord` (`daos/record.go`
  l.586-610).

`RecordRepository(name, file_semantics=None)` opts out of the check, like
`SaveNoValidate`. The service functions `create_record` and `update_record`
check names only when they receive `file_semantics`. The record API passes
it; other internal callers do not. Never pass a client body to a write that
is not checked.

Server-side writes normalize file values like a request: duplicates are
dropped, a JSON array string is decoded, and a single field keeps the last
name. Names sent under `f` are kept together with the files uploaded under
`f`; before this change the uploads replaced them.

## Dashboard

A record duplicated in the dashboard no longer copies its file fields, like
PocketBase's dashboard (v0.40.6 `ui/src/fields/file/onrecordduplicate.js`).

## Known differences

- 0.22 mode: a name written by a request hook into `e.data` is refused with
  the 0.22 load error. In 0.22.34 the hooks receive an already validated
  `e.Record`, and `dao.SaveRecord` saves whatever they set on it
  (`forms/record_upsert.go` l.744-749, l.809).
- PocketBase normalizes every file field on each save. An update in PPBase
  rewrites only the file fields the request addresses.
- Names are checked against the value read at the start of the update,
  without a row lock. Two concurrent updates can only bring back a name the
  same record held.
- Default mode: PPBase's validation messages list the fields
  (`Failed to update record. Validation failed for: docs.`), where v0.40.6
  says only `Failed to update record.`.
- Upload size and MIME type refusals keep PPBase's own codes and messages.
- 0.22 mode: a repeated multipart text key that is not a field name keeps
  every value. 0.22.34 keeps only the first.
