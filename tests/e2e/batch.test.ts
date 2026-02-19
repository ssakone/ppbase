/**
 * PocketBase SDK E2E Tests: Batch API
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import type PocketBase from 'pocketbase'
import { createTestCollection, getAdminPb } from './helpers'
import { existsSync } from 'node:fs'
import { join } from 'node:path'

function randomRecordId(): string {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789'
  let result = ''
  for (let i = 0; i < 15; i++) {
    result += chars[Math.floor(Math.random() * chars.length)]
  }
  return result
}

describe('Batch API', () => {
  let adminPb: PocketBase
  let collection: any
  let cleanup: () => Promise<void>

  beforeAll(async () => {
    adminPb = await getAdminPb()
    const created = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'count', type: 'number', required: false },
      ],
    })
    collection = created.collection
    cleanup = created.cleanup
  })

  afterAll(async () => {
    await cleanup()
  })

  it('should execute create, update, upsert and delete in a single batch transaction', async () => {
    const firstId = randomRecordId()
    const secondId = randomRecordId()
    const upsertId = randomRecordId()

    const result = await adminPb.send('/api/batch', {
      method: 'POST',
      body: {
        requests: [
          {
            method: 'POST',
            url: `/api/collections/${collection.name}/records`,
            body: { id: firstId, title: 'first', count: 1 },
          },
          {
            method: 'POST',
            url: `/api/collections/${collection.name}/records`,
            body: { id: secondId, title: 'second', count: 2 },
          },
          {
            method: 'PATCH',
            url: `/api/collections/${collection.name}/records/${firstId}`,
            body: { title: 'first-updated', count: 11 },
          },
          {
            method: 'PUT',
            url: `/api/collections/${collection.name}/records`,
            body: { id: upsertId, title: 'upsert-created', count: 3 },
          },
          {
            method: 'DELETE',
            url: `/api/collections/${collection.name}/records/${secondId}`,
          },
        ],
      },
    })

    expect(Array.isArray(result)).toBe(true)
    expect(result).toHaveLength(5)
    expect(result[0].status).toBe(200)
    expect(result[1].status).toBe(200)
    expect(result[2].status).toBe(200)
    expect(result[3].status).toBe(200)
    expect(result[4].status).toBe(204)

    const first = await adminPb.collection(collection.name).getOne(firstId)
    expect(first.title).toBe('first-updated')
    expect(first.count).toBe(11)

    await expect(
      adminPb.collection(collection.name).getOne(secondId),
    ).rejects.toThrow()

    const upsert = await adminPb.collection(collection.name).getOne(upsertId)
    expect(upsert.title).toBe('upsert-created')
    expect(upsert.count).toBe(3)
  })

  it('should reject batch requests when batch setting is disabled', async () => {
    const settings = await adminPb.settings.getAll()
    const previousBatch = { ...(settings.batch || {}) }

    try {
      await adminPb.settings.update({
        batch: {
          ...previousBatch,
          enabled: false,
        },
      })

      await expect(
        adminPb.send('/api/batch', {
          method: 'POST',
          body: {
            requests: [
              {
                method: 'POST',
                url: `/api/collections/${collection.name}/records`,
                body: { id: randomRecordId(), title: 'disabled-batch' },
              },
            ],
          },
        }),
      ).rejects.toThrow(/Batch requests are not allowed/i)
    } finally {
      await adminPb.settings.update({
        batch: previousBatch,
      })
    }
  })

  it('should enforce batch maxRequests setting', async () => {
    const settings = await adminPb.settings.getAll()
    const previousBatch = { ...(settings.batch || {}) }

    try {
      await adminPb.settings.update({
        batch: {
          ...previousBatch,
          enabled: true,
          maxRequests: 1,
        },
      })

      await expect(
        adminPb.send('/api/batch', {
          method: 'POST',
          body: {
            requests: [
              {
                method: 'POST',
                url: `/api/collections/${collection.name}/records`,
                body: { id: randomRecordId(), title: 'first' },
              },
              {
                method: 'POST',
                url: `/api/collections/${collection.name}/records`,
                body: { id: randomRecordId(), title: 'second' },
              },
            ],
          },
        }),
      ).rejects.toThrow(/allowed max number of batch requests is 1/i)
    } finally {
      await adminPb.settings.update({
        batch: previousBatch,
      })
    }
  })

  it('should enforce batch maxBodySize setting', async () => {
    const settings = await adminPb.settings.getAll()
    const previousBatch = { ...(settings.batch || {}) }

    try {
      await adminPb.settings.update({
        batch: {
          ...previousBatch,
          enabled: true,
          maxBodySize: 80,
        },
      })

      await expect(
        adminPb.send('/api/batch', {
          method: 'POST',
          body: {
            requests: [
              {
                method: 'POST',
                url: `/api/collections/${collection.name}/records`,
                body: {
                  id: randomRecordId(),
                  title: 'this-title-is-long-enough-to-force-a-larger-json-payload',
                },
              },
            ],
          },
        }),
      ).rejects.toThrow(/max batch request body size/i)
    } finally {
      await adminPb.settings.update({
        batch: previousBatch,
      })
    }
  })

  it('should rollback and fail when batch processing exceeds timeout setting', async () => {
    const settings = await adminPb.settings.getAll()
    const previousBatch = { ...(settings.batch || {}) }

    try {
      await adminPb.settings.update({
        batch: {
          ...previousBatch,
          enabled: true,
          timeout: 0.001,
          maxRequests: 200,
        },
      })

      const requestItems = Array.from({ length: 180 }, () => ({
        method: 'POST',
        url: `/api/collections/${collection.name}/records`,
        body: {
          id: randomRecordId(),
          title: 'timeout-check',
        },
      }))

      await expect(
        adminPb.send('/api/batch', {
          method: 'POST',
          body: {
            requests: requestItems,
          },
        }),
      ).rejects.toThrow(/Batch transaction failed/i)
    } finally {
      await adminPb.settings.update({
        batch: previousBatch,
      })
    }
  })

  it('should rollback all operations when one nested request fails', async () => {
    const rollbackId = randomRecordId()

    await expect(
      adminPb.send('/api/batch', {
        method: 'POST',
        body: {
          requests: [
            {
              method: 'POST',
              url: `/api/collections/${collection.name}/records`,
              body: { id: rollbackId, title: 'rollback-me' },
            },
            {
              method: 'POST',
              url: `/api/collections/${collection.name}/records`,
              body: { id: randomRecordId() },
            },
          ],
        },
      }),
    ).rejects.toThrow(/Batch transaction failed/i)

    await expect(
      adminPb.collection(collection.name).getOne(rollbackId),
    ).rejects.toThrow()
  })

  it('should apply per-request query options like fields in nested requests', async () => {
    const recordId = randomRecordId()

    const result = await adminPb.send('/api/batch', {
      method: 'POST',
      body: {
        requests: [
          {
            method: 'POST',
            url: `/api/collections/${collection.name}/records?fields=id,title`,
            body: { id: recordId, title: 'field-test', count: 100 },
          },
          {
            method: 'PATCH',
            url: `/api/collections/${collection.name}/records/${recordId}?fields=id,count`,
            body: { count: 101 },
          },
        ],
      },
    })

    expect(result).toHaveLength(2)
    expect(result[0].status).toBe(200)
    expect(result[0].body.id).toBe(recordId)
    expect(result[0].body.title).toBe('field-test')
    expect(result[0].body.count).toBeUndefined()

    expect(result[1].status).toBe(200)
    expect(result[1].body.id).toBe(recordId)
    expect(result[1].body.count).toBe(101)
    expect(result[1].body.title).toBeUndefined()

    const record = await adminPb.collection(collection.name).getOne(recordId)
    expect(record.title).toBe('field-test')
    expect(record.count).toBe(101)
  })

  it('should support multipart batch body with file keys in both dot and bracket notation', async () => {
    const { collection: filesCollection, cleanup: cleanupFiles } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'document', type: 'file', required: false, options: { maxSelect: 1 } },
      ],
    })

    try {
      const firstId = randomRecordId()
      const secondId = randomRecordId()

      const form = new FormData()
      form.append('@jsonPayload', JSON.stringify({
        requests: [
          {
            method: 'POST',
            url: `/api/collections/${filesCollection.name}/records`,
            body: { id: firstId, title: 'first-file' },
          },
          {
            method: 'POST',
            url: `/api/collections/${filesCollection.name}/records`,
            body: { id: secondId, title: 'second-file' },
          },
        ],
      }))
      form.append(
        'requests.0.document',
        new Blob(['alpha-content'], { type: 'text/plain' }),
        'alpha.txt',
      )
      form.append(
        'requests[1].document',
        new Blob(['beta-content'], { type: 'text/plain' }),
        'beta.txt',
      )

      const result = await adminPb.send('/api/batch', {
        method: 'POST',
        body: form,
      })

      expect(result).toHaveLength(2)
      expect(result[0].status).toBe(200)
      expect(result[1].status).toBe(200)
      expect(typeof result[0].body.document).toBe('string')
      expect(typeof result[1].body.document).toBe('string')
      expect(result[0].body.document.endsWith('.txt')).toBe(true)
      expect(result[1].body.document.endsWith('.txt')).toBe(true)

      const fileUrlOne = adminPb.getFileUrl(result[0].body, result[0].body.document)
      const fileUrlTwo = adminPb.getFileUrl(result[1].body, result[1].body.document)

      const fileOneText = await (await fetch(fileUrlOne)).text()
      const fileTwoText = await (await fetch(fileUrlTwo)).text()

      expect(fileOneText).toBe('alpha-content')
      expect(fileTwoText).toBe('beta-content')
    } finally {
      await cleanupFiles()
    }
  })

  it('should rollback multipart batch writes when any nested request fails', async () => {
    const { collection: filesCollection, cleanup: cleanupFiles } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'document', type: 'file', required: false, options: { maxSelect: 1 } },
      ],
    })

    try {
      const rollbackId = randomRecordId()

      const form = new FormData()
      form.append('@jsonPayload', JSON.stringify({
        requests: [
          {
            method: 'POST',
            url: `/api/collections/${filesCollection.name}/records`,
            body: { id: rollbackId, title: 'will-rollback' },
          },
          {
            method: 'POST',
            url: `/api/collections/${filesCollection.name}/records`,
            body: { id: randomRecordId() },
          },
        ],
      }))
      form.append(
        'requests.0.document',
        new Blob(['rollback-content'], { type: 'text/plain' }),
        'rollback.txt',
      )

      await expect(
        adminPb.send('/api/batch', {
          method: 'POST',
          body: form,
        }),
      ).rejects.toThrow(/Batch transaction failed/i)

      await expect(
        adminPb.collection(filesCollection.name).getOne(rollbackId),
      ).rejects.toThrow()

      const storageDir = join(
        process.cwd(),
        '../../pb_data/storage',
        filesCollection.id,
        rollbackId,
      )
      expect(existsSync(storageDir)).toBe(false)
    } finally {
      await cleanupFiles()
    }
  })

  it('should replace single file field on multipart batch update', async () => {
    const { collection: filesCollection, cleanup: cleanupFiles } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'document', type: 'file', required: false, options: { maxSelect: 1 } },
      ],
    })

    try {
      const recordId = randomRecordId()

      const createForm = new FormData()
      createForm.append('@jsonPayload', JSON.stringify({
        requests: [
          {
            method: 'POST',
            url: `/api/collections/${filesCollection.name}/records`,
            body: { id: recordId, title: 'replace-file' },
          },
        ],
      }))
      createForm.append(
        'requests.0.document',
        new Blob(['v1-content'], { type: 'text/plain' }),
        'v1.txt',
      )

      const created = await adminPb.send('/api/batch', {
        method: 'POST',
        body: createForm,
      })
      const oldFilename = created[0].body.document
      expect(typeof oldFilename).toBe('string')

      const updateForm = new FormData()
      updateForm.append('@jsonPayload', JSON.stringify({
        requests: [
          {
            method: 'PATCH',
            url: `/api/collections/${filesCollection.name}/records/${recordId}`,
            body: { title: 'replace-file-updated' },
          },
        ],
      }))
      updateForm.append(
        'requests.0.document',
        new Blob(['v2-content'], { type: 'text/plain' }),
        'v2.txt',
      )

      const updated = await adminPb.send('/api/batch', {
        method: 'POST',
        body: updateForm,
      })
      const newFilename = updated[0].body.document
      expect(typeof newFilename).toBe('string')
      expect(newFilename).not.toBe(oldFilename)

      const newFileUrl = adminPb.getFileUrl(updated[0].body, newFilename)
      const oldFileUrl = adminPb.getFileUrl(updated[0].body, oldFilename)

      expect(await (await fetch(newFileUrl)).text()).toBe('v2-content')
      expect((await fetch(oldFileUrl)).status).toBe(404)
    } finally {
      await cleanupFiles()
    }
  })
})
