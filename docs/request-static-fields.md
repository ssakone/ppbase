# @request.* static fields, list totals and the expand key

## Absent and present @request.* values (no 500)

PocketBase 0.22.34 resolves a `@request.*` filter/rule value with
`resolveStaticRequestField` (`resolvers/record_field_resolver.go` l.162-223):
an absent key (anonymous caller, missing query/header/data key) becomes the
SQL literal `NULL`, a present value a bound parameter (a bool, a number, a
string, or the JSON text of a list/object). `true`/`false` are the integer
identifiers `1`/`0` (`tools/search/filter.go` l.249-262), and comparisons go
through `resolveEqualExpr` (l.290-368) in SQLite, where two values without
column affinity compare by storage class: numbers (and Go booleans, bound as
1/0) numerically, text with text, and a number never equals a text.

PPBase bound an absent value as `''` and emitted `$n IS NOT NULL` and
`'' = TRUE`, which asyncpg/PostgreSQL reject (`IndeterminateDatatypeError`
"could not determine data type of parameter", `DataError` "a boolean is
required (got type str)"), closing the connection and failing the next
request. On the real backup this hit anonymous lists of
`buma_article_stockage` (`@request.auth.id != '' && @request.auth.id != null`),
`buma_pdv` and the other role-permission collections. A present non-boolean
value (`read: 1`, `isAdmin: "true"`, `@request.data.flag` sent as a JSON
string) compared with `true` still raised `:p = TRUE` errors.

PPBase now (`ppbase/services/filter_parser.py`):

- binds an **absent** `@request.*` value (auth, data, query, headers, any
  other `@request.` key) as SQL `NULL`, a present `''` as an empty string —
  so, like PocketBase, `<`, `<=`, `>`, `>=`, `~`, `!~` never match an absent
  value (including against an empty date), while `=`/`!=` treat it as empty;
- resolves `=`/`!=` involving a `@request.*` macro (`_static_equal_sql`):
  both operands empty → `TRUE`/`FALSE`; an empty operand versus a known
  value → `FALSE`/`TRUE`; an empty operand versus a column →
  `COALESCE(col::text,'') = ''`; **two values known while parsing** (a
  present request value versus a literal, `true`/`false` or another request
  value) → a `TRUE`/`FALSE` constant computed with SQLite's storage-class
  comparison (`1 = true`, `1.0 = true`, `"true" != true`, `"1" != 1`); a
  present value versus a column → a bound parameter;
- folds `<`/`<=`/`>`/`>=` between two known values the same way (`NULL` if
  either is absent, numbers before text).

No comparison of a known value ever binds it against a boolean or numeric
parameter type, so these never raise. The BUMA role-permission context
(`app/auth/context.py`, read-only) flattens the role JSON into dotted
`@request.auth.role.permission.*` keys; an undeclared key is absent, i.e.
NULL, exactly like PocketBase's `json_extract` of a missing key through the
`role` relation.

Paired with the 0.22.34 binary (0 differences) by
`tests/pocketbase_parity/request_static_rules_probe.py`: anonymous callers,
and authenticated callers with `read` = `true`, `1`, `0`, `"true"`, a missing
key, a role without permissions, no role, and `isAdmin` = `true`/`"true"`,
against `= true` and `!= true` (the PPBase side emulates BUMA's flattened
context). Absent values against dates are paired by
`tests/pocketbase_parity/empty_date_probe.py` (`request_filters`). Executed
against PostgreSQL by `tests/integration/test_request_static_rules.py`; SQL
pinned by `tests/test_filter_parser_request_static.py`.

### Remaining limits

- A present value compared with a **column** of another type keeps a typed
  parameter: e.g. a string `@request.auth.x` versus a number column, or a
  number versus a text column. SQLite would apply the column affinity;
  PostgreSQL may reject the parameter (500). No BUMA rule does this (every
  BUMA `@request.auth.*` value compared with a column is a record id).
- PocketBase normalizes multipart form values for `@request.data.*`
  (`"true"` → `true`, `"5"` → `5`, `tools/rest/multi_binder.go` l.150-179);
  PPBase keeps the strings, so a multipart `flag=true` against `= true` is
  false (400 for a create rule) where PocketBase allows it. No BUMA rule uses
  `@request.data.*`.

## totalPages of an empty list

PocketBase computes `totalPages = ceil(totalItems / perPage)`
(`tools/search/provider.go` l.254), so an empty result set has
`totalPages = 0`. PPBase clamped it to `1` for records, collections, admins
and logs; all four now return `0`. (The collections list still ignores
`filter` and the logs list supports only a few simple filter fields, so their
`totalItems` can differ from PocketBase's for a filtered request; both are
admin-only APIs that BUMA clients do not call. PPBase's own
`/api/migrations` list is not a PocketBase route and keeps its minimum of
one page.)

## totalItems counts distinct ids

PocketBase counts a records list with `COUNT(DISTINCT [[<table>.id]])`, the
ORDER BY reset and the same filter (v0.22.34 `tools/search/provider.go`
l.236-256, v0.40.6 l.298-327: `countQuery.Distinct(false).Select("COUNT(DISTINCT
[[" + countCol + "]])")`), and computes `totalPages` from that number. PPBase
counted rows (`COUNT(*)`); it now counts the distinct ids of a view, so
`totalItems`, `totalPages` (`ceil(totalItems / perPage)`) and `skipTotal`
(`-1`/`-1`, no count query) follow both PocketBase versions. A base or auth
table keeps `COUNT(*)`: `id` is its primary key and PPBase renders joins as
`EXISTS`, so no row is repeated and both counts are the same number
(`COUNT(DISTINCT id)` measured about 20 times slower than `COUNT(*)` for a
filtered count of 500,000 of 1,000,000 rows on PostgreSQL 16). A view may
repeat an id: its pages
still serve every row of the query, while `totalItems` counts the ids (NULL
ids are not counted, as in SQLite). On the real BUMA backup the public
`buma_article_stockage_list_view` serves 989 rows for 42 ids: `totalItems`
was 989 and `totalPages` 33 at `perPage` 30, PocketBase and now PPBase answer
42 and 2, and pages 3 to 33 still serve rows. It is the only one of the 42
BUMA views that repeats an id. Paired with the 0.22.34 binary by the `totals`
group of `tests/pocketbase_parity/empty_date_probe.py`; tested by
`tests/integration/test_list_total_items.py`.

The relation existence check of a create or update keeps PocketBase's
`count(*)` (v0.22.34 `forms/validators/record_data.go` l.387-394, v0.40.6
`core/field_relation.go` l.225-233): its ids are deduplicated first and only
a view may target a view, which cannot be written. The collections, admins
and logs lists count primary-keyed tables, where both counts are equal.

Open divergence: a filter or rule that joins another table (`@collection`, a
relation or back-relation path, or, for an authenticated request,
`@request.auth.<field>` other than the plain auth fields: v0.22.34
`resolvers/record_field_resolve_runner.go` l.184-211 and
`resolvers/record_field_resolver.go` l.26-36) makes PocketBase select
`DISTINCT` rows (`resolvers/record_field_resolver.go` l.121-138), so the
identical rows of a view collapse into one. PPBase renders these joins as
correlated `EXISTS` and keeps serving every row; `totalItems` is the same.
Client impact: none on the real BUMA backup, where no view has two identical
rows (42 views checked read-only); a view that later repeats identical rows
and is listed through such a filter or rule would serve more items on PPBase.
Pinned by `test_identical_view_rows_under_a_join_filter_are_not_collapsed`.

## Omitted `expand` key

PocketBase omits the `expand` key of a record that has no expanded relation
(`models/record.go` l.545-559, `daos/record_expand.go` l.227-229). PPBase
served `expand: {}`; `expand_records` now drops empty `expand` dicts
recursively, so a record without a linked (or viewable) record has no
`expand` key.
