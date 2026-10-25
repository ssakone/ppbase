import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildRecordFormData,
  getEditableRecordFields,
  getPasswordMinimum,
  getRecordEditorFields,
} from '../admin-ui/src/lib/record-editor-fields.ts'
import {
  canDuplicateCollectionField,
  canRemoveCollectionField,
  duplicateCollectionField,
  getLockedFieldOption,
} from '../admin-ui/src/lib/collection-field-behavior.ts'

const authFields = [
  {
    name: 'id',
    type: 'text',
    required: true,
    primaryKey: true,
    autogeneratePattern: '[a-z0-9]{15}',
  },
  { name: 'password', type: 'password', required: true, min: 5 },
  { name: 'tokenKey', type: 'text', required: true, hidden: true },
  { name: 'email', type: 'email', required: true },
  { name: 'emailVisibility', type: 'bool' },
  { name: 'verified', type: 'bool' },
  { name: 'displayName', type: 'text' },
]

test('auth record fields keep API metadata but exclude internal form controls', () => {
  const collection = {
    id: 'users_id',
    name: 'users',
    type: 'auth',
    fields: authFields,
  }

  const fields = getRecordEditorFields(collection)
  assert.deepEqual(fields.map((field) => field.name), authFields.map((field) => field.name))

  const editable = getEditableRecordFields(collection, fields)
  assert.deepEqual(
    editable.map((field) => field.name),
    ['id', 'email', 'emailVisibility', 'verified', 'displayName'],
  )
  assert.equal(getPasswordMinimum(fields), 5)
})

test('auth duplication resets the id and never carries password or tokenKey', () => {
  const collection = {
    id: 'users_id',
    name: 'users',
    type: 'auth',
    fields: authFields,
  }
  const editable = getEditableRecordFields(collection, getRecordEditorFields(collection))
  const duplicate = buildRecordFormData(
    editable,
    {
      id: 'existingrecord1',
      email: 'user@example.com',
      emailVisibility: true,
      verified: true,
      displayName: 'User',
      password: 'must-not-copy',
      tokenKey: 'must-not-copy',
    },
    true,
  )

  assert.equal(duplicate.id, null)
  assert.equal(duplicate.email, 'user@example.com')
  assert.equal(duplicate.password, undefined)
  assert.equal(duplicate.tokenKey, undefined)
})

test('a duplicate leaves the file fields out, like PocketBase', () => {
  const fields = [
    { name: 'id', type: 'text', primaryKey: true },
    { name: 'title', type: 'text' },
    { name: 'doc', type: 'file', maxSelect: 1 },
    { name: 'docs', type: 'file', maxSelect: 4 },
  ]
  const source = { id: 'existingrecord1', title: 'T', doc: 'a.txt', docs: ['b.txt'] }

  assert.deepEqual(buildRecordFormData(fields, source, true), { id: null, title: 'T' })
  assert.deepEqual(buildRecordFormData(fields, source), source)
})

test('_superusers hides verified but keeps the public-email toggle', () => {
  const collection = {
    id: 'superusers_id',
    name: '_superusers',
    type: 'auth',
    fields: authFields,
  }
  const editable = getEditableRecordFields(collection, getRecordEditorFields(collection))

  assert.deepEqual(
    editable.map((field) => field.name),
    ['id', 'email', 'emailVisibility', 'displayName'],
  )
})

test('base collection fields remain unchanged', () => {
  const collection = {
    id: 'posts_id',
    name: 'posts',
    type: 'base',
    fields: [{ name: 'title', type: 'text', required: true }],
  }
  const fields = getRecordEditorFields(collection)
  const editable = getEditableRecordFields(collection, fields)

  assert.deepEqual(fields, collection.fields)
  assert.deepEqual(editable, collection.fields)
  assert.deepEqual(buildRecordFormData(editable), { title: null })
})

test('system auth fields follow PocketBase duplicate and remove actions', () => {
  const systemFields = authFields.map((field) => ({ ...field, system: true }))
  const id = systemFields[0]
  const password = systemFields[1]
  const tokenKey = systemFields[2]

  assert.equal(canRemoveCollectionField(id), false)
  assert.equal(canRemoveCollectionField(password), false)
  assert.equal(canRemoveCollectionField(tokenKey), false)
  assert.equal(canDuplicateCollectionField(id), true)
  assert.equal(canDuplicateCollectionField(tokenKey), true)
  assert.equal(canDuplicateCollectionField(password), false)

  const duplicated = duplicateCollectionField(systemFields, 2)
  assert.equal(duplicated[3].name, 'tokenKey_copy')
  assert.equal(duplicated[3].system, false)
  assert.equal(duplicated[3].primaryKey, false)
})

test('protected Required and Hidden options use PocketBase system rules', () => {
  const id = { name: 'id', type: 'text', system: true }
  const password = { name: 'password', type: 'password', system: true }
  const tokenKey = { name: 'tokenKey', type: 'text', system: true }
  const email = { name: 'email', type: 'email', system: true }
  const custom = { name: 'name', type: 'text' }

  assert.equal(getLockedFieldOption(id, 'auth', 'required'), true)
  assert.equal(getLockedFieldOption(id, 'auth', 'hidden'), false)
  assert.equal(getLockedFieldOption(password, 'auth', 'required'), true)
  assert.equal(getLockedFieldOption(password, 'auth', 'hidden'), true)
  assert.equal(getLockedFieldOption(tokenKey, 'auth', 'required'), true)
  assert.equal(getLockedFieldOption(tokenKey, 'auth', 'hidden'), true)
  assert.equal(getLockedFieldOption(email, 'auth', 'hidden'), false)
  assert.equal(getLockedFieldOption(email, 'auth', 'required'), undefined)
  assert.equal(getLockedFieldOption(custom, 'auth', 'required'), undefined)
})
