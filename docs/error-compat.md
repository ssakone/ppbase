# PocketBase 0.22 error compatibility (opt-in)

PPBase answers errors with the envelope of current PocketBase versions,

```json
{"status": 403, "message": "Only superusers can perform this action.", "data": {}}
```

(v0.40.6 `tools/router/error.go` l.36-42). PocketBase 0.22.34 answers

```json
{"code": 403, "message": "Only admins can perform this action.", "data": {}}
```

(`apis/api_error.go` l.11-15, `apis/record_crud.go` l.57). Clients written
against 0.22 (the 0.22 JS/Dart SDKs read `code`) can select a compatibility
mode. It is **disabled by default**: without it, PPBase's responses are
unchanged byte for byte, and every PPBase error body is exactly
`{status, message, data}`. This middleware is the only place that
post-processes error bodies (an interim additive `code` next to `status` was
removed, see `docs/error-envelope.md`).

## Enabling it

From the host application, once, at startup — typically in the hooks
registration function passed to `ppbase serve --hooks module:register`:

```python
def register(pb):
    pb.set_error_compat("0.22")   # "" or None restores PPBase's envelope
    ...
```

or with the equivalent setting / environment variable:

```bash
PPBASE_ERROR_COMPAT=0.22 python -m ppbase serve ...
```

`pb.error_compat` returns the active mode; `Settings(error_compat="0.22")`
and `ppbase.middleware.error_compat.set_error_compat(app, "0.22")` do the
same for an app built with `create_app`. Accepted values: `""`/`off`/`none`
(PPBase) and `0.22` (aliases `v0.22`, `pocketbase-0.22`); anything else
raises `ValueError`.

## What it does

One outermost ASGI middleware (`ppbase/middleware/error_compat.py`) looks at
every response with status >= 400 and `Content-Type: application/json`:

- a PPBase error body — exactly the keys `status`, `message` and optionally
  `data`, or Starlette's bare `{"detail": "<text>"}` of an unknown route or
  method — becomes `{"code": <HTTP status>, "message": ..., "data": {...}}`;
- the message is translated when PocketBase 0.22.34 words it differently
  (table below), then normalized like `NewApiError`
  (`strings.TrimSpace(inflector.Sentenize(message))`, `apis/api_error.go`
  l.67-75: capitalized, final period); an empty message takes the default of
  its status (l.31-65); `data` is always an object (`safeErrorsData`,
  l.77-90);
