# Changelog

## Unreleased

### Security follow-up: qualified paths, modifiers, realtime payload, expand

See `docs/hidden-auth-fields.md`, `docs/request-static-fields.md` and
`docs/error-envelope.md`.

- A dotted filter path must start with a relation, a back relation or a JSON
  field: `users.password_hash~"$2b$"` (table-qualified self reference) used to
  read the hidden column, and `other.col` produced a 500; both are now an
  opaque 400 like in PocketBase 0.22.34.
- A system field never takes a modifier: `email:lower`, `email:length`,
  `email:isset`, `email:each`, `id:length`... are a 400 (they used to drop the
  email visibility guard, a masked-email oracle).
- Every list filter/sort error answers PocketBase's opaque message.
- Realtime payloads no longer send a hidden email to every subscriber: only
  the owner whose `manageRule` check passes receives it, like PocketBase
  0.22.34 (admins do not).
- Expand applies the related collection's `viewRule` (nil: admins only).
- `emailVisibility`/`verified` are parsed like Go's `cast.ToBool` on create
  and update: the multipart/JSON string `"false"` is false.
- `username` resolves in a filter/sort only when the auth collection has a
  `username` column (400 instead of a 500 otherwise).
- A present `@request.*` value compared with a literal, `true`/`false` or
  another request value is folded with SQLite's comparison (`1 = true`,
  `"true" != true`) instead of being bound against a boolean (500); an absent
  value is SQL NULL, so `<`, `>`, `~` never match it.
- `totalPages` is `ceil(totalItems / perPage)` (0 when empty) for the
  collections, admins and logs lists too.
- JSON error bodies carry PocketBase 0.22's `code` next to `status`.

### File downloads like PocketBase

See `docs/file-serving.md`.

- `GET`/`HEAD /api/files/...` take the content type from the `.attrs`
  side-car of a PocketBase storage (an undecodable one is a 404), always send
  `Content-Disposition` (`inline` for PocketBase's safe types, `attachment`
  otherwise: `text/*` is now an attachment), PocketBase's
  `Content-Security-Policy`, `Cache-Control`, `X-Content-Type-Options` and
  `X-XSS-Protection`, and answer `HEAD`.
- `?download` is a Go boolean (`1`, `t`, `true`...): `?download=<name>` no
  longer renames the download.
- `?thumb` serves a stored `thumbs_<file>/<size>_<file>` as is, or generates
  it like `CreateThumb` (only `100x100` and the field's sizes, otherwise the
  original), one generation per thumbnail at a time.
- Where PocketBase 0.22.34 and 0.40.6 differ (file name quoting, `.js` and
  Office overrides, `Cross-Origin-Opener-Policy`, webp thumbnails, thumbnail
  format, `.attrs` JSON decoding, file token trimming), PPBase follows 0.40.6
  by default and 0.22.34 when `app.state.error_compat` is `"0.22"` (the
  PocketBase 0.22 mode).
- A protected file's token is the first `?token` value, trimmed like Go's
  `strings.TrimSpace` in the 0.22 mode (as PocketBase 0.22.34: Go's Unicode
  spaces, not Python's `str.strip()` set; an empty result is no token) and
  verified as received by default (as 0.40.6).
  It must decode like golang-jwt: a padded token or one with any other
  character than the URL-safe alphabet is refused (PyJWT accepted
  `token=`), CR/LF in its signature segment are skipped.
- On local storage a name ending in `.attrs` is a `404`, even when a record
  references it (it was served as a JSON attachment).
- In the 0.22 mode an `.attrs` md5 array element that is not a byte is a
  `404` whatever its length (a 5000-digit one was a `500`).

### Absent @request.* values no longer 500; totalPages and expand key

See `docs/request-static-fields.md`.

- An absent `@request.*` value (anonymous caller, or a role-permission key the
  flattened BUMA context does not declare) is resolved with PostgreSQL-safe,
  PocketBase-equivalent equality instead of `$n IS NOT NULL`/`'' = TRUE`,
  which asyncpg/PostgreSQL rejected. Anonymous lists of `buma_article_stockage`,
  `buma_pdv` and the other role-permission collections return 200 with the
  authorized rows.
- `totalPages` of an empty result set is `0` (was `1`), matching
  `ceil(totalItems / perPage)`.
- A record with no expanded relation no longer carries an empty `expand: {}`;
  the key is omitted, as in PocketBase.

### Security: hidden auth fields are not filterable/sortable

See `docs/hidden-auth-fields.md`.

- A filter, sort or rule may no longer reference `password`,
  `passwordHash`/`password_hash`, `tokenKey`/`token_key`, `email_visibility`
  or `lastResetSentAt`; PocketBase 0.22.34 returns the same opaque 400 for
  every caller. This closes an anonymous oracle on the bcrypt hash, token key
  and masked emails (`users.listRule` is `""`).
- A non-admin list request and a realtime client filter restrict an `email`
  comparison to `emailVisibility = TRUE`; admins and server-evaluated access
  rules carry no guard. `emailVisibility`/`verified`/`username` resolve to
  their columns.
- Realtime subscriptions enforce the `@collection.`/`@request.` admin-only
  check on both the `filter` and `sort` options and evaluate the client
  filter without hidden-field access.
- Auth record create now accepts PocketBase's `emailVisibility` wire name (it
  previously ignored it, leaving visibility always off).

