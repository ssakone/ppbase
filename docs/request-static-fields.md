# @request.* static fields, totalPages and the expand key

## Absent @request.* values (no 500)

PocketBase 0.22.34 resolves a `@request.*` filter/rule value with
`resolveStaticRequestField` (`resolvers/record_field_resolver.go` l.162-223):
an absent key (anonymous caller, or a role-permission key the flattened BUMA
context does not declare) becomes the SQL literal `NULL`, a present value a
typed parameter. Comparisons then go through `resolveEqualExpr`
(`tools/search/filter.go` l.290-368) over SQLite's loose typing, where
`'' = TRUE`/`'' = 1` is simply `false` and `col IS NOT NULL` works.

PPBase bound an absent value as `''` and emitted `$n IS NOT NULL` and
`'' = TRUE`, which asyncpg/PostgreSQL reject (`IndeterminateDatatypeError`
"could not determine data type of parameter", `DataError` "a boolean is
required (got type str)"), closing the connection and failing the next
request. On the real backup this hit anonymous lists of
`buma_article_stockage` (`@request.auth.id != '' && @request.auth.id != null`),
`buma_pdv` and the other role-permission collections.

PPBase now resolves `=`/`!=` comparisons that involve a `@request.*` macro
with PostgreSQL-safe, PocketBase-equivalent SQL
(`_static_equal_sql` in `ppbase/services/filter_parser.py`):

- both operands empty (absent/`''`/`null`) → `TRUE`/`FALSE` constant;
- an empty operand versus a known non-empty value → `FALSE`/`TRUE` (an empty
  value never equals a non-empty one, as in SQLite) — this removes the
  `'' = TRUE` cast error;
- an empty operand versus a column → `COALESCE(col::text,'') = ''` (matches
  the column's empty/NULL rows, like PocketBase);
- a present value binds as a typed parameter (`:p = TRUE` for a boolean).

So an anonymous list returns `200` with the authorized rows and the
connection stays usable. The BUMA role-permission context
(`app/auth/context.py`, read-only) flattens the role JSON into dotted
`@request.auth.role.permission.*` keys; an undeclared key is absent and folds
to `FALSE`, exactly like PocketBase resolving it to `NULL`.

Paired with the 0.22.34 binary (0 differences) by
`tests/pocketbase_parity/request_static_rules_probe.py`; executed against
PostgreSQL by `tests/integration/test_request_static_rules.py`.

## totalPages of an empty list

PocketBase computes `totalPages = ceil(totalItems / perPage)`
(`tools/search/provider.go` l.254), so an empty result set has
`totalPages = 0`. PPBase clamped it to `1`; it now returns `0`.

## Omitted `expand` key

PocketBase omits the `expand` key of a record that has no expanded relation
(`models/record.go` l.545-559, `daos/record_expand.go` l.227-229). PPBase
served `expand: {}`; `expand_records` now drops empty `expand` dicts
recursively, so a record without a linked record has no `expand` key.
