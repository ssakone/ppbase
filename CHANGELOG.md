# Changelog

## Unreleased

### Error envelope: current PocketBase by default, 0.22 on demand

See `docs/error-envelope.md` and `docs/error-compat.md`.

- The default error envelope is exactly current PocketBase's `{status,
  message, data}` (v0.40.6 `tools/router/error.go` l.36-42). The interim
  `code` key next to `status` (never released) is removed again: it matched
  neither version and hid PPBase's envelope from the 0.22 mode.
- The opt-in PocketBase 0.22 mode (`pb.set_error_compat("0.22")`,
  `PPBASE_ERROR_COMPAT=0.22`, `Settings(error_compat="0.22")`) is the API
  for clients written against PocketBase 0.22: exactly `{code, message,
  data}` with the 0.22.34 messages. One outermost middleware is the only
  error post-processing.

### Security follow-up: qualified paths, modifiers, realtime payload, expand

See `docs/hidden-auth-fields.md` and `docs/request-static-fields.md`.

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

### Security: expand applies the related viewRule

See `docs/expand-view-rules.md`.

- Every expanded record must satisfy the `viewRule` of its collection for the
  current request (PocketBase 0.22.34 `expandFetch`): admins see everything,
  a `null` rule fails the path, any other rule is evaluated by the list
  filter parser with the same `@request.*` context. An anonymous caller or a
  role without the permission no longer reads protected records through a
  public relation (BUMA: `buma_sale.customer`, `users.employee`,
  `buma_payment.caisse_transaction`, ...).
- Paths are normalized like PocketBase, a failing path is skipped, a failing
  nested level drops the whole path, refused or missing targets are skipped
  in field order; `<collection>_via_<field>` back relations are supported.
- Expanded auth records show their email only to an admin, the owner or a
  `manageRule` match.
- Create/update, auth responses (as the authenticated record), realtime and
  `pb.apis.enrich_records`/`record_auth_response` use the same rules; one
  query per path and level, in a savepoint inside write transactions.
- `pb.set_auth_record_rule_context(loader)` extends `@request.auth` for the
  authenticated record of an auth response (BUMA's role permissions);
  `ppbase.api.record_auth.auth_response_rule_context` builds that context for
  host login routes. `pb.apis.enrich_records` passes a JSON body as
  `@request.data`.
- Behavior change: `pb.apis.enrich_record(s)` expands `default_expand` and
  appends the request's `?expand` paths (PocketBase `EnrichRecords`); it used
  to apply `default_expand` only when the request had no `?expand`.
- Realtime delete events still carry only `{id}` and reach only admins and
  public-rule subscriptions without a filter (0.22.34 sends the whole
  record to every matching subscriber): a documented open divergence.

### Security: emails of listed, viewed and written auth records

- The main auth records of a list, a view, a create, an update and a batch
  write show a hidden email (`emailVisibility` false) only to an admin, the
  owner and a `manageRule` match, like PocketBase's `EnrichRecords`. Create
  and update responses used to always show it: a caller allowed to update an
  auth record read its hidden email. A guest signup response now hides the
  new email unless `emailVisibility`.

### PocketBase 0.22 error compatibility (opt-in)

See `docs/error-compat.md`.

- `pb.set_error_compat("0.22")` or `PPBASE_ERROR_COMPAT=0.22` makes every
  PPBase JSON error answer `{code, message, data}` with the PocketBase
  0.22.34 messages ("Only admins can perform this action.", ...). Disabled by
  default; status codes and non-PPBase bodies are never changed.
- Form validation errors take the 0.22.34 message per case (record update
  and an admin's create unwrapped, a user's create "Failed to create
  record.", login forms with only the failing fields), required schema
  fields "Missing required value.", and the `pb.apis` guards, the
  `pb.require_auth()`/`pb.require_record_auth()` dependencies and the
  `event.require_*()` helpers the messages of their 0.22.34 middlewares;
  handlers tag these cases with a private header the middleware strips.
- A body that cannot be bound (invalid JSON, not an object) answers the
  `c.Bind` message of its 0.22.34 route.
- `event.require_superuser()` answers 401 without auth (was 403), like
  `pb.apis.require_superuser_auth()` and both PocketBase versions.
- `PATCH /api/settings` and `POST /api/settings/test/email` answer their 400
  load error to an invalid JSON body (was a plain 500).

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

### NULL values served as PocketBase zero values

See `docs/null-values.md`.

- A `NULL` value (a view `LEFT JOIN` without match, an absent column) is
  served as the zero value of its field type, like PocketBase 0.22.34 and
  v0.40.6: `""` for text, email, url, editor and single select/file/relation,
  `0` for number, `false` for bool, `[]` for multiple select/file/relation
  (a 0.22 `maxSelect: null` relation included), `null` for json. A `NULL` id
  is `""`. Stored values, filters, sorts and totals are unchanged (filters
  and sorts: see the next entry).

### NULL values filtered and sorted like PocketBase on SQLite

See `docs/null-values.md`.

- `=`/`!=` follow PocketBase's `resolveEqualExpr` (v0.22.34
  `tools/search/filter.go` l.284-368, the same in v0.40.6): `""` and `null`
  also match `NULL` (`txt = ""`, `sel = ""`, `rel = ""`, an auth email read
  through a `LEFT JOIN`), `!=` is NULL-safe (`txt != "x"` keeps the `NULL`
  rows) and two columns compare with `COALESCE(x, '')`. A current-record
  `?op` is `op` (`num ?> 1` was a 500). Non-NULL values of one type keep
  their comparison. `totalItems` uses the same clause.
- Operands of different types compare like SQLite instead of a PostgreSQL
  type error (`txt ?= 0`, `num ?= "x"`, `flag ?= 1`, `num ?!= txt` and
  their `=`/`!=` forms were HTTP 500; `?=`/`?!=` had compared texts before
  the current-record rewrite): a TEXT column reads a filter number as its
  REAL text (`txt = 1` matches `'1.0'`, `txt = true` matches `'1'`), a
  number, bool or multi-value column reads a numeric text as a number
  (`num = " 1 "` matches 1, `flag = "1"` the true rows), two columns go
  through `COALESCE(x, '')` without affinity and a number never equals a
  text. A `?=`/`?!=` whose operand type is unknown keeps the text
  comparison.
- Visible in API rules: a stored `''` is empty too, so `txt = null` matches
  it (before: `txt IS NULL`), and an anonymous `@request.auth.id` (`NULL`)
  `?= owner` lists the records without owner, like PocketBase.
- A comparison without any column (`1 = null`, `"" = 0`, `true = 1`,
  `1 ?= null`) is folded like SQLite compares the bound values
  (`1 = ""` was a 500).
- A json operand compared with `null` or `""` reads like `JSON_EXTRACT`: a
  SQL `NULL` and a stored JSON `null` are both `null` (also for a base
  collection, the filter part of BUMA D-0029), only the JSON string `""` is
  `""` (`meta = ""` was a 500); relation and `@collection` json leaves too.
  `~`/`!~`/`?~`/`?!~` and multi-match subqueries also read a JSON `null` as
  `NULL`, `json != v` and `json ?!= v` keep the `NULL` rows, and a json
  operand compared with a column is NULL-safe for `=`, `!=`, `?=` and `?!=`
  (`meta ?!= txt` no longer keeps, and `meta ?= txt` no longer misses, a
  row where both are `NULL`); a date compared with json reads as its text
  (`meta = day` was a 500).
- Sort: `NULL` comes first in ascending and last in descending order for
  every view column and every json value (a JSON `null` sorts as `NULL`),
  like dates already did. Base and auth columns other than dates are
  `NOT NULL` and keep their term.
- Still different (documented): a multi-value field sorts by its array in
  PPBase and by its JSON text in PocketBase; in a view, an empty date and a
  `LEFT JOIN` `NULL` tie, and a view date compared with json or matched by
  `?~`/`?!~` reads that `NULL` as an empty date; a json value compared with
  a non-null value keeps PPBase's jsonb reading (`meta = 5` on a `jsonb`
  column is still a 500); relation paths and `<`/`>` between operands of
  different types keep the typed comparison.

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
