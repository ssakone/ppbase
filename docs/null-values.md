# NULL values in record responses

PocketBase reads a SQL `NULL` as `nil` (v0.22.34 `models/record.go`
l.48-58; v0.40.6 `core/record_model.go` l.491-500) and serves every schema
field through its type's `PrepareValue` (v0.22.34 `models/record.go`
l.328-359 and l.528-531, `models/schema/schema_field.go` l.304-390; v0.40.6
`core/record_model.go` l.496-499 and l.1274-1289). A view column that a
`LEFT JOIN` without match leaves `NULL` is therefore served as the zero value
of its field type, never `null`. PPBase does the same in
`build_record_response` (`ppbase/models/record.py`, `null_field_value`), so
every record response gets it: list, view, write responses, batch, expand
(from and into a view record), auth responses, realtime and
`enrich_records`.

| Field type | Served for `NULL` | PocketBase 0.22.34 / v0.40.6 |
| --- | --- | --- |
| `text`, `email`, `url`, `editor` | `""` | `cast.ToString(nil)`: l.310-311 / `core/field_text.go` l.174-176, `field_email.go` l.117-119, `field_url.go` l.117-119, `field_editor.go` l.121-123 |
| `number` | `0` | `cast.ToFloat64(nil)`: l.344-345 / `field_number.go` l.134-136 |
| `bool` | `false` | `cast.ToBool(nil)`: l.346-347 / `field_bool.go` l.104-106 |
| `date`, `autodate` | `""` | zero `DateTime` (see `empty-dates.md`) |
| single `select`, `file`, `relation` | `""` | `list.ToUniqueStringSlice(nil)` then the last element: l.351-386 / `field_select.go` l.156-167, `field_file.go` l.714-726, `field_relation.go` l.169-180 |
| multiple `select`, `file`, `relation` | `[]` | the same empty, non-nil slice (`tools/list/list.go` l.90-108; `field_file.go` l.764-777) |
| `json` | `null` | empty `JsonRaw` marshals as `null` (l.312-343, `tools/types/json_raw.go` l.26-32) / `field_json.go` l.121-150 |

Multiplicity is `field_is_multiple` (`relation-multiplicity.md`): a
PocketBase 0.22 relation with `maxSelect: null` is multiple and gets `[]`.
A `NULL` id is served `""` and a `NULL` auth `email`/`emailVisibility`/
`verified` `""`/`false`/`false` (same `cast` rules, v0.22.34 l.294-295,
l.346-354 and l.534-541).

Only `NULL` values change: stored values (including `""`, `0`, `false`, `[]`
and a stored JSON `null`) are served as before. Filters and sorts on `NULL`
follow PocketBase too (next section).

The tables PPBase creates for base and auth collections cannot hold `NULL`
in a served column outside `date` fields: every other field column, `id`,
`created`, `updated` and the auth columns are `NOT NULL` (fields with their
zero value as `DEFAULT`), including columns added to a collection that already
has records (`ppbase/db/schema_manager.py` `_col_ddl` and
`_AUTH_SYSTEM_COLUMN_DEFINITIONS`). The rule still applies to them, for
example to a column made nullable by hand.

## Filters and sorts on NULL values

PocketBase builds `=` and `!=` (and `?=`/`?!=`, the same case) with
`resolveEqualExpr` (v0.22.34 `tools/search/filter.go` l.150-154 and
l.284-368) and runs them on SQLite. v0.40.6 (l.189-192 and l.334-422) is the
same for every 0.22 operand: `NoCoalesce` became
`NullFallback: NullFallbackDisabled`, and the new `NullFallbackEnforced` only
serves filter functions 0.22 does not have (`tools/search/token_functions.go`
l.109 and l.145), so PPBase applies it by default. PPBase
(`ppbase/services/filter_parser.py`: `_direct_equal_sql`, `_pb_equal_sql`,
`_folded_equal_sql`, `_json_empty_sql`, `_json_like_text_sql`) builds the
same result in PostgreSQL:

