# Relation multiplicity (`maxSelect`)

PPBase decides whether a `select`, `file` or `relation` field stores one
string or an array of strings in a single place:
`ppbase/models/field_multiplicity.py` (and its dashboard twin
`admin-ui/src/lib/field-multiplicity.ts`). DDL, record validation, expand,
filters, cascade deletes, file lookups, backups and the dashboard use it.
Serialization and realtime need no rule of their own: they return the stored
value, which the column shape already makes a string or a list.

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

Two deliberate differences follow from storing one normalized shape for both
formats. Neither affects BUMA: the 191 relation fields of the BUMA export all
carry the key (188 with `1`, 3 with `null`).

- **Absent key → single.** PocketBase 0.22.34 would decode a hand-written 0.22
  definition without the key as a `nil` pointer (multiple), and the v0.23.0
  upgrade would give it `2147483647`. PPBase keeps it single because current
  PocketBase decodes an absent key as `0` and every existing PPBase collection
  relied on that.
- **Flat `maxSelect: null` → multiple.** Current PocketBase decodes `null` into
  its `int` as `0` (single). PPBase exposes the 0.22 `null` in the flat
  `fields` list too, so it must read it back as multiple.

## Physical columns

New relation columns are unbounded, like PocketBase's `TEXT`/`JSON`:

| Relation | Column | Default | Managed index |
| --- | --- | --- | --- |
| multiple | `TEXT[] NOT NULL` | `'{}'` | GIN |
| single | `TEXT NOT NULL` | `''` | btree `WHERE col != ''` |

Columns created by earlier PPBase versions with the *matching* shape
(`VARCHAR(15)` for a single relation, `VARCHAR(15)[]` for a multiple one) are
not rewritten: CRUD, filters, expand, modifiers and backups keep working on
them (covered by `tests/integration/test_relation_maxselect_null.py`). Their
only difference is the 15-character limit: PostgreSQL refuses a longer id (for
example two ids joined by a comma) instead of truncating it. Widening them to
`TEXT` is an explicit, optional migration (`ALTER COLUMN … TYPE TEXT[]`/`TEXT`;
dependent views must be dropped and recreated by that migration).

## Databases created before the rule

A database created by an earlier PPBase can hold a column whose *shape*
contradicts the rule. The case that matters is a PocketBase 0.22 relation
with `maxSelect: null` that older PPBase versions created as a scalar
`VARCHAR(15)` column with a btree index (BUMA's `buma_customer.pdv`,
`buma_pilotage_ticket.tag` and `buma_wallet.managers` in a database migrated
before this change). Serving such a table would be wrong in every layer:
reads would return a string where PocketBase returns a list, filters on the
field would fail (`array_to_json(character varying)` does not exist), API
writes would fail (a list cannot be stored in `character varying`) and any
collection update would fail while recreating the GIN index.

PPBase therefore fails explicitly:

- **startup** (`ppbase serve`, `pb.start()`, any app lifespan): after the
  application migrations have run, PPBase lists the mismatched columns with
  `find_multi_value_shape_mismatches()` and refuses to start with
  `MultiValueShapeMismatchError`, which names each column and
  `reconcile_multi_value_columns`;
- **collection update**: `update_collection_table()` raises the same error
  (HTTP 400 on `PATCH /api/collections/{id}`) instead of a raw SQL error.

The repair is explicit and must run before the database is served:
`ppbase.db.schema_manager.reconcile_multi_value_columns(executor, collection,
field_names=None)` converts only the columns whose live shape contradicts the
rule (`''` → `{}`, `'x'` → `{x}`, a stored JSON array of plain strings → that
array; an array column for a single field keeps its last element), recreates
their managed indexes, returns the converted field names and is a no-op on a
second run. Run it from an application migration, where
`await app.reconcile_multi_value_columns("<collection>", ["<field>"])` calls
it inside the migration transaction (a failing migration rolls the conversion
back): pending migrations run at startup before the check, or with
`python -m ppbase migrate up`. BUMA's migration `1789236850` does this for its
three relations.

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
"Failed to find all relation records with the provided ids." PocketBase
validates the whole form before the `OnRecordBefore*Request` hooks, so PPBase
checks the client payload (merged with the current record and its modifiers
on update) before running request hooks, after the superuser-only rule (403)
and the update visibility rule (404). On update the whole record is checked,
as in PocketBase, so an untouched orphan relation also fails.

