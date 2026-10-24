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
| record or admin file token | no `for` claim | protected file downloads, with the `fileToken` secret |
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
`POST /api/admins/confirm-password-reset` follow `v0.22.34 apis/admin.go:21-22`:
validation codes and messages of the 0.22.34 binary, `204` for any valid
e-mail (known or not), a 120 second resend throttle on `last_reset_sent_at`,
a link `{appURL}/_/#/confirm-password-reset/{token}`, a new token key after
the reset. The e-mail is sent only through the `PPBASE_SMTP_*` settings and
the link or token is never logged. The PPBase dashboard has no page for this
link (nor for the record confirmation links): the token can be posted to the
API.

## Realtime identity

A realtime client keeps the identity of its first authenticated subscription
request: the record `collectionId` and `id`, never the token type
(`v0.22.34 apis/realtime.go:187-203`, `v0.40.6 apis/realtime.go:218-231`,
`isSameAuth :843-853`). A 0.22 token refreshed by PPBase therefore keeps
subscribing on the same connection. A guest may authenticate; any other change
answers `403 The current and the previous request authorization don't match.`
When the auth record or admin is deleted, its clients become guests
(`v0.22.34 apis/realtime.go:241-280`).

## Secrets are never served

- Collections: the five token secrets and OAuth2 `clientSecret` are omitted
  from every response (`v0.40.6 core/collection_model.go:564-584`). Saving a
  served collection keeps them: an absent, `null`, empty or `******` secret
  keeps the stored one and OAuth2 providers are merged by name
  (`v0.40.6 core/collection_model_auth_options.go:415-470`). To rotate a
  secret, send a new value.
- Settings: `GET` and `PATCH /api/settings` answer the 0.22.34 shape where each
  non-empty secret is `******` (`smtp.password`, `s3.secret`,
  `backups.s3.secret`, any 0.22 token or OAuth2 key stored in the document;
  `v0.22.34 apis/settings.go:29-30`, `models/settings/settings.go:279-334`). A
  `PATCH` with `******` or `null` keeps the stored secret, `""` clears it. The
  dashboard removes masked values before saving, like the 0.22.34 dashboard.
- SQL errors and logs never contain bound values: every engine sets
  `hide_parameters=True`.

## Statuses and messages

| Case | Answer |
| --- | --- |
| no, invalid or expired record token on a record route requiring it | `401 The request requires valid record authorization token to be set.` |
| admin token on a record `auth-refresh` | `401`, same message |
| record token of another auth collection | `403 The request requires auth record from <token collection> collection.` |
| superuser route without a valid admin token | `401 The request requires valid admin authorization token to be set.` |

## Known differences

- Error envelope: PPBase answers `{status, message, data}` (current
  PocketBase), PocketBase 0.22 `{code, message, data}`.
- Tokens issued by PPBase use `type=auth`, so PocketBase 0.22 rejects them
  and the PocketBase JS SDK 0.21 `isAuthRecord` is false after a PPBase
  refresh.
- `GET /api/settings` has the current PPBase shape (no 0.22 token or OAuth2
  sections); 0.22 token keys sent to `PATCH /api/settings` are stored but do
  not rotate the collection secrets.
- PocketBase 0.22.34 stores a `******` sent to `PATCH /api/settings` as the
  new secret (its dashboard strips it first); PPBase keeps the stored one.
- An empty collection token secret keeps the stored one where PocketBase
  >= 0.23 answers a validation error.
- Purpose-token refusals use `validation_invalid_token` where 0.22.34 has
  more specific codes (`validation_invalid_token_claims`,
  `validation_token_collection_mismatch`); PPBase also refuses a reset or
  email-change link whose `email` claim no longer matches the record.