| Filter | PocketBase SQL | Matches a `NULL` |
| --- | --- | --- |
| `x = ""`, `x = null`, `"" = x`, `null = x` | `(x = '' OR x IS NULL)` | yes (a stored `''` too) |
| `x != ""`, `x != null` | `(x IS NOT '' AND x IS NOT NULL)` | no |
| `x = "v"` (non-empty value, `true`, `false`, numbers) | `x = 'v'` | no |
| `x != "v"` | `x IS NOT 'v'` (NULL-safe) | yes |
| `x = y`, `x != y` (two columns) | `COALESCE(x, '') = COALESCE(y, '')` | as `''` |
| no column: `1 = null`, `"" != null`, `1 = "1"`, `true = 1` | `'' = 1.0`, `'' IS NOT ''`, `1.0 = '1'`, `1 = 1.0` | folded: false, false, false, true |
| json `x = null` / `x != null` | `JSON_EXTRACT(x) IS [NOT] NULL` | yes / no |
| json `x = ""` / `x != ""` | `JSON_EXTRACT(x) IS [NOT] ''` | no / yes |
| json `x != v` (`v` non-empty) | `JSON_EXTRACT(x) IS NOT v` | yes |
| json `x = y` / `x != y` (`y` a column) | `JSON_EXTRACT(x) IS [NOT] y` | equals another `NULL` |
| json `x ~ v` / `x !~ v` | `JSON_EXTRACT(x) [NOT] LIKE v` | no |

- The same rules apply to every column type (text, email, url, editor,
  number, bool, select, file, relation, a multi-value field through its JSON
  text, an auth record column read through a view `LEFT JOIN`), to relation
  and `@collection` paths (already `_pb_equal_sql`) and to `@request.*`
  values. Dates keep their own rules (`empty-dates.md`).
- A stored `''` is an empty operand: `txt = null` is `(txt = '' OR txt IS
  NULL)`, so it now matches a stored `''` in a base or auth collection
  (before WS-A11: `txt IS NULL`, never true on a `NOT NULL` column). This is
  visible in API rules.
- A current-record `?=`, `?!=`, `?>`... is the plain operator: PocketBase
  shares one case per operator pair (filter.go l.150-176) and only adds a
  multi-match subquery for relation/`@collection` paths (l.183-215); a
  current-record field, a literal and a `@request.*` value never multi-match
  (v0.22.34 `resolvers/record_field_resolve_runner.go` l.454-476,
  `resolvers/record_field_resolver.go` l.162-225). Json operands, `:each`
  and fields without metadata keep PPBase's array-any reading. Visible in
  API rules: for an anonymous caller `@request.auth.id` is `NULL`, so
  `@request.auth.id ?= owner` lists the records without owner, like
  PocketBase (none before WS-A11).
- A comparison without any column (literals, `null`, `@request.*` values)
  is folded to `TRUE`/`FALSE` while parsing, like SQLite compares the
  bound values without affinity: a number is a REAL, `true`/`false` are
  `1`/`0`, a text stays a text (filter.go l.226-281 and l.318-338). It was
  a PostgreSQL type error (`1 = ''`, `TRUE = 1`). A `@now` value is only
  folded against an empty operand.
- Two non-NULL columns keep PostgreSQL's typed comparison: only the `NULL`
  cases follow `COALESCE(x, '')`.