When an id is missing, the 400 response lists what PocketBase's
`RecordDataValidator` reports together (v0.22.34 l.52-97): every relation
error (required and `maxSelect` are checked before the target lookup, like
`checkRelationValue`) and the errors of the other schema fields of the
submitted form. File fields (their uploads are processed later) and auth
system fields are not part of this merge. Requests whose relation ids all
exist continue exactly as before: hooks run, then the regular validation.

Values written by a request hook, and every internal write
(`record_service.create_record`/`update_record`, the equivalent of
PocketBase `dao.SaveRecord`), are saved without a target lookup, so
historical orphans can be imported and kept. The check is available as
`record_service.relation_target_errors(engine, collection, data,
record_id=None)`.

## Deletes

`record_service.delete_record()` follows PocketBase `Dao.DeleteRecord`
(v0.22.34 `daos/record.go` l.619-776, v0.40.6 `core/record_model.go`
l.1490-1646): the record is deleted first, then every relation field that
references its collection is cleaned up, collection by collection in name
order:

- the deleted id is removed from each referencing record (the stored value is
  re-prepared, so a single relation becomes `''`), the record's `updated` is
  refreshed and a realtime `update` is published;
- a referencing record is cascade-deleted only when its relation has
  `cascadeDelete` **and** no id is left (a multiple relation keeps the record
  while other ids remain);
- if a `required` relation would become empty, the whole delete fails with
  `RecordReferenceError` and is rolled back; the API answers 400 "Failed to
  delete record. Make sure that the record is not part of a required relation
  reference." with empty `data`, like PocketBase.

Reference cleanup runs when `all_collections` is passed (every caller does)
and, like PocketBase, inside one transaction (`run_record_storage_transaction`).
Earlier PPBase versions deleted the main record last, never removed the id
from non-cascading relations and cascade-deleted a multiple-relation record as
soon as it contained the id.

## Collection updates

Updating a collection without changing multiplicity never touches the
column. Switching single ↔ multiple converts the stored values like
PocketBase `normalizeSingleVsMultipleFieldChanges` (v0.22.34
`daos/record_table_sync.go` l.166-291, v0.40.6
`core/collection_record_table_sync.go` l.155-298): `''` → `{}`, `'x'` →
`{x}`, a stored JSON array of plain strings → that array; multiple → single
keeps the last element. Earlier PPBase versions dropped and re-added the
column, losing the data.

Limits of the scalar → array conversion, unreachable with relation ids:
elements of a JSON array are kept as stored (`'["", "a", "a"]'` gives
`{"", "a", "a"}`, where PocketBase hides the empty and duplicate items when it
reads the value), and a valid JSON array containing escaped or non-string
items is kept as one element (PocketBase keeps the array).

## Filters and rules

A **direct reference to a multi-value field** is compared through its JSON
text, exactly like PocketBase (`array_to_json(col)::text` renders the same
compact JSON as the stored PocketBase value): `rels = "id"` and
`rels ?= "id"` never match an array, `rels ~ "id"` is a substring match,
`rels = '[]'` matches empty lists. The **last segment of a path** (relation
traversal, back relation, `@collection`) is resolved the same way:
`customer.pdv = "id"` compares the JSON text, `customer.pdv:length` counts
its items (v0.22.34 `resolvers/record_field_resolve_runner.go` l.356-477,
v0.40.6 `core/record_field_resolver_runner.go` l.758-852).

Modifiers on select/file/relation values, direct or at the end of a path:

- `:length` counts the items; a single value counts as one item and `''` as
  zero (v0.22.34/v0.40.6 `tools/dbutils/json.go` `JsonArrayLength`);
- `:each` iterates the items (a single value is one item). With a `?`
  operator any item may match; with another operator a **multiple** field
  needs a non-empty list whose items all match. `= ""` also matches an empty
  list and `!=` is NULL-safe, following PocketBase's `json_each` LEFT JOIN and
  multi-match subquery (v0.22.34 `tools/search/filter.go` l.183-215,
  l.290-368, l.630-706).

