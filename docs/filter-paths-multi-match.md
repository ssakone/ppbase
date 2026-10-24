# Relation paths in filters and rules: LEFT JOIN and multi-match

PPBase evaluates the relation, back-relation and `@collection` paths of a
filter or of an API rule (`listRule`, `viewRule`, `createRule`, `updateRule`,
`deleteRule`) like PocketBase 0.22.34, the version of the BUMA backend. The
code is in `ppbase/services/filter_parser.py` (`_path_operand`,
`_path_comparison`, `render`).

## PocketBase 0.22.34

References are tags of the official repository
(`git show v0.22.34:<file>`).

- **LEFT JOINs on the main query.** Every path segment registers a LEFT JOIN
  and the query becomes `SELECT DISTINCT` (`resolvers/record_field_resolver.go`
  l.121-138, l.245-262). A single relation joins its target on
  `target.id = parent.field`, a multiple relation joins `json_each(field)` then
  the target, a back relation `x_via_f` joins the referencing records, and
  `@collection.x` joins every row of `x` without an ON condition
  (`resolvers/record_field_resolve_runner.go` l.144-182, l.515-713). The alias
  depends only on the path prefix, so every comparison of the filter that
  uses the same prefix reads the same joined row. A missing target (empty
  value, id of a deleted record, empty list, empty collection) is a row of
  NULLs; `rel.id` is the joined target id, so it is NULL for a missing target.
- **Comparisons.** `=`/`!=` follow `resolveEqualExpr`
  (`tools/search/filter.go` l.290-368): `!=` is `IS NOT` (NULL-safe), an empty
  operand (`''` or NULL) also matches NULL (`a = '' OR a IS NULL`), two
  per-row operands compare `COALESCE(a, '') = COALESCE(b, '')`. Other operators
  are plain SQL (NULL never matches).
- **Multi-match.** When the path goes through `@collection`, a multiple
  relation, a back relation whose field is multiple or has no single-column
  unique index (l.561-565, l.594-596), or `:each` on a multiple field, a
  comparison with an operator **without** `?` also requires that no row of a
  copy of the joins fails it (`tools/search/filter.go` l.183-215; subquery in
  `resolvers/multi_match_subquery.go` l.19-70; `NOT EXISTS` in
  `tools/search/filter.go` l.569-678). A row fails when `NOT (condition)`, and,
  for every operator except `=`, also when its value is NULL
  (`multiMatchAfterBuildFunc`, l.680-706). `?` operators are any-match.

Consequences, verified with the official binary:

- `@collection.users.id = @request.auth.id` is true only when every `users`
  row is the caller: never with two accounts or more. During a create, the
  record being created is already in the table (dry submit,
  `apis/record_crud.go` l.215-260), so it is never true for a create.
- `rels.name = "a"` needs every related record to be named `a` (an empty list
  matches nothing); `rels.name != "a"` also fails on an empty list or a
  missing target.
- `client.id = ''` is true when `client` is empty **or** points to a missing
  record.
- `@collection.empty.f != "x"` is false when the collection is empty.

## Differences with the current PocketBase (v0.40.6)

The client-visible behavior stays the 0.22.34 one. The current code differs
on three points:

- v0.24.0 removed the NULL clause of the multi-match (a NULL row no longer
  fails `!=`, `<`, `~`...; `@collection.empty.f != "x"` becomes true) and the
  create dry submit (v0.40.6 `tools/search/filter.go` l.700-738, changelog of
  v0.24.0);
- v0.23.0 reads `rel.id` of a single relation from the stored column instead
  of the joined target (v0.40.6 `core/record_field_resolver_runner.go`
  l.651-657), so an orphan id is no longer empty;
- v0.40.6 keeps a back relation without unique index on a plain join and
  enables the multi-match separately (l.546-596), with the same results.

## PPBase implementation

- The joins are registered once per path prefix (`_register_join`) as
  `LEFT JOIN "target" AS "__pbN" ON ...`; a multiple relation uses
  `LEFT JOIN LATERAL unnest(col) AS "__pbN"(value) ON TRUE` (one row per item,
  one NULL row for an empty list, like `json_each`).