### Empty dates served, filtered and sorted like PocketBase

See `docs/empty-dates.md`.

- An empty date is served `""` (never `null`); view records omit empty
  `created`/`updated`.
- Filters and sorts give the stored `NULL` the semantics of PocketBase's
  `''` (before every date), including relation and `@collection` leaves,
  where a missing target stays SQL `NULL`, and multi-match subqueries.

### Relation paths: PocketBase LEFT JOIN and multi-match

See `docs/filter-paths-multi-match.md`.

- Relation, back-relation and `@collection` paths in filters and API rules
  are LEFT JOINs shared by the whole expression, like PocketBase 0.22.34: a
  missing target is a NULL leaf (`rel.f = ""` and `rel.id = ""` match a record
  without or with an orphan relation) and `rel.id` joins the target.
- Comparisons without `?` through `@collection`, a multiple relation or a
  back relation (without single-column unique index) need every row to match
  (PocketBase multi-match; a NULL row fails every operator but `=`).
  `@collection.users.id = @request.auth.id` is now false with two accounts or
  more: in the BUMA `users` create/update rules, an ordinary user can no
  longer create accounts or edit other accounts.
- Guests and auth records get 403 "Only admins can filter by @collection."
  (or `@request.`) for those fields in a list `filter`/`sort`.

### Relation multiplicity (PocketBase 0.22 `maxSelect: null`)

See `docs/relation-multiplicity.md` for the rule, the PocketBase references
and the known differences.

- A relation with `options.maxSelect: null` (PocketBase 0.22 export) is an
  unlimited multiple relation; the stored schema and the API keep `null`.
- New relation columns are `TEXT`/`TEXT[]` (no 15-character limit); existing
  `VARCHAR(15)`/`VARCHAR(15)[]` columns of the matching shape keep working.
- Relation values follow PocketBase `PrepareValue`; the record API rejects ids
  that do not exist (`validation_missing_rel_records`) before request hooks,
  together with the other field errors of the form.
- Deleting a record cleans its references like PocketBase `DeleteRecord`:
  ids are removed, cascade happens only when no id is left, an emptied
  required relation refuses the delete (HTTP 400).
- Switching a field between single and multiple converts the stored values
  instead of dropping the column.

### Behavior changes for existing collections

- Direct multi-value fields (`maxSelect > 1`) and multi-value path leaves
  compare their JSON text, like PocketBase: `tags ?= "a"` and
  `members ?= @request.auth.id` no longer match an element, `tags = "a"` no
  longer fails with an internal error.
- `:each`: `?` operators are any-match; other operators need a non-empty
  list. `:length` of a single select/file/relation is 0 or 1.
- List or object `@request.*` values are compared as their JSON text.
- Invalid filter identifiers or modifiers return HTTP 400.
- Startup and collection updates refuse a select/file/relation column whose
  shape contradicts its field (for example a `maxSelect: null` relation
  created as `VARCHAR(15)` by an earlier version) until
  `reconcile_multi_value_columns()` has converted it, typically from an
  application migration with `app.reconcile_multi_value_columns(...)`.
