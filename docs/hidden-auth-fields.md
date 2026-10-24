# Hidden auth fields in filters, sorts, rules, expand and realtime

PocketBase 0.22.34 resolves a filter/sort/rule field only when it is a
base-model field (`id`, `created`, `updated`), a collection schema field or,
for an auth collection, one of `username`, `verified`, `emailVisibility`,
`email` (`resolvableSystemFieldNames`,
`resolvers/record_field_resolve_runner.go` l.718-732). Every other name —
`password`, `passwordHash`/`password_hash`, `tokenKey`/`token_key`,
`email_visibility` (snake), `lastResetSentAt`, … — is an "unknown field"
error for every caller, admin included (0.22.34 has no hidden-field flag;
the flag only gates the `email` guard, l.361-376).

PPBase stores these as physical columns (`password_hash`, `token_key`,
`email_visibility`). A filter used to resolve straight to the column, so an
anonymous caller could probe the bcrypt hash or token key
(`GET .../users/records?filter=password_hash~"$2a$12$A"`) or a masked email,
because BUMA's `users.listRule` is `""`. PPBase now rejects those fields,
including the bypasses found in review:

- **Table-qualified or unresolved dotted paths**: PocketBase's first path
  segment must be a relation, a back relation (`x_via_y`) or a JSON field
  ("failed to resolve field" / "is not a valid relation", l.517-523 and
  l.635-636). PPBase used to emit any other dotted path verbatim, so
  `users.password_hash~"$2b$"` read the current table's hidden column and
  `other.col` produced invalid SQL (500). With collection metadata the parser
  now raises (400); only a metadata-less parser keeps the legacy raw path.
- **Modifiers on system fields**: PocketBase matches the whole prop
  (`email:lower`) against the system names and then looks the bare name up in
  `collection.Schema`, which never holds a system field, so `email:lower`,
  `email:length`, `email:isset`, `email:each`, `id:length`, `created:isset`,
  `verified:length`… are unknown fields (l.361-395; `splitModifier` even
  rejects `lower`, `resolvers/record_field_resolver.go` l.286-301). PPBase
  used to drop the email guard under a modifier; it now raises on the current
  table, relation, back-relation and `@collection` leaves. (v0.40.6 resolves
  `email:lower` with the guard kept, `core/record_field_resolver_runner.go`
  l.823-849; BUMA's clients follow 0.22.34.)

## Rules enforced

- **Filter / sort / collection rule** (`list_records`, `check_record_rule`):
  an unresolvable field raises `FilterFieldError` and the list API returns
  PocketBase's opaque `400` — like every other filter/sort error, whose
  message is always "Something went wrong while processing your request."
  with `data: {}` (`NewBadRequestError("", err)`, `apis/record_crud.go`
  l.83-86). The body carries both `code` and `status`
  (`docs/error-envelope.md`).
- **`email` visibility guard**: for a non-admin list request (filter and
  list rule, both resolved with `allowHiddenFields=false`, l.59-66) and for a
  realtime client filter, an `email` comparison is ANDed with
  `emailVisibility = TRUE` on the leaf's table (AfterBuild, main query only,
  never inside the multi-match subquery). Admins, `@collection`/`@request`
  paths and server-evaluated access rules (`viewRule`, realtime
  `CanAccessRecord`, expand, `daos/record.go` l.567) carry no guard. Sorting by
  `email` is allowed for everyone and has no guard (PocketBase applies
  AfterBuild to filters only).
- **`emailVisibility`** resolves to the physical `email_visibility` column,
  `verified` to `verified`; **`username`** resolves only when the auth
  collection declares a `username` schema field, i.e. has the physical column
  (BUMA's `users`/`agent_users`); otherwise the filter/sort is a 400, never a
  500 on a missing column.
- **Realtime subscriptions** (`apis/realtime.go` l.555-590): the access rule
  is evaluated with hidden fields allowed (the call keeps its historical
  five-argument shape, so wrappers such as BUMA's test stub keep working); the
  client `filter` option is evaluated like a non-admin list filter (guard on
  `email`, unresolvable fields deny the event). The `@collection.`/`@request.`
  admin-only check runs on the subscription `filter` and `sort` options, only
  when a non-empty `filter` is present (l.567-574).
- **Realtime payload email**: PocketBase serializes each broadcast with
  `PublicExport` and ignores the email visibility only when the client's auth
  id is the record id **and** the `manageRule` check passes
  (`apis/realtime.go` l.439-447). PPBase sent the stored email to every
  subscriber; it now drops a hidden email for guests, other users and admins,
  and for the owner unless the manage rule allows it (nil: admins only, `""`:
  everyone). BUMA: `users.manageRule` is
  `@request.auth.role.permission.isAdmin = true`, so only a BUMA-admin owner
  receives its own hidden email over realtime. (Since v0.23 the owner, and
  managers, always receive it: v0.40.6 `apis/realtime.go` l.690-696.)
- **Expand**: PocketBase fetches expanded records with `expandFetch`
  (`apis/record_helpers.go` l.177-210): admins see everything, a nil target
  `viewRule` fails the relation, `""` allows every record and any other rule
  filters the related records (hidden fields allowed). PPBase expanded every
  related record; it now applies the same rule, and a relation with nothing
  visible has no `expand` key.
- **Auth booleans on create/update**: PocketBase loads `emailVisibility` and
  `verified` with `cast.ToBool` (`forms/record_upsert.go` l.380-384):
  `strconv.ParseBool` for strings, anything else false. PPBase used Python
  `bool()`, so the multipart/JSON string `"false"` published the email.

## Client impact for BUMA

The BUMA rules (194 non-empty rule strings, manage rules included, in the 114
definitions) and the `filter=` strings used by BUMA (`app/` and the frozen
source) reference none of the now-rejected fields, no table-qualified path,
no modifier on a system field and no bare `email` leaf, so existing calls are
unaffected. `users.listRule = ""` stays public for the resolvable fields; the
hidden columns, masked emails (filters, realtime payloads) and admin-only
related records (expand) become unreadable, matching PocketBase 0.22.34.

## Known differences

Measured against the PocketBase 0.22.34 binary by
`tests/pocketbase_parity/hidden_fields_probe.py` (`KNOWN_DIVERGENCES`):

- **Rule save validation**: PocketBase rejects a collection whose rule
  references a hidden field at save time (`forms/collection_upsert.go`
  l.372-391, `400 validation_invalid_rule`); PPBase accepts the save and only
  rejects the rule when it is evaluated. No BUMA rule references a hidden
  field, so this is latent.
- **`username` column**: PocketBase resolves `username` for any auth
  collection; PPBase only for the auth collections that declare it (see
  above). A filter/sort on `username` elsewhere is a 400 in PPBase and a
  normal comparison in PocketBase.

Not covered by this change (open, reported separately): the expand of the
auth routes (`auth-with-password`, `auth-refresh`, OAuth2) is still not
filtered by the target `viewRule` (PocketBase uses the authenticated record
as the requester, `apis/record_helpers.go` l.105-116); expanded auth records
do not get PocketBase's owner/manager email visibility and responses omit
`username`; relation-path sorts (`sort=friend.email`) are a 400 in PPBase.
