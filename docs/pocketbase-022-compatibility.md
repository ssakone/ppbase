# PocketBase 0.22 sessions, tokens and secrets

Applications migrated from PocketBase 0.22 keep their users signed in when
the migration copies each auth record `tokenKey`, the admins' `tokenKey` and
the 0.22 token secrets into the auth collection options (`authToken`,
`passwordResetToken`, `verificationToken`, `emailChangeToken`, `fileToken`;
the 0.22 `admin*` secrets go to `_superusers`). This page lists what PPBase
then accepts and what it never serves. References are PocketBase source tags
and files.

## Tokens issued by PocketBase 0.22

PocketBase 0.22 signs HS256 JWTs with `tokenKey + <secret of the purpose>`
(`v0.22.34 tools/security/jwt.go:45-58`). Records use `type=authRecord` for
every purpose and admins `type=admin` (`tokens/tokens.go:4-7`): only the
secret tells a session from a reset link.

| Token | Claims | Accepted by PPBase |
| --- | --- | --- |
| record session | `exp, id, type=authRecord, collectionId` | every route, `auth-refresh` (answers a PPBase `type=auth` token) |
| admin session | `exp, id, type=admin` | every superuser route, `/api/admins/auth-refresh` |
| verification, password reset | `+ email` | `confirm-verification`, `confirm-password-reset` |
| email change | `+ email, newEmail` | `confirm-email-change` |
| record or admin file token | no `for` claim | protected file downloads, with the `fileToken` secret (proved over HTTP with tokens issued by the 0.22.34 binary) |
| admin password reset | `exp, id, type=admin, email` | `POST /api/admins/confirm-password-reset` |

Purpose tokens are checked like PocketBase 0.22.34: the signature with the
purpose secret, the record collection (`forms/record_*_confirm.go`); a
session token is never accepted as a purpose or file token because it is
signed with another secret. PPBase's own tokens keep their explicit types
(`auth`, `verification`, `passwordReset`, `emailChange`, `for=file`).

A token whose `collectionId` names a base or view collection is ignored
(anonymous request), as in `v0.22.34 daos/record.go:432-434`.

## Admin password reset (0.22 routes)

`POST /api/admins/request-password-reset` and
`POST /api/admins/confirm-password-reset` follow `v0.22.34 apis/admin.go:21-22`
and answer the statuses, codes and messages of the 0.22.34 binary, compared
request by request (synthetic data):

- Request: blank, then more than 255 bytes, then e-mail format, first error
  only (`forms/admin_password_reset_request.go:47-55`). The format is
  govalidator `IsEmail`, the rule of ozzo `is.EmailFormat` in 0.22.34: it
  refuses `someone@[127.0.0.1]`, `us..er@example.com` or `.user@example.com`
  and accepts a few odd quoted local parts such as `"j"@example.com`.
- Confirmation: token, password and confirmation are checked together. The
  password has 10 to 72 bytes, not characters (`Length(10, 72)`,
  `forms/admin_password_reset_confirm.go:45`): five two-byte characters pass,
  37 are refused, with a valid token or not.
- Body: JSON, `application/x-www-form-urlencoded` and `multipart/form-data`,
  read like the 0.22.34 binder (`tools/rest/multi_binder.go:29-146`): case
  folded keys, first form value, query string merged with form bodies,
  `@jsonPayload`, a lone surrogate escape read as U+FFFD. Another or no
  `Content-Type` answers `400 An error occurred while loading the submitted
  data.` The details and the measured cases are in `ppbase/api/legacy_forms.py`.
- `204` for any valid e-mail (known or not), a 120 second resend throttle on
  `last_reset_sent_at`, a link `{appURL}/_/#/confirm-password-reset/{token}`,
  `updated` set with `last_reset_sent_at` when the e-mail is sent (`SaveAdmin`),
  a new token key after the reset.

The e-mail is sent only through the `PPBASE_SMTP_*` settings and the link or
token is never logged. The PPBase dashboard has no page for this link (nor for
the record confirmation links): the token can be posted to the API.

## Passwords and bcrypt's 72 bytes

bcrypt cannot hash more than 72 bytes, which a password of 71 characters or
less can reach with multi-byte characters; no route answers `500` for that:

- `POST /api/admins` and `PATCH /api/admins/{id}` apply the 0.22.34 admin
  rule, 10 to 72 bytes (`forms/admin_upsert.go:75-79`), with the 0.22.34
  answer `400 Something went wrong while processing your request.`; an
  unknown admin is a `404` first.
