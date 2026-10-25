# Error envelope: `status` by default, `code` in the 0.22 mode

PPBase answers every error with the exact envelope of the current PocketBase
version, `{"status": <http status>, "message": ..., "data": {...}}` (v0.40.6
`tools/router/error.go` l.36-42). PocketBase 0.22.34 answered
`{"code": <http status>, "message": ..., "data": {...}}` (`apis/api_error.go`
l.11-15), with its own messages.

Clients written for PocketBase 0.22 (the 0.22 JS/Dart SDKs read
`response.code`) select the opt-in PocketBase 0.22 error mode
(`pb.set_error_compat("0.22")`, `PPBASE_ERROR_COMPAT=0.22` or
`Settings(error_compat="0.22")`): the envelope becomes exactly `{code,
message, data}` and the messages those of 0.22.34. Both behaviors come from
one outermost middleware, `ppbase/middleware/error_compat.py`, the only place
that post-processes error bodies; see `docs/error-compat.md` for the
translated messages, the handler tags and the known differences.

An interim version added `code` next to `status` in every PPBase error body
(`ErrorEnvelopeCodeMiddleware`, never released). It was removed: the hybrid
`{code, status, message, data}` matched neither version, and it ran before the
0.22 mode, which then no longer recognized PPBase's envelope and left the
current messages untranslated. Client impact: none for a released version; a
client of the default mode reads `status` (as with current PocketBase), a
0.22 client enables the 0.22 mode. Tested by `tests/test_error_envelope.py`
(a single error middleware, outermost; both envelopes) and the exact bodies of
`tests/test_backup_api.py`, `tests/integration/test_public_dir_static.py` and
`tests/integration/test_rate_limit_middleware.py`.
