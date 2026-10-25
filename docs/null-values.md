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
and a stored JSON `null`) are served as before, and filters, sorts and
`totalItems` are untouched.

The tables PPBase creates for base and auth collections cannot hold `NULL`
in a served column outside `date` fields: every other field column, `id`,
`created`, `updated` and the auth columns are `NOT NULL` (fields with their
zero value as `DEFAULT`), including columns added to a collection that already
has records (`ppbase/db/schema_manager.py` `_col_ddl` and
`_AUTH_SYSTEM_COLUMN_DEFINITIONS`). The rule still applies to them, for
example to a column made nullable by hand.

## Not applied, and known differences

- `geoPoint` exists only in current PocketBase (v0.40.6
  `core/field_geo_point.go` l.114-118 serves `{"lon":0,"lat":0}` for `nil`).
  It is not a PocketBase 0.22 type, so a `NULL` geoPoint (only possible in a
  view) stays `null`. Unknown types stay `null` too (0.22.34 l.387-388 leaves
  the value unmodified). A view without a schema serves its raw columns.
- Filters and sorts on such a view column keep PostgreSQL's `NULL`
  semantics. Measured by `tests/pocketbase_parity/null_zero_values_probe.py`
  against the official PocketBase 0.22.34 binary (`KNOWN_DIVERGENCES`): for an
  unmatched `LEFT JOIN` row, PocketBase matches `txt = ""`, `rel = ""`,
  `tid = ""` and `meta = null`, and sorts the `NULL` first in ascending order
  and last in descending order; PPBase does not (dates excepted, see
  `empty-dates.md`). `num = 0` and `flag = false` agree (neither matches the
  `NULL`). This is an open filter/sort item, not part of the serialization
  rule.
- A stored JSON `null` of a base record is `jsonb 'null'` in PPBase and SQL
  `NULL` in PocketBase: same served `null`, different `= null`/`!= null`
  filters (BUMA D-0029).
