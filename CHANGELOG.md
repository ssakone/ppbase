# Changelog

## Unreleased

### Relation multiplicity (PocketBase 0.22 `maxSelect: null`)

See `docs/relation-multiplicity.md` for the rule, the PocketBase references
and the known differences.

- A relation with `options.maxSelect: null` (PocketBase 0.22 export) is an
  unlimited multiple relation; the stored schema and the API keep `null`.
- New relation columns are `TEXT`/`TEXT[]` (no 15-character limit); existing
  `VARCHAR(15)`/`VARCHAR(15)[]` columns of the matching shape keep working.
- Relation values follow PocketBase `PrepareValue`; the record API rejects ids
  that do not exist (`validation_missing_rel_records`) before request hooks,
  together with the other field errors of the form.
- Deleting a record cleans its references like PocketBase `DeleteRecord`:
  ids are removed, cascade happens only when no id is left, an emptied
  required relation refuses the delete (HTTP 400).
- Switching a field between single and multiple converts the stored values
  instead of dropping the column.

### Behavior changes for existing collections

- Direct multi-value fields (`maxSelect > 1`) and multi-value path leaves
  compare their JSON text, like PocketBase: `tags ?= "a"` and
  `members ?= @request.auth.id` no longer match an element, `tags = "a"` no
  longer fails with an internal error.
- `:each`: `?` operators are any-match; other operators need a non-empty
  list. `:length` of a single select/file/relation is 0 or 1.
- List or object `@request.*` values are compared as their JSON text.
- Invalid filter identifiers or modifiers return HTTP 400.
- Startup and collection updates refuse a select/file/relation column whose
  shape contradicts its field (for example a `maxSelect: null` relation
  created as `VARCHAR(15)` by an earlier version) until
  `reconcile_multi_value_columns()` has converted it, typically from an
  application migration.
