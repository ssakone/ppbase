# Empty dates

PocketBase stores an empty `date` field as `''` in a
`TEXT DEFAULT '' NOT NULL` column (v0.22.34 `models/schema/schema_field.go`
l.163; v0.40.6 `core/field_date.go` l.109-110) and serves the zero
`types.DateTime` as `""` (v0.22.34 `tools/types/datetime.go` l.47-58).
PPBase stores it as `NULL` in a `TIMESTAMPTZ NULL` column. PPBase gives that
`NULL` the observable semantics of PocketBase's `''`:

- **Responses**: a `NULL` date (or `autodate`) is served `""`, never `null`
  (records, expand, auth responses, batch and realtime). A view record has no
  `created`/`updated` key when the view does not select them or selects an
  empty value (v0.22.34 `models/record.go` l.545-550).
- **Filters** (`tools/search/filter.go` l.155-176 and l.290-368): `=`/`!=`
  treat `''` and `NULL` alike; `<`, `<=`, `>`, `>=`, `~` and `!~` compare the
  stored text, where `''` comes before every date and `~` matches the
  `YYYY-MM-DD HH:MM:SS.mmmZ` text. The `null` identifier stays SQL `NULL` for
  the `<`-like operators.
- **Relation and `@collection` leaves** are LEFT JOINs: a missing target is
  SQL `NULL` in PocketBase too, so only the `NULL` of an existing row is an
  empty date (`other.td < "2024-06-01"` matches an empty target date, not a
  record without target). Inside a multi-match (comparison without `?` through
  `@collection`, a multiple or back relation) the negated condition gets
  PocketBase's exact TRUE/FALSE/NULL result.
- **Sort** (`tools/search/sort.go` l.23-37): `''` sorts first in ascending
  order and last in descending order (`NULLS FIRST`/`NULLS LAST`).

## Known differences

Measured by `tests/pocketbase_parity/empty_date_probe.py` against the official
PocketBase 0.22.34 binary (`KNOWN_DIVERGENCES` in the probe):

- A view column that is SQL `NULL` (e.g. `LEFT JOIN` without match) cannot be
  told from an empty date in PostgreSQL; SQLite keeps `NULL` and `''` apart.
  Only `<`, `!~` and the ascending sort of such a column differ.
- PocketBase 0.22 zeroes an unparseable date string (`cast.ToTime`) and
  stores `''`; PPBase rejects it with a 400 validation error.