- `POST /api/admins/init` and `POST /api/admins/{id}/change-password`, PPBase
  routes, keep their rule and answer over 72 bytes the `validation_invalid_value`
  error of a v0.40.6 superuser (`core/field_password.go:170-172`).
- A record `confirm-password-reset` over 72 bytes answers `400` with empty data,
  as 0.22.34 does from 73 to 100 bytes (`forms/record_password_reset_confirm.go:48`,
  `:92-93`).

Passwords are hashed and checked as the Go strings PocketBase would hash: a
lone surrogate escape in JSON is U+FFFD, so a password set through PocketBase
0.22 with such escapes still signs in.

## E-mailed links

Every e-mailed link (admin password reset, record verification, password
reset and e-mail change) starts with the stored `meta.appURL`, like
PocketBase (`v0.22.34 mails/admin.go:22-26`, `mails/record.go:175-184`;
`v0.40.6 mails/record.go:265-266`), used as stored apart from a trailing slash
(no `0.0.0.0` rewrite). Without a settings row, or with an empty or
non-absolute `appURL`, the default `http://localhost:8090` is used, as in
PocketBase (`v0.22.34 models/settings/settings.go:77`). The `Host` and
`X-Forwarded-*` request headers are never used: they are chosen by the
caller, who could otherwise have the real server send a link to their own
site and read the token from the URL fragment. Measured on the 0.22.34 binary:
forged headers change neither the admin nor the record link. Set `appURL`
before sending any link.

Without `PPBASE_SMTP_HOST` nothing is sent, and the message, link, token or OTP
code is logged only in the explicit development mode (`PPBASE_DEV=true`);
otherwise a warning without recipient, link or code is logged. The admin
password reset link is never logged. PocketBase 0.22 would use its own SMTP
settings or `sendmail` instead: PPBase does not read `smtp` from
`/api/settings` for these e-mails.

## Realtime identity

A realtime client keeps the identity of its first authenticated subscription
request: the record `collectionId` and `id`, never the token type
(`v0.22.34 apis/realtime.go:187-203`, `v0.40.6 apis/realtime.go:218-231`,
`isSameAuth :843-853`). A 0.22 token refreshed by PPBase therefore keeps
subscribing on the same connection. A guest may authenticate; any other change
answers `403 The current and the previous request authorization don't match.`
When the auth record or admin is deleted, its clients become guests
(`v0.22.34 apis/realtime.go:241-280`).

PPBase compares the collection and the id like v0.40.6 `isSameAuth`, whereas
v0.22.34 compares the id alone (`extractAuthIdFromGetter`,
`apis/realtime.go:539-551`). Switching one SSE client to an account of another
auth collection, or from an admin to a record, that has the same id is
accepted by 0.22.34 and refused here (`403`). Ids are random 15-character
strings, so this needs two accounts sharing an id.

## Secrets are never served

- Collections: the five token secrets and OAuth2 `clientSecret` are omitted
  from every response (`v0.40.6 core/collection_model.go:564-584`). Saving a
  served collection keeps them: an absent, `null`, empty or `******` secret
  keeps the stored one and OAuth2 providers are merged by name
  (`v0.40.6 core/collection_model_auth_options.go:415-470`). A `null` token
  configuration, `oauth2` or other option section keeps the stored one, as
  Go ignores a JSON `null` bound to a struct (it used to replace the section
  and lose its secrets). To rotate a secret, send a new value.
- Settings: `GET` and `PATCH /api/settings` answer the 0.22.34 shape where each
  non-empty secret is `******` (`smtp.password`, `s3.secret`,
  `backups.s3.secret`, any 0.22 token or OAuth2 key stored in the document;
  `v0.22.34 apis/settings.go:29-30`, `models/settings/settings.go:279-334`). A
  `PATCH` with `******` keeps the stored secret, `""` clears it. `null`, at any
  depth (a section such as `{"smtp": null}` or a value), keeps the stored
  value, as measured on 0.22.34. The dashboard removes masked values before
  saving, like the 0.22.34 dashboard.
