import assert from 'node:assert/strict'
import test from 'node:test'

import {
  hasUnlimitedRelationMaxSelect,
  isMultiValueField,
  isRelationMultiple,
  parseRelationMaxSelectChoice,
  relationMaxSelectChoice,
} from '../admin-ui/src/lib/field-multiplicity.ts'
import { getSearchableFields } from '../admin-ui/src/lib/normalize-search-filter.ts'

const relation = (extra) => ({ name: 'rels', type: 'relation', collectionId: 'target', ...extra })

test('a PocketBase 0.22 relation with maxSelect null is multiple and unlimited', () => {
  for (const field of [
    relation({ maxSelect: null }),
    { name: 'rels', type: 'relation', options: { collectionId: 'target', maxSelect: null } },
  ]) {
    assert.equal(isRelationMultiple(field), true)
    assert.equal(isMultiValueField(field), true)
    assert.equal(hasUnlimitedRelationMaxSelect(field), true)
    assert.equal(relationMaxSelectChoice(field), 'null')
  }
})

test('current-format relations keep their single/multiple meaning', () => {
  assert.equal(isRelationMultiple(relation({})), false)
  assert.equal(isRelationMultiple(relation({ maxSelect: 0 })), false)
  assert.equal(isRelationMultiple(relation({ maxSelect: 1 })), false)
  assert.equal(isRelationMultiple(relation({ maxSelect: 2 })), true)
  assert.equal(isRelationMultiple(relation({ maxSelect: 999 })), true)
  assert.equal(relationMaxSelectChoice(relation({})), '1')
  assert.equal(relationMaxSelectChoice(relation({ maxSelect: 3 })), '3')
  assert.equal(hasUnlimitedRelationMaxSelect(relation({ maxSelect: 999 })), false)
})

test('nested options win over flat keys like the server merge', () => {
  const field = { name: 'rels', type: 'relation', maxSelect: 1, options: { maxSelect: null } }
  assert.equal(isRelationMultiple(field), true)
})

test('select and file keep integer semantics: null stays single', () => {
  assert.equal(isMultiValueField({ name: 's', type: 'select', maxSelect: null }), false)
  assert.equal(isMultiValueField({ name: 's', type: 'select', maxSelect: 2 }), true)
  assert.equal(isMultiValueField({ name: 'f', type: 'file', options: { maxSelect: 5 } }), true)
  assert.equal(isMultiValueField({ name: 't', type: 'text', maxSelect: null }), false)
})

test('the max select picker preserves null unless the user picks a number', () => {
  assert.equal(parseRelationMaxSelectChoice('null'), null)
  assert.equal(parseRelationMaxSelectChoice('1'), 1)
  assert.equal(parseRelationMaxSelectChoice('999'), 999)
})

test('plain-text search uses the array operator for maxSelect null relations', () => {
  const collection = {
    id: 'c',
    name: 'customers',
    type: 'base',
    fields: [
      { name: 'pdv', type: 'relation', maxSelect: null },
      { name: 'owner', type: 'relation', maxSelect: 1 },
      { name: 'tags', type: 'select', maxSelect: null },
    ],
  }
  const ops = Object.fromEntries(getSearchableFields(collection).map((f) => [f.name, f.op]))
  assert.equal(ops.pdv, '?~')
  assert.equal(ops.owner, '~')
  assert.equal(ops.tags, '~')
})