- Json: SQLite extracts a JSON `null` as SQL `NULL` (`tools/dbutils/json.go`
  l.30-47), so a SQL `NULL` and a stored JSON `null` (`jsonb 'null'` in
  PPBase) are both `null`, for a view, a base collection (the filter part of
  BUMA D-0029) and json paths (`meta.k = null` matches `{"k": null}`). This
  also holds for `~`/`!~`/`?~`/`?!~` (a JSON `null` never matches) and for a
  json leaf of a multi-match subquery, whose `multiMatchValue IS NULL`
  clause (filter.go l.680-706) rejects a related JSON `null`
  (`rels.meta != ""` fails a record with a related JSON `null`). A json
  operand compared with a column is NULL-safe (`IS` / `IS NOT`, compared as
  jsonb), and `json ?!= v` (PPBase's array-any reading) keeps a SQL `NULL`.
- `<`, `<=`, `>`, `>=`, `~`, `!~` are otherwise unchanged: a `NULL` never
  matches, as in SQLite.
- `totalItems` counts with the same `WHERE` clause.

Sort: PocketBase sorts `ORDER BY <identifier> ASC|DESC`
(`tools/search/sort.go` l.23-37; v0.40.6 l.26-45) and SQLite treats `NULL`
as smaller than any value, first in ascending and last in descending order
(https://www.sqlite.org/lang_select.html#orderby); PostgreSQL does the
opposite by default (https://www.postgresql.org/docs/current/queries-order.html).
PPBase (`record_service._sort_term`) adds `NULLS FIRST`/`NULLS LAST` to every
column of a view, to every date and to every json value, which it sorts as
`NULLIF(to_jsonb(col), 'null'::jsonb)` (PocketBase sorts
`JSON_EXTRACT(col, '$')`, `NULL` for a JSON `null`, v0.22.34
`resolvers/record_field_resolve_runner.go` l.465-474). The other base and
auth columns are `NOT NULL` and keep a plain, index-friendly term. Non-`NULL`
values keep their order.

Measured by `tests/pocketbase_parity/null_zero_values_probe.py` against the
official PocketBase 0.22.34 binary. Its `null` section holds 413 results:
236 filters and 30 sorts of a `LEFT JOIN` view (every 0.22 field type,
every operator, both operand orders and sort directions), 9 base filters,
18 relation and `@collection` paths into the view, 9 filters and 4 sorts of
an auth `LEFT JOIN` view, 26 comparisons without a column, 9 json `~`/`!~`,
13 json comparisons with a value, 20 json multi-match paths, 18 filters and
4 sorts of computed json view columns, and 17 list rules (anonymous and
owner). The PocketBase results are pinned in
`tests/integration/test_null_zero_values.py`.

## Not applied, and known differences

- `geoPoint` exists only in current PocketBase (v0.40.6
  `core/field_geo_point.go` l.114-118 serves `{"lon":0,"lat":0}` for `nil`).
  It is not a PocketBase 0.22 type, so a `NULL` geoPoint (only possible in a
  view) stays `null`. Unknown types stay `null` too (0.22.34 l.387-388 leaves
  the value unmodified). A view without a schema serves its raw columns.
- A multi-value field (select, file, relation) is sorted by its array in
  PPBase and by its stored JSON text in PocketBase (`'[]'` after `'["x"]'`):
  the order of non-`NULL` values differs, the `NULL` row is placed like
  PocketBase (probe `KNOWN_DIVERGENCES`; open, not a `NULL` case).
- In a view, the empty date of a matched row is `NULL` in PPBase like the
  `NULL` of a missing row, so both tie in a sort where SQLite puts `NULL`
  before `''` (`empty-dates.md`).
- Json comparisons with a non-null value keep PPBase's jsonb reading,
  where PocketBase compares `JSON_EXTRACT`'s SQLite value. On a `jsonb`
  column PostgreSQL rejects `meta = 5`, `meta = "x"`, `meta = true`,
  `meta > 1` and `meta.k = 1` (HTTP 500, unchanged by WS-A11), and
  `?=`/`?!=`/`?!~` read a JSON array element by element (`meta ?= "x"`
  matches `["nu", "x"]`, PocketBase the JSON string `"x"`). Only their `NULL`
  rows follow PocketBase (`!=` keeps them). Non-null json values keep the
  jsonb sort order. Open, not a `NULL` case (13 probe expressions in
  `KNOWN_DIVERGENCES`).
- A stored JSON `null` of a base record is `jsonb 'null'` in PPBase and SQL
  `NULL` in PocketBase: same served `null` and, since WS-A11, the same
  `= null`/`!= null` filters and sort (BUMA D-0029).