- the rewritten body has the bytes of 0.22.34 (`encode_like_pocketbase_022`;
  `apis/base.go` l.104 writes it with echo's `json.Encoder`): fields in
  struct order, `data` keys sorted (a Go map), `<`, `>`, `&`, U+2028 and
  U+2029 escaped as `\u003c`..., UTF-8 text and a final newline — measured on
  the binary (`{"code":404,"message":"The requested resource wasn't
  found.","data":{}}` plus `\n`, 72 bytes); the `Content-Type` stays
  `application/json` (0.22.34 adds `; charset=UTF-8` to every JSON answer,
  successes included, which this mode does not touch);
- every other body is left untouched: a host route that already answers
  `{"code", ...}` (BUMA's legacy routes), a custom `{"message": ...}`,
  successes, non-JSON and streamed responses.

Status codes are never changed.

### Errors tagged by their handler

The body alone does not always identify the 0.22.34 error: a record create
validation error has the same PPBase body for an admin (0.22.34 returns the
form error unwrapped) and for a user (wrapped by the `createRule`
`DrySubmit`), and two route guards share one PPBase message. The handler
that knows the case then tags its response with a private
`x-ppbase-error-kind` header (`ppbase.middleware.error_compat.mark_error`,
or `error_kind_headers` for an `HTTPException`; the route guards pass
`kind=` to `event._http_error`). The tag is set only while the mode is on
and the middleware always removes it, so it never reaches a client and the
default mode is unchanged. A tagged error skips the message table:

| Tag | PocketBase 0.22.34 error | Source (v0.22.34) |
| --- | --- | --- |
| `form` | default message of the status ("Something went wrong while processing your request."), the form errors as `data` (pydantic's "Field required" in ozzo's words, "Cannot be blank.") | `apis/base.go` l.75-80; record update `apis/record_crud.go` l.372, admin create l.280, admin login `apis/admin.go` l.92 |
| `body_load` | the `c.Bind` failure of the route, `data` `{}`: "Failed to load the submitted data due to invalid formatting." for admin create and update, "An error occurred while loading the submitted data." otherwise | `apis/admin.go` l.247-249, l.293-295, l.83-85; `apis/backup.go` l.76-77 |
| `query` | default message, `data` `{}` | `apis/record_crud.go` l.83-86 (search provider parse error: filter, sort, page, perPage, skipTotal) |
| `create_rule_failed` | "Failed to create record.", `data` `{}` | `apis/record_crud.go` l.250-261 |
| `guest_only` | The request can be accessed only by guests. | `apis/middlewares.go` l.37 |
| `record_auth_required` | The request requires valid record authorization token to be set. | l.77, l.99 |
| `record_collection_forbidden` | The authorized record model is not allowed to perform this action. | l.82, l.169 |
| `admin_auth_required` | The request requires valid admin authorization token to be set. | l.119, l.147 |
| `admin_or_record_auth_required` | The request requires admin or record authorization token to be set. | l.165, l.193 |
| `owner_forbidden` | You are not allowed to perform this request. | l.204 |

Tagged by PPBase: record create validation errors of an admin (relation
targets and fields), every record update validation error (and the
`oldPassword` check), a `createRule` mismatch, the list filter/sort parse
errors, pydantic request validation errors (`body_load` when the body is not
JSON or not an object, `query` when only query or path parameters fail, else
`form`), and the guards:

| PPBase guard | PocketBase 0.22.34 counterpart | Tags |
| --- | --- | --- |
| `pb.apis.require_guest_only()` | `RequireGuestOnly()` | `guest_only` |
| `pb.apis.require_auth()` | `RequireAdminOrRecordAuth()` | `admin_or_record_auth_required` |
| `pb.apis.require_auth("c", ...)` | `RequireRecordAuth("c", ...)` | `record_auth_required`, `record_collection_forbidden` |
| `pb.apis.require_auth("c", "_superusers")` | `RequireAdminOrRecordAuth("c")` | `admin_or_record_auth_required`, `record_collection_forbidden` |
| `pb.apis.require_superuser_auth()` | `RequireAdminAuth()` | `admin_auth_required` |
| `pb.apis.require_superuser_or_owner_auth(p)` | `RequireAdminOrOwnerAuth(p)` | `admin_or_record_auth_required`, `owner_forbidden` |
| `pb.require_auth()` (FastAPI dependency, also `POST /api/files/token`) | `RequireAdminOrRecordAuth()` | `admin_or_record_auth_required` |
| `pb.require_record_auth()` (FastAPI dependency) | `RequireRecordAuth()` | `record_auth_required` (no auth, admin token) |
| `event.require_auth()` / `require_auth_record()` / `require_superuser()` / `require_same_auth_record()` | the same middlewares | `admin_or_record_auth_required` / `record_auth_required` / `admin_auth_required` / `owner_forbidden` |

`event.require_superuser()` answers 401 without auth (it answered 403), like
`pb.apis.require_superuser_auth()`, 0.22.34 `RequireAdminAuth()` (l.114-124)
and v0.40.6 `RequireSuperuserAuth()` (`apis/middlewares.go` l.91-95, l.108).

A host route tags its own errors the same way when it reproduces a
PocketBase 0.22.34 error the body cannot identify:
`return mark_error(request, response, "form")`.

## Messages translated

| Status | PPBase message | PocketBase 0.22.34 | Routes | Source (v0.22.34) |
| --- | --- | --- | --- | --- |
| 403 | Only superusers can perform this action. | Only admins can perform this action. | all | `apis/record_crud.go` l.57, l.155, l.209, l.325, l.417 |
| 401 | The request requires admin authorization token to be set. | The request requires valid admin authorization token to be set. | all | `apis/middlewares.go` l.119, l.147 |
| 404 | Missing collection context. | The requested resource wasn't found. | `/api/collections/{c}/…` | `apis/middlewares.go` l.263-266 (`record_crud.go` l.45 passes the text as data) |
| 401 | The request requires valid auth token to be set. | The request requires valid record authorization token to be set. | auth-refresh | `apis/middlewares.go` l.94-100, `apis/record_auth.go` l.46 |
| 400 | Invalid JSON body. / Request body must be a JSON object. | Failed to load the submitted data due to invalid formatting. | record create/update | `apis/record_crud.go` l.227, l.270, l.362 |
| 400 | Invalid JSON body. / Request body must be a JSON object. | An error occurred while loading the submitted data. | auth-with-password, auth-with-oauth2, request/confirm-password-reset, request/confirm-verification, request/confirm-email-change | `apis/record_auth.go` l.202, l.302, l.379, l.433, l.471, l.525, l.568, l.603 |
| 400 | Something went wrong while processing your request. | An error occurred while validating the form. | request-password-reset, request-verification | `apis/record_auth.go` l.383, l.475 |
| 400 | Failed to confirm password reset. / Failed to confirm verification. | Something went wrong while processing your request. | confirm-password-reset, confirm-verification | `apis/record_auth.go` l.440-460, l.532-552, `apis/base.go` l.75-80 |
| 400 | Invalid JSON body. | Something went wrong while processing your request. | POST /api/realtime | `apis/realtime.go` l.172-174 |
| 400 | Missing clientId. | Something went wrong while processing your request. (data `clientId` `validation_required`) | POST /api/realtime | `apis/realtime.go` l.177-179 |
| 400 | Failed to create record. Validation failed for: … (a user's create; an admin's is tagged `form`) | Failed to create record. | record create | `apis/record_crud.go` l.250-261 (`DrySubmit`, `forms/record_upsert.go` l.702) |
| 400 | Failed to update record. Validation failed for: … | Something went wrong while processing your request. | record update | `apis/record_crud.go` l.372, `forms/record_upsert.go` l.744-747, `apis/base.go` l.75-80 |
| 400 | Failed to authenticate. (data `identity`/`password` `validation_required` or `{}`) | Something went wrong while processing your request. (data: only the failing fields) | record auth-with-password | `apis/record_auth.go` l.311, `forms/record_password_login.go` l.43-57, `apis/base.go` l.75-80 |
| 400 | Failed to authenticate. (data `identity` `validation_invalid_credentials`) | Failed to authenticate. (data `{}`) | record and admin auth-with-password | `apis/record_auth.go` l.316-317, `apis/admin.go` l.97-98 |
| 404 | Collection '<name>' not found. | The requested resource wasn't found. | `/api/collections/{c}` (admin) | `apis/collection.go` l.62, l.117, l.156 |
| 404 | Log not found. | The requested resource wasn't found. | `/api/logs/…` | `apis/logs.go` l.72, l.77 |
| 400 | Invalid settings payload. (also an invalid JSON body, which answered a plain 500) | An error occurred while loading the submitted data. | PATCH `/api/settings` | `apis/settings.go` l.52-54 |
| 400 | Failed to send the test email. (data `body` `validation_invalid_value`: a body that is not a JSON object; an invalid JSON body answered a plain 500) | An error occurred while loading the submitted data. (data `{}`) | POST `/api/settings/test/email` | `apis/settings.go` l.112-115 |
| 400 | Invalid UTF-8 JSON body. | An error occurred while loading the submitted data. | POST `/api/backups` | `apis/backup.go` l.76-77 |
| 404 | Not Found (Starlette `detail`) | Not Found. | unknown route | `apis/base.go` l.72-74 (echo `HTTPError` through `NewApiError`) |

Messages already identical (e.g. "The requested resource wasn't found.",
"Failed to authenticate.", "Only admins can filter by @collection." and
"... @request.", "Missing or invalid client id.", "The current and the
previous request authorization don't match.", "The request requires auth
record from <collection> collection.", a persistence error's "Failed to
create/update record.", the 0.22 admin forms and admin password reset of
`docs/pocketbase-022-compatibility.md`, already worded like 0.22.34) only
change envelope. A 400 of a list route that is
not tagged (for example raised by a host list-request hook) keeps its
message.

Record form `data`: every missing required **schema** field reports
`validation_required` "Missing required value." (0.22.34
`forms/validators/record_data.go` l.21, l.79-83; PPBase says "Cannot be
blank.", or "Must be true." for a bool), applied by the record create/update
handlers (`compat_record_errors`, they know the schema); auth system fields
keep ozzo's "Cannot be blank.".

## Evidence

- `tests/test_error_compat_unit.py` (modes, `pb`/environment activation,
  translation table, tags, the middleware consuming the tag, untouched
  bodies), `tests/integration/test_error_compat.py` (both modes on 400, 401,
  403, 404 and a 500 JSON route: create/update form errors per caller,
  relation targets, `createRule` mismatch, login forms, bad `perPage`, host
  bodies untouched, the tag never leaks) and
  `tests/integration/test_error_compat_guards.py` (every guard, both modes).
- `tests/pocketbase_parity/expand_rules_probe.py` against the 0.22.34
  binary: `errors` group (82 requests) and `guards` group (40: the `$apis`
  middlewares in `pb_hooks` against `pb.apis`, the `pb.require_auth()` /
  `pb.require_record_auth()` dependencies and the `event.require_*()`
  helpers), plus `realtime_auth` (5 successive subscription requests of one
  client): every message and `data` identical; the known differences are the
  statuses and the token detail listed below. The `errors` group includes
  the errors WS-A5 and WS-A2 added or changed: hidden fields, modifiers and
  qualified paths in a list filter/sort (opaque 400), "Only admins can
  filter by @request." (filter and sort), the admin create/update forms
  (blank, short password), the admin password reset request/confirm (blank,
  invalid e-mail, unknown e-mail 204, bad token, mismatch, invalid JSON),
  admin auth-refresh without a token or with a record token,
  request-email-change with an admin token, settings and logs guards, and
  the `c.Bind` failures of the admin, settings and backup forms.

## Known differences (not rewritten)

Status codes that PPBase answers differently, which this mode does not
change:

- an auth route on a non-auth collection: PPBase 404 "The collection … is not
  an auth collection.", 0.22.34 400 "Unsupported collection type."
  (`apis/middlewares.go` l.269-271);
- `POST /api/files/token` without auth: PPBase 401 (0.22 mode: "The request
  requires admin or record authorization token to be set.", the
  `pb.require_auth()` guard of the route), 0.22.34 400 "Failed to generate
  file token." (`apis/file.go` l.39, l.72-75: the route has no guard; current
  PocketBase binds `RequireAuth()`, v0.40.6 `apis/file.go` l.44). Aligning it
  needs a change of `ppbase/api/files.py`, outside this mode;
- an unknown method on a known path: PPBase 405, 0.22.34 404 "Not Found.";
- route guards (message identical, status not): `require_guest_only()`
  with any auth PPBase 403, 0.22.34 400 (`apis/middlewares.go` l.37);
  `require_auth("c")`, `pb.require_record_auth()` and
  `event.require_auth_record()` with an admin token PPBase 403, 0.22.34 401
  (l.77); `require_superuser_auth()` and `event.require_superuser()` with a
  record token PPBase 403, 0.22.34 401 (l.119).

Validation details inside `data` keep PPBase's wording outside the cases
above (client impact: the text of `ClientResponseError.response.data[field]
.message`, and some codes; the top-level message and status are those of
0.22.34). Measured or read in the source:

- record field constraints other than required: some PPBase texts differ
  from 0.22.34 `forms/validators/record_data.go` l.140-393, for example
  "Must be at most N character(s)." (0.22.34 "Must be less than N
  character(s)."), "Must be >= N." (0.22.34 "Must be larger than
  N.000000."), "Must be a valid email address." (0.22.34 "Must be a valid
  email."), "Must select at most N value(s)." (0.22.34 "Select no more than
  N."); "Must be at least N character(s)." is identical;
- auth system fields: a duplicate email is `validation_not_unique` "The
  email is already in use." in PPBase, `validation_invalid_email` "The email
  is invalid or already in use." in 0.22.34 (`forms/record_upsert.go`
  l.584-598); username likewise (l.565-578);
- a unique index violation on save: 0.22.34 reports the field as
  `validation_not_unique` (`prepareError`, l.930-953), PPBase `data` `{}`
  (same "Failed to create/update record." message);
- an unparsable verification token: 0.22.34
  `validation_invalid_token_claims`, PPBase `validation_invalid_token`;
- `confirm-password-reset` / `confirm-verification` with missing fields:
  0.22.34 validates `token`, `password` and `passwordConfirm` together
  (`forms/record_password_reset_confirm.go` l.47-49), PPBase stops at the
  token;
- admin login with an empty or non-email identity: 0.22.34 form errors
  (`forms/admin_login.go` l.43), PPBase "Failed to authenticate.";
- a list query string over 2048 characters: 0.22.34 400 "Query string is
  too large." (`apis/record_crud.go` l.78-81), PPBase has no such limit.

PPBase-only routes (backups, hooks,
migrations, batch, OTP, impersonate) keep their messages and only change
envelope. Admin collection create/update validation errors keep PPBase's
message and data (0.22.34: "Something went wrong while processing your
request." or "Failed to create/update the collection." with form errors,
`apis/collection.go` l.85-99, l.124-138). A Python exception that escapes
every handler is still Starlette's
plain-text 500; PocketBase 0.22.34 turns a recovered panic into a 400 JSON
(`apis/base.go` l.52, l.75-80).