`@request.*` values that are lists or objects (for example
`@request.data.tags`) are bound as their JSON text, like PocketBase's
`resolveStaticRequestField` (v0.22.34 `resolvers/record_field_resolver.go`
l.203-215): `@request.data.tags ?= "b"` does not match `["a","b"]`, while
`@request.data.tags = '["a","b"]'` does. `@request.data.<arrayable
field>:length` counts `list.ToUniqueStringSlice(value)` (l.233-241 of the
runner) and `@request.data.<field>:each` iterates that same list.

Invalid identifiers or modifiers in a filter now raise `ValueError` (HTTP
400) instead of an internal error.

## Behavior changes for existing PPBase collections

These changes apply to every PPBase collection, not only to PocketBase 0.22
imports; each one aligns PPBase with PocketBase (0.22.34 and v0.40.6 agree):

- direct multi-value fields (`maxSelect > 1`) compare their JSON text:
  `tags ?= "a"` and `members ?= @request.auth.id` no longer match an element
  (a rule written that way now denies access), `tags = "a"` returns no record
  instead of a 500;
- `:each` on an empty list no longer matches with non-`?` operators, and `?`
  operators are any-match instead of all-match;
- `:length` of a single select/file/relation is 0 or 1 instead of the string
  length;
- list or object `@request.*` values compare as JSON text;
- deleting a record removes its id from referencing relations, refuses to
  empty a required relation and cascade-deletes only empty relations;
- the record API rejects relation ids that do not exist;
- startup and collection updates refuse a column whose shape contradicts the
  rule until it is reconciled.

## Known differences with PocketBase

They predate this rule. The multi-match and the NULL leaf of a missing
relation in a path, formerly listed here, now follow PocketBase 0.22.34: see
`docs/filter-paths-multi-match.md`.

- **Envelope and messages.** PPBase answers validation errors with
  `{"status", "message", "data"}` and the message "Failed to create record.
  Validation failed for: rels, title."; PocketBase 0.22.34 uses the `code`
  key, "Failed to create record." for a non-admin create and "Something went
  wrong while processing your request." for an admin create and for updates.
  `data` (field names and codes) is identical, except the `maxSelect`
  overflow code (`validation_max_select_constraint` vs
  `validation_too_many_values`); `minSelect` is not enforced.
- The relation check runs before every request hook, so it also precedes a
  hook that reproduces another form check (BUMA's 15-character id check):
  with both errors, PPBase reports the relation and PocketBase the id.
- `+field` (prepend) is applied; PocketBase 0.22.34 ignores the key.
- Back-relation expand (`x_via_rels`) is not supported. (A record with
  nothing expanded no longer serves `expand: {}`: the key is omitted like in
  PocketBase, see `docs/request-static-fields.md`.)
- Sorting on a multi-value field uses PostgreSQL array order, not the JSON
  text order; JSON text uses `array_to_json`, which does not apply Go's HTML
  escaping of `<`, `>` and `&` (relation ids never contain them).
- (Fixed since: a missing `@request.*` key is now bound as NULL like in
  PocketBase, see `docs/request-static-fields.md`.)

## Backups

The backup canonical model expects the new `TEXT`/`TEXT[]` relation columns
and also accepts a live legacy `VARCHAR(15)`/`VARCHAR(15)[]` relation column
of the matching multiplicity. A scalar column for a multiple relation is
refused until it is reconciled.

## Dashboard

The record editor treats `maxSelect: null` relations as multiple; the
collection editor shows them as "Multiple (no max limit)" and keeps `null`
unless another value is picked. Limit: once another value is picked, the
"no max limit" choice disappears until the edit is discarded, and new
relations cannot be created with `null` (current PocketBase has no `null`).

## Evidence

`tests/pocketbase_parity/relation_multiplicity_probe.py` sends the same
synthetic requests to an official PocketBase 0.22 binary and to PPBase and
writes both results and their differences; the integration tests above use
its PocketBase 0.22.34 values.
