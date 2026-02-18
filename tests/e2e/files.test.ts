/**
 * PocketBase SDK E2E Tests: Files API
 */
import { describe, it, expect, beforeAll } from 'vitest'
import type PocketBase from 'pocketbase'
import { createTestCollection, getAdminPb, getFreshPb } from './helpers'

describe('Files API', () => {
  let adminPb: PocketBase

  beforeAll(async () => {
    adminPb = await getAdminPb()
  })

  it('should honor download query option when serving files', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'doc', type: 'file', required: false, options: { maxSelect: 1 } },
      ],
    })

    try {
      const formData = new FormData()
      formData.set('doc', new Blob(['hello file body'], { type: 'text/plain' }), 'sample.txt')

      const created = await adminPb.send(`/api/collections/${collection.name}/records`, {
        method: 'POST',
        body: formData,
      })

      const storedFilename = String(created.doc || '')
      expect(storedFilename).toMatch(/^sample_[A-Za-z0-9]{10}\.txt$/)

      const fileUrl = adminPb.files.getUrl(created, storedFilename)

      const inlineRes = await fetch(fileUrl)
      expect(inlineRes.status).toBe(200)
      expect(await inlineRes.text()).toBe('hello file body')
      const inlineDisposition = inlineRes.headers.get('content-disposition') || ''
      expect(inlineDisposition.toLowerCase()).not.toContain('attachment')

      const downloadRes = await fetch(`${fileUrl}?download=1`)
      expect(downloadRes.status).toBe(200)
      const downloadDisposition = downloadRes.headers.get('content-disposition') || ''
      expect(downloadDisposition.toLowerCase()).toContain('attachment')
      expect(downloadDisposition).toContain(storedFilename)

      const customFilename = 'custom-report.txt'
      const namedDownloadRes = await fetch(
        `${fileUrl}?download=${encodeURIComponent(customFilename)}`
      )
      expect(namedDownloadRes.status).toBe(200)
      const namedDisposition = namedDownloadRes.headers.get('content-disposition') || ''
      expect(namedDisposition.toLowerCase()).toContain('attachment')
      expect(namedDisposition).toContain(customFilename)

      const thumbFallbackRes = await fetch(`${fileUrl}?thumb=100x100`)
      expect(thumbFallbackRes.status).toBe(200)
      expect(await thumbFallbackRes.text()).toBe('hello file body')
    } finally {
      await cleanup()
    }
  })

  it('should require file token for protected files and allow access with valid token', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'privateDoc',
          type: 'file',
          required: false,
          options: { maxSelect: 1, protected: true },
        },
      ],
    })

    try {
      const formData = new FormData()
      formData.set(
        'privateDoc',
        new Blob(['top secret'], { type: 'text/plain' }),
        'private.txt'
      )

      const created = await adminPb.send(`/api/collections/${collection.name}/records`, {
        method: 'POST',
        body: formData,
      })

      const storedFilename = String(created.privateDoc || '')
      expect(storedFilename).toMatch(/^private_[A-Za-z0-9]{10}\.txt$/)
      const fileUrl = adminPb.files.getUrl(created, storedFilename)

      const noTokenRes = await fetch(fileUrl)
      expect(noTokenRes.status).toBe(404)

      const invalidTokenRes = await fetch(`${fileUrl}?token=invalid.token.value`)
      expect(invalidTokenRes.status).toBe(404)

      const anonymousPb = getFreshPb()
      await expect(anonymousPb.files.getToken()).rejects.toThrow()

      const fileToken = await adminPb.files.getToken()
      expect(fileToken).toBeTruthy()

      const protectedRes = await fetch(`${fileUrl}?token=${encodeURIComponent(fileToken)}`)
      expect(protectedRes.status).toBe(200)
      expect(await protectedRes.text()).toBe('top secret')
    } finally {
      await cleanup()
    }
  })

  it('should enforce maxSize for file uploads', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'doc',
          type: 'file',
          required: false,
          options: { maxSelect: 1, maxSize: 5 },
        },
      ],
    })

    try {
      const tooBigForm = new FormData()
      tooBigForm.set('doc', new Blob(['123456'], { type: 'text/plain' }), 'big.txt')

      await expect(
        adminPb.send(`/api/collections/${collection.name}/records`, {
          method: 'POST',
          body: tooBigForm,
        })
      ).rejects.toThrow()

      const okForm = new FormData()
      okForm.set('doc', new Blob(['12345'], { type: 'text/plain' }), 'ok.txt')

      const created = await adminPb.send(`/api/collections/${collection.name}/records`, {
        method: 'POST',
        body: okForm,
      })
      expect(String(created.doc || '')).toMatch(/^ok_[A-Za-z0-9]{10}\.txt$/)
    } finally {
      await cleanup()
    }
  })

  it('should enforce mimeTypes for file uploads', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'photo',
          type: 'file',
          required: false,
          options: { maxSelect: 1, mimeTypes: ['image/png'] },
        },
      ],
    })

    try {
      const wrongMimeForm = new FormData()
      wrongMimeForm.set(
        'photo',
        new Blob(['plain text'], { type: 'text/plain' }),
        'wrong.txt'
      )

      await expect(
        adminPb.send(`/api/collections/${collection.name}/records`, {
          method: 'POST',
          body: wrongMimeForm,
        })
      ).rejects.toThrow()

      const validMimeForm = new FormData()
      validMimeForm.set(
        'photo',
        new Blob(['png-bytes'], { type: 'image/png' }),
        'ok.png'
      )

      const created = await adminPb.send(`/api/collections/${collection.name}/records`, {
        method: 'POST',
        body: validMimeForm,
      })
      expect(String(created.photo || '')).toMatch(/^ok_[A-Za-z0-9]{10}\.png$/)
    } finally {
      await cleanup()
    }
  })
})
