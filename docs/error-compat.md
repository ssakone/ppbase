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
unchanged byte for byte.

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
- every other body is left untouched: a host route that already answers
  `{"code", ...}` (BUMA's legacy routes), a custom `{"message": ...}`,
  successes, non-JSON and streamed responses.

Status codes are never changed.

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
| 400 | Failed to create/update record. Validation failed for: … | Failed to create record. / Failed to update record. | record create/update | `apis/record_crud.go` l.286, l.378 |
| 400 | any filter/sort parse error text | Something went wrong while processing your request. | GET record list | `apis/record_crud.go` l.82-86 |
| 400 | Failed to authenticate. (data `identity` `validation_invalid_credentials`) | Failed to authenticate. (data `{}`) | record and admin auth-with-password | `apis/record_auth.go` l.316-317, `apis/admin.go` l.97-98 |
| 404 | Not Found (Starlette `detail`) | Not Found. | unknown route | `apis/base.go` l.72-74 (echo `HTTPError` through `NewApiError`) |

Messages already identical (e.g. "The requested resource wasn't found.",
"Failed to authenticate.", "Only admins can filter by @collection.",
"Missing or invalid client id.") only change envelope.

## Evidence

- `tests/test_error_compat_unit.py` (modes, `pb`/environment activation,
  translation table, untouched bodies) and
  `tests/integration/test_error_compat.py` (both modes on 400, 401, 403,
  404 and a 500 JSON route, host bodies untouched).
- `tests/pocketbase_parity/expand_rules_probe.py` (`errors` group, 35
  requests) against the 0.22.34 binary: 31 identical; the 4 known differences
  are listed below.

## Known differences (not rewritten)

Status codes that PPBase answers differently, which this mode does not
change:

- auth-refresh with an admin token: PPBase 403 "The request requires auth
  record from <collection> collection.", 0.22.34 401 (`apis/middlewares.go`
  l.97-100);
- an auth route on a non-auth collection: PPBase 404 "The collection … is not
  an auth collection.", 0.22.34 400 "Unsupported collection type."
  (l.269-271);
- `POST /api/files/token` without auth: PPBase 401, 0.22.34 400 "Failed to
  generate file token." (`apis/file.go` l.74);
- an unknown method on a known path: PPBase 405, 0.22.34 404 "Not Found.".

Validation details inside `data` keep PPBase's codes (for example an
unparsable verification token: 0.22.34 `validation_invalid_token_claims`,
PPBase `validation_invalid_token`). PPBase-only routes (backups, hooks,
migrations, batch, OTP, impersonate) keep their messages and only change
envelope. A Python exception that escapes every handler is still Starlette's
plain-text 500; PocketBase 0.22.34 turns a recovered panic into a 400 JSON
(`apis/base.go` l.52, l.75-80).
