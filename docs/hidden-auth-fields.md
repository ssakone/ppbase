# Hidden auth fields in filters, sorts and rules

PocketBase 0.22.34 resolves a filter/sort/rule field only when it is a
base-model field (`id`, `created`, `updated`), a collection schema field or,
for an auth collection, one of `username`, `verified`, `emailVisibility`,
`email` (`resolvableSystemFieldNames`,
`resolvers/record_field_resolve_runner.go` l.718-732). Every other name —
`password`, `passwordHash`/`password_hash`, `tokenKey`/`token_key`,
`email_visibility` (snake), `lastResetSentAt`, … — is an "unknown field"
error for every caller, admin included (0.22.34 has no hidden-field flag;
the flag only gates the `email` guard, l.356-374).

PPBase stores these as physical columns (`password_hash`, `token_key`,
`email_visibility`). Before this change a filter resolved straight to the
column, so an anonymous caller could probe the bcrypt hash or token key
(`GET .../users/records?filter=password_hash~"$2a$12$A"`) or a masked email,
because `users.listRule` is `""`. PPBase now rejects those fields.

## Rules enforced

- **Filter / sort / collection rule** (`list_records`, `check_record_rule`):
  an unresolvable field raises and the API returns PocketBase's opaque
  `400 {"code"/"status":400,"message":"Something went wrong while processing
  your request.","data":{}}`. This covers the current collection, relation,
  back-relation and `@collection` leaves, and the `:length`/`:each`/`:lower`
  modifiers.
- **`email` visibility guard**: for a non-admin list request and for a
  realtime client filter (PocketBase `allowHiddenFields=false`), an `email`
  comparison is ANDed with `emailVisibility = TRUE` on the leaf's table
  (AfterBuild, applied to the main query only, never inside the multi-match
  subquery). Admins and server-evaluated access rules (`allowHiddenFields=
  true`, e.g. a `viewRule` or the realtime `CanAccessRecord`) carry no guard.
  Sorting by `email` is allowed for everyone and has no guard.
- **`emailVisibility`** resolves to the physical `email_visibility` column;
  `verified` and `username` resolve to their columns.
- **Realtime subscriptions** (`apis/realtime.go` l.555-590): the access rule
  is evaluated with hidden fields allowed; the client `filter` option is
  evaluated like a non-admin list filter (guard on `email`, hidden fields
  deny the event). The `@collection.`/`@request.` admin-only check runs on
  the subscription `filter` and `sort` options, but only when a non-empty
  `filter` is present (so `sort=@request.method` alone is not blocked).

## Client impact for BUMA

The 114 BUMA rules and the `filter=` strings used by BUMA (`app/` and the
frozen source) reference none of the now-rejected fields, so existing calls
are unaffected. `users.listRule = ""` stays public for the resolvable fields;
only the hidden columns and masked emails become unreadable, matching
PocketBase 0.22.34.

## Known differences

Measured against the PocketBase 0.22.34 binary by
`tests/pocketbase_parity/hidden_fields_probe.py`
(`KNOWN_DIVERGENCES`):

- **Rule save validation**: PocketBase rejects a collection whose rule
  references a hidden field at save time (`forms/collection_upsert.go`
  l.372-391, `400 validation_invalid_rule`); PPBase accepts the save and only
  rejects the rule when it is evaluated. No BUMA rule references a hidden
  field, so this is latent.
- **`username` column**: PocketBase resolves `username` for any auth
  collection. PPBase only has a physical `username` column on the auth
  collections that declare it (BUMA's `users`/`agent_users` via migration); a
  freshly-created PPBase auth collection without that column raises instead of
  resolving. BUMA's auth collections have the column, so BUMA is unaffected;
  the probe therefore does not exercise `username`.