- The condition tree keeps PocketBase's "one joined row" semantics: a join
  group distributes over `||`, and the `&&` operands that share a join stay
  in one group (same joined row), the others get their own. A group reading
  the current record is rendered `id IN (SELECT base.id FROM <table> AS base
  LEFT JOIN ... WHERE ...)`, the shape of PocketBase's `SELECT DISTINCT ...
  LEFT JOIN`, which PostgreSQL runs as a semi-join; a group without the
  current record (only `@collection` joins) is an uncorrelated `EXISTS`
  evaluated once.
- A list rule and the request filter are parsed as `(rule) && (filter)`, so
  they share their joins like PocketBase's single resolver.
- The multi-match is a `NOT EXISTS` over the same joins with their own
  aliases (`_many_vs_one_sql`, `_many_vs_many_sql`), with the 0.22.34 NULL
  clause (`_multi_match_failure`). When the path starts at the current
  record, it lists every record's rows and matches them on the record id,
  like PocketBase's `WHERE __mm_base.id = base.id`, so PostgreSQL can use an
  anti-join.
- Performance on synthetic tables (200,000 records, 1,000 targets, local
  PostgreSQL 16): `rel.name = ...` 7 ms before, 22 ms now; `rels.name ?=`
  7.9 s before, 0.2 s now; `rels.name =` 0.46 s (any-match before);
  single-record rule checks about 1 ms.
- `=`/`!=` on a path leaf follow `resolveEqualExpr` (`_pb_equal_sql`), with a
  text cast where PostgreSQL types would refuse `''`. JSON leaves keep
  PPBase's existing JSON comparison.
- The relation index records, for each back relation, whether its field has
  a single-column unique index (`_has_single_column_unique_index`, port of
  `dbutils.ParseIndex`, v0.22.34 `tools/dbutils/index.go` l.11-12,
  l.127-207).
- A records list whose `filter` or `sort` contains `@collection.` or
  `@request.` is refused with 403 "Only admins can filter by @collection." (or
  `@request.`) unless the caller is a superuser, before the request hooks
  (`checkForAdminOnlyRuleFields`, v0.22.34 `apis/record_helpers.go`
  l.319-345, `apis/record_crud.go` l.50-53).

## Behavior changes for existing PPBase collections

- `@collection` and multiple/back-relation paths with a non-`?` operator
  need every row to match. Rules written as `@collection.x.y = v` or
  `rels.id = @request.auth.id` deny more than before (as in PocketBase);
  write `?=` for "any row".
- A missing relation target is a NULL leaf: `rel.f = ""`, `rel.f != "x"`,
  `rel.id = ""` and `rel:length = 0` now match a record without (or with an
  orphan) relation.
- `rel.id` joins the target (orphan ids compare as NULL).
- Guests and auth records get 403 for `@collection.`/`@request.` in a list
  `filter`/`sort`.

## Known limits

- `@request.auth.<relation>.<field>` is bound from the request context
  (`auth` keys such as `role.permission.isAdmin`), not joined; PPBase alone
  binds a missing key as `''`. BUMA fills these keys from the live role
  (`app/auth/context.py` in the BUMA repository). `@request.data.<relation>.<field>`
  is not resolved.
- Realtime subscriptions do not apply the admin-only filter check
  (PocketBase: `apis/realtime.go` l.572).
- Filters can name columns that PocketBase does not resolve, such as the
  hidden `password_hash` and `token_key` of auth collections, and compare
  `email` without the `emailVisibility` condition PocketBase adds for
  non-admins (`resolvers/record_field_resolve_runner.go` l.366-375): PocketBase
  answers 400 for the first two and filters the third. This predates this
  change and stays open.
- A create denied by its rule answers `data: {"rule": {...}}`; PocketBase
  0.22.34 answers `data: {}`.

## Evidence

- `tests/pocketbase_parity/relation_multiplicity_probe.py --groups
  multimatch,buma_rules` sends the same requests to the official binary and
  to PPBase: 53 filters and the BUMA `users`/`buma_pdv` rules.
- `tests/integration/test_multi_match_rules.py` pins the 0.22.34 answers;
  `tests/integration/test_admin_only_filter_fields.py` the 403 check;
  `tests/integration/test_relation_maxselect_null.py` and
  `tests/integration/test_relation_delete_and_paths.py` the former strict
  `xfail` cases.