- Request logs: the request logger stores bodies redacted key by key. A
  urlencoded body (such as an admin reset confirmation with `token` and
  `password`) is redacted like JSON, keys folded like PocketBase reads them
  (`paſſword`, with long s, is a password); a JSON body that cannot be parsed is
  stored as `[REDACTED MALFORMED BODY]`, never as is, and a body beyond Python's
  integer or nesting limits no longer turns the handled request into a `500`.
  `text/*` bodies are still stored as sent and multipart bodies never.
- SQL errors: every engine sets `hide_parameters=True`, so SQLAlchemy never
  appends the bound values (password hashes, token keys, secrets) to
  `DBAPIError` messages or echo logs. This does not cover values quoted by the
  driver or the server: asyncpg copies up to 40 characters of a value it
  cannot encode, PostgreSQL quotes the input of a failed cast, and the
  `DETAIL` of a unique violation (`Key (column)=(value) already exists.`)
  appears in full tracebacks through the chained asyncpg exception. Treat the
  PPBase logs as confidential.

## Statuses and messages

| Case | Answer |
| --- | --- |
| no, invalid or expired record token on a record route requiring it | `401 The request requires valid record authorization token to be set.` |
| admin token on a record `auth-refresh` | `401`, same message |
| record token of another auth collection | `403 The request requires auth record from <token collection> collection.` |
| superuser route without a valid admin token | `401 The request requires valid admin authorization token to be set.` |

## Known differences

- Error envelope: PPBase answers `{status, message, data}` (current
  PocketBase), PocketBase 0.22 `{code, message, data}`; a host whose clients
  were written for 0.22 enables the 0.22 error mode
  (`pb.set_error_compat("0.22")`, `docs/error-compat.md`), which answers the
  0.22.34 envelope and messages of the errors above.
- Tokens issued by PPBase use `type=auth`, so PocketBase 0.22 rejects them
  and the PocketBase JS SDK 0.21 `isAuthRecord` is false after a PPBase
  refresh.
- `GET /api/settings` has the current PPBase shape (no 0.22 token or OAuth2
  sections); 0.22 token keys sent to `PATCH /api/settings` are stored but do
  not rotate the collection secrets.
- PocketBase 0.22.34 stores a `******` sent to `PATCH /api/settings` as the
  new secret (its dashboard strips it first); PPBase keeps the stored one.
- A `null` sent to `PATCH /api/settings` for the 0.22 `pkce` pointer or the
  `emailAuth` domain lists keeps the stored value, where 0.22.34 resets them
  (`models/settings/settings.go:639`, `:695-696`); PPBase does not use them.
- An empty collection token secret keeps the stored one where PocketBase
  >= 0.23 answers a validation error.
- Purpose-token refusals use `validation_invalid_token` where 0.22.34 has
  more specific codes (`validation_invalid_token_claims`,
  `validation_token_collection_mismatch`); PPBase also refuses a reset or
  email-change link whose `email` claim no longer matches the record.
- Realtime identity: collection and id (v0.40.6) instead of the id alone
  (0.22.34), see above.
- `PATCH /api/admins/{id}` (and `POST /api/admins`) keep `passwordConfirm`
  optional, where 0.22.34 requires it with a password
  (`forms/admin_upsert.go:80-84`); the rest of the 0.22 admin form (id,
  avatar bounds, e-mail rules) is not reproduced either.
- Body binding of the admin reset routes, beyond the measured cases: LF-only
  multipart line endings and blanks after a multipart boundary are refused
  (accepted by Go), invalid UTF-8 in a form value counts three bytes (Go: one),
  and Python's JSON decoder stops a few levels before Go's 10000-level limit.
- A protected file refused to a token is a `404` (current PocketBase,
  `v0.40.6 apis/file.go:132-133`) where 0.22.34 answers `403 Insufficient
  permissions to access the file resource.` (`v0.22.34 apis/file.go:136-138`).
- A record reset refusal keeps PPBase's message (`Failed to confirm password
  reset.`, 0.22.34: `Something went wrong while processing your request.`) and
  PPBase's 71-character password rule.
- The record verification and password reset requests have no resend
  throttle (0.22.34: 120 seconds, `forms/record_verification_request.go:37`,
  `forms/record_password_reset_request.go:38`), and the record e-mails have
  PPBase's fixed text: neither the 0.22 templates of the settings
  (`meta.verificationTemplate`, `meta.resetPasswordTemplate`,
  `meta.confirmEmailChangeTemplate`, `models/settings/settings.go:81-83`) nor
  the templates of the auth collection options are used. The admin password
  reset has the 0.22.34 throttle.
