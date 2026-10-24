# Expand applies the related `viewRule`

PocketBase never returns, through `expand`, a record that the caller could
not view directly. PPBase used to expand any related record without checking
a rule: an anonymous caller could read protected records through a public
collection (in the BUMA schema, 49 relations go from a collection an anonymous
caller can read to a collection whose `viewRule` is not public, for example
`buma_sale.customer` → `buma_customer`, `buma_customer_pdv.pdv` → `buma_pdv`,
`buma_payment.caisse_transaction` → `buma_caisse_transactions`,
`users.employee` → `buma_employee`, `buma_buy.supplier` → `buma_supplier`).

`ppbase/services/expand_service.py` now follows PocketBase 0.22.34.

## PocketBase 0.22.34 semantics

Sources: `git show v0.22.34:<file>` in the official repository.

- **Entry points.** `apis.EnrichRecords` (`apis/record_helpers.go`
  l.149-174) is used by list (`apis/record_crud.go` l.99), view (l.186),
  create (l.289) and update (l.381); `RecordAuthResponse` (l.72-139) by
  auth-with-password, auth-with-oauth2 and auth-refresh; realtime builds its
  own request info (`apis/realtime.go` l.410-437). Expand failures are logged
  and never fail the request.
- **Fetch with the rule** (`expandFetch`, l.176-210): an admin receives every
  related record; if the related collection's `viewRule` is `null` the fetch
  fails (`only admins can view collection %q records`); `""` is public; any
  other rule is added as a filter, built by
  `resolvers.NewRecordFieldResolver(dao, relCollection, requestInfo, true)`:
  the request info of the current request (`@request.auth`, `@request.data`,
  `@request.query`, headers, method, context) and hidden fields allowed. The
  rule check runs before the query even when there is no id to load
  (`Dao.FindRecordsByIds` applies its filters first, `daos/record.go`
  l.167-196).
- **Request info per entry point.** HTTP: the request (`Context` "default",
  method, body as `@request.data`). Auth responses: a copy whose `Admin` is
  nil and whose `AuthRecord` is the authenticated record (l.105-116).
  Realtime: `Context` "realtime", method GET, the subscription `options`
  query and headers, the subscriber's admin or record (`apis/realtime.go`
  l.410-418).
- **Paths** (`Dao.ExpandRecords`, `daos/record_expand.go` l.37-55;
  `normalizeExpands` l.269-302): spaces removed, dots trimmed, empty paths
  dropped, a path contained in a longer one merged into it
  (`customer,customer.pdv` → `customer.pdv`), duplicates removed. Each path is
  expanded independently; a failing path is collected and skipped, the
  others still apply.
- **One level** (`expandRecords`, l.66-267): the relation field must exist
  and be a relation (else the path fails); `<collection>_via_<field>` (and the
  deprecated `collection(field)`) is a back relation: the ids of the records
  of `<collection>` whose `<field>` targets the parent, `LIMIT 1000`, no rule
  on that lookup, a list unless `<field>` has a single-column unique index
  (l.102-170). All ids of the level are fetched at once; the **nested levels
  are expanded before this level is assigned**, so a failing nested level
  (for example an admin-only collection under `customer`) drops the whole
  path, parent level included (l.203-209). Each record then receives its
  related records in field order, refused or missing ids skipped, no key
  when none is left (l.217-229), a single object when `maxSelect` is a number
  `<= 1`, else a list (l.257-261); expands of an earlier path are merged by
  id (`MergeExpand`, `models/record.go` l.176-249). `MaxExpandDepth` is 6.
- **Emails of expanded auth records** (`autoIgnoreAuthRecordsEmailVisibility`,
  l.212-279, called for every fetched level, l.204-206): the email is shown
  when `emailVisibility` is true, to an admin, to the record's owner
  (`requestInfo.AuthRecord.Id`) and to a caller for whom the collection
  `manageRule` matches the record (evaluated with hidden fields allowed;
  errors are ignored). Otherwise the `email` key is omitted.

The current version (v0.40.6, `apis/record_helpers.go` l.355-508,
`core/record_query_expand.go`) keeps this structure. Differences, not applied
because BUMA clients see 0.22.34: the request info of the expand gets
`Context = "expand"` (l.376-380), superusers replace admins, the
`OnRecordEnrich` hooks run on expanded records and a superuser also sees
hidden fields (`autoResolveRecordsFlags`).

## PPBase implementation

- `expand_records(engine, collection, records, expand, all_collections,
  request_context=None)`. With a `request_context` (all PPBase API callers)
  the semantics above apply; the admin test is the records API one (`type`
  admin, or a `_superusers` record). Without one, the server-side
  `Dao.ExpandRecords(..., nil)` semantics are kept: every related record,
  for hooks.
- The rule is compiled by `filter_parser.parse_filter` with the exact
  `request_context` of the list/view (`allow_hidden=True`), so host
  extensions of that context (BUMA's flattened `role.permission.*`) apply to
  expand too. One query per path and level (plus one for a `manageRule`),
  never one per record; the collections already loaded are reused for the
  relation metadata.
- Inside a write transaction (create/update expands on the pinned
  connection) every expand query runs in a savepoint: a failing rule only
  fails its path and never aborts the write.
- The back-relation lookup is one window query for all parents; each
  parent's ids are ordered by `ctid` (PPBase's `@rowid`); a view keeps
  PostgreSQL's order. PocketBase uses SQLite's scan (rowid) order; an
  updated PostgreSQL row moves in `ctid` order. No BUMA client uses `_via_`.
- Auth responses (auth-with-password/otp/oauth2, auth-refresh, impersonate)
  expand as the authenticated record (`_auth_response_expand_context`);
  `pb.apis.enrich_records` uses the records API context and
  `pb.apis.record_auth_response` the authenticated record.

## Evidence

- `tests/integration/test_expand_view_rules.py` (PostgreSQL): anonymous,
  user without and with the permission, `manageRule` match and admin; single,
  multiple (partially authorized, orphan id skipped), nested admin-only
  level, unknown nested field, `_via_` back relations, emails, `@request.data`
  on create, `@request.query`, a BUMA-like flattened role context, update,
  auth-with-password/auth-refresh, realtime and the one-query-per-level
  count.
- `tests/pocketbase_parity/expand_rules_probe.py` against the 0.22.34
  binary: 105 expand checks (list/view for seven callers), 9 create/update/
  delete, 4 auth responses and 4 realtime events identical; the only known
  difference of the expand groups is the open item below.

## Open

- The **listed or viewed** auth records themselves (not the expanded ones)
  do not get the `manageRule` email visibility of
  `autoIgnoreAuthRecordsEmailVisibility` (`EnrichRecords` applies it to the
  main records too, l.156): a caller matching `manageRule` sees PocketBase
  0.22.34 emails that PPBase hides (probe group `expand.main_auth_list`).
- `@request.auth.<field>` resolves from the token claims and the host
  context only (`api/deps.py` loads no auth record), in expand as in every
  rule; PocketBase reads the auth record and its relations. BUMA provides the
  values it needs through its live auth context.
