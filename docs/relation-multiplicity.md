# Relation multiplicity (`maxSelect`)

PPBase decides whether a `select`, `file` or `relation` field stores one
string or an array of strings in a single place:
`ppbase/models/field_multiplicity.py` (and its dashboard twin
`admin-ui/src/lib/field-multiplicity.ts`). Every layer uses it: DDL, record
validation, serialization, expand, filters, realtime, cascade delete, file
lookups, backups and the dashboard.

## The rule

| Stored field definition | Shape |
| --- | --- |
| `relation`, `options.maxSelect: null` (PocketBase 0.22 export) | multiple, no upper limit |
| any type, `maxSelect` numeric `n` | multiple iff `n > 1` (`0` and `1` are single) |
| `maxSelect` key absent | single |
| `select`/`file`, `maxSelect: null` | single |

Why, with references to the official PocketBase sources:

- PocketBase **v0.22.34** `models/schema/schema_field.go`:
  `RelationOptions.MaxSelect *int` (l.693, "If nil no limits are applied"),
  validated `NilOrNotEmpty` and `Min(minSelect)` (l.709, so `0` cannot be
  saved), `IsMultiple() = MaxSelect == nil || *MaxSelect > 1` (l.715-716).
  Multiple values use `JSON DEFAULT '[]' NOT NULL`, single ones
  `TEXT DEFAULT '' NOT NULL` (l.150-165). `SelectOptions`/`FileOptions` keep a
  plain `int` (l.603, l.649) whose `IsMultiple()` is `MaxSelect > 1`.
  `RelationOptions` marshals `maxSelect` without `omitempty`: definitions
  produced by PocketBase 0.22 always carry the key (`null` when unlimited).
- PocketBase **v0.40.6** (latest tag) `core/field_relation.go`: `MaxSelect int`
  (l.98), `IsMultiple() = MaxSelect > 1` (l.151-153), `JSON`/`TEXT` column
  (l.156-162). An absent key decodes as `0`, i.e. single.
- The **v0.22 → v0.23** upgrade (`migrations/1717233556_v0.23_migrate.go`
  l.844-848) turns a relation `maxSelect <= 0`, including `null`, into
  `2147483647`: PocketBase itself keeps `null` relations multiple.
- Current documentation (https://pocketbase.io/docs/collections/): "For single
  relation (the MaxSelect option is <= 1) the field value is a string … For
  multiple relation (the MaxSelect option is >= 2) the field value is an array".

The stored schema is never rewritten: `GET /api/collections` keeps returning
`maxSelect: null` (in `schema[].options` and in the flat `fields`), as
PocketBase 0.22 does, and sending those `fields` back keeps the relation
multiple.

An absent key stays single because PocketBase 0.22 never produces it, the
current PocketBase format reads it as `0`, and PPBase always treated it as
`1`: existing PPBase collections are unchanged.

## Physical columns

New relation columns are unbounded, like PocketBase's `TEXT`/`JSON`:

| Relation | Column | Default | Managed index |
| --- | --- | --- | --- |
| multiple | `TEXT[] NOT NULL` | `'{}'` | GIN |
| single | `TEXT NOT NULL` | `''` | btree `WHERE col != ''` |

Columns created by earlier PPBase versions (`VARCHAR(15)` /
`VARCHAR(15)[]`) are not rewritten: CRUD, filters, expand, modifiers and
backups keep working on them (covered by
`tests/integration/test_relation_maxselect_null.py`). Their only difference
is the 15-character limit: PostgreSQL refuses a longer id (for example two
ids joined by a comma) instead of truncating it. Widening them to `TEXT` is an
explicit, optional migration (`ALTER COLUMN … TYPE TEXT[]`/`TEXT`; dependent
views must be dropped and recreated by that migration).

## Writes

Values are normalized like PocketBase relation `PrepareValue` (v0.22.34
l.375-386, `list.ToUniqueStringSlice` in `tools/list/list.go` l.110-140):
ids are de-duplicated (first occurrence kept), empty strings dropped, a JSON
array string is decoded, a single relation keeps the **last** id and a
multiple relation keeps the ordered list (a plain string becomes a
one-element list). Modifiers `field+`, `+field` and `field-` use the same
list semantics.

The record API (create, update, batch) also requires every referenced id to
exist, like PocketBase's record form (`forms/validators/record_data.go`
`checkRelationValue`, l.361-398): `validation_missing_rel_records` /
"Failed to find all relation records with the provided ids." PocketBase runs
this form validation before the `OnRecordBefore*Request` hooks, so PPBase
checks the client payload (merged with the current record and its modifiers
on update) before running request hooks, after the superuser-only rule (403)
and the update visibility rule (404). On update the whole record is
validated, as in PocketBase, so an untouched orphan relation also fails.
Values written by a request hook, and every internal write
(`record_service.create_record`/`update_record`, the equivalent of
PocketBase `dao.SaveRecord`), are saved without a target lookup, so
historical orphans can be imported and kept. The check is available as
`record_service.relation_target_errors(engine, collection, data,
record_id=None)`.

## Collection updates

Updating a collection without changing multiplicity never touches the
column. Switching single ↔ multiple converts the stored values like
PocketBase `normalizeSingleVsMultipleFieldChanges` (v0.22.34
`daos/record_table_sync.go` l.166-291, v0.40.6
`core/collection_record_table_sync.go` l.155-298): `''` → `{}`, `'x'` →
`{x}`, a stored JSON array of plain strings → that array; multiple → single
keeps the last element. Earlier PPBase versions dropped and re-added the
column, losing the data.

`ppbase.db.schema_manager.reconcile_multi_value_columns(executor, collection,
field_names=None)` is an explicit repair for databases created before this
rule (for instance a `maxSelect: null` relation created as a scalar
`VARCHAR(15)` column). It converts only columns whose live shape contradicts
the rule, recreates their managed indexes, returns the converted field names
and is a no-op on a second run. It never runs automatically.

## Filters and rules

A direct reference to a multi-value field is compared through its JSON text,
exactly like PocketBase (`array_to_json(col)::text` renders the same compact
JSON as the stored PocketBase value): `rels = "id"` and `rels ?= "id"` never
match an array, `rels ~ "id"` is a substring match, `rels = '[]'` matches
empty lists. Traversals (`rels.name ?= …`, `rels.id ?= …`), back relations
(`x_via_rels.…`) and `:length` work on the array.

Known differences with PocketBase, unchanged by this rule and covered by a
strict `xfail` test: traversals and `:each` through a multiple relation use
any-match `EXISTS` semantics for non-`?` operators, while PocketBase applies a
match-all subquery (`rels.name = "a"` requires every related record to match).

## Backups

The backup canonical model expects the new `TEXT`/`TEXT[]` relation columns
and also accepts a live legacy `VARCHAR(15)`/`VARCHAR(15)[]` relation column
of the matching multiplicity. A scalar column for a multiple relation is
refused until it is reconciled.

## Dashboard

The record editor treats `maxSelect: null` relations as multiple; the
collection editor shows them as "Multiple (no max limit)" and keeps `null`
unless another value is picked.
