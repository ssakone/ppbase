# Error envelope: `code` and `status`

PocketBase 0.22.34 serializes every API error as
`{"code": <http status>, "message": ..., "data": {...}}`
(`apis/api_error.go` l.12-15). Since v0.23.0 the key is `status`
(`{"data", "message", "status"}`, v0.40.6 `tools/router/error.go` l.36-42).
PPBase builds the current shape in many modules (records, auth, realtime,
settings, backups, exception handlers), so a client written for 0.22 that
reads `response.code` — `ClientResponseError.response.code` (JS SDK) or
`ClientException.response["code"]` (Dart SDK) — got `undefined`/`null`.
The SDKs themselves take `status` from the HTTP status line, not the body.

`ppbase/middleware/error_envelope.py` (outermost ASGI middleware) now adds
`code` next to `status` for exactly PPBase's native error shape: an HTTP
status >= 400 with an `application/json` object body whose integer `status`
equals the HTTP status and which has `message` and `data` and no `code`.
The body becomes `{"code", "status", "message", "data"}`, readable by 0.22
and current clients. Everything else passes through byte for byte:
successful responses, files, SSE, plain text, custom payloads and bodies
already in the 0.22 shape (BUMA's ported routes answer
`{"code", "message", "data"}` themselves, `app/legacy/http.py`).

Client impact: additive. A 0.22 client now finds `code`; a client that
compares the whole error body with PocketBase 0.22.34's would still see the
extra `status` key. Tested by `tests/test_error_envelope.py`.
