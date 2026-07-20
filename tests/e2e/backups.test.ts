import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { getAdminPb, getFreshPb } from './helpers'

const runConfiguredLifecycle = process.env.PPBASE_BACKUP_E2E === '1' ? it : it.skip

describe('PocketBase BackupService v0.27 compatibility', () => {
  it('builds the exact tokenized PPBase download URL', () => {
    const pb = getFreshPb()
    const url = pb.backups.getDownloadURL('file token/+', 'backup key.zip')

    expect(url).toContain('/api/backups/backup%20key.zip?token=file%20token%2F%2B')
  })

  it('routes restore() to the PocketBase-compatible PPBase endpoint', async () => {
    const pb = getFreshPb()
    let requestPath = ''
    let requestOptions: Record<string, unknown> | undefined
    pb.send = (async (path: string, options?: Record<string, unknown>) => {
      requestPath = path
      requestOptions = options
      return undefined
    }) as typeof pb.send

    await pb.backups.restore('backup key.zip')

    expect(requestPath).toBe('/api/backups/backup%20key.zip/restore')
    expect(requestOptions?.method).toBe('POST')
  })

  describe('configured native-backup server', () => {
    let pb: Awaited<ReturnType<typeof getAdminPb>>

    beforeAll(async () => {
      if (process.env.PPBASE_BACKUP_E2E !== '1') return
      pb = await getAdminPb()
    })

    afterAll(() => {
      if (pb) pb.authStore.clear()
    })

    runConfiguredLifecycle(
      'creates, lists, downloads, deletes and uploads a real PPBase ZIP',
      async () => {
        const before = await pb.backups.getFullList()
        const beforeKeys = new Set(before.map(item => item.key))
        const requestedName = `ppbase_backup_sdk_${Date.now()}.zip`

        await pb.backups.create(requestedName)
        const createdList = await pb.backups.getFullList()
        const created = createdList.find(item => !beforeKeys.has(item.key))
        expect(created).toBeTruthy()
        expect(created?.key).toBe(requestedName)
        expect(typeof created?.size).toBe('number')
        expect(created!.size).toBeGreaterThan(0)
        expect(typeof created?.modified).toBe('string')

        await expect(pb.backups.create(requestedName)).rejects.toMatchObject({
          status: 409,
        })

        const token = await pb.files.getToken()
        const downloadURL = pb.backups.getDownloadURL(token, created!.key)
        const response = await fetch(downloadURL)
        expect(response.status).toBe(200)
        expect(response.headers.get('content-type')).toContain('application/zip')
        expect(response.headers.get('content-disposition')).toContain(requestedName)
        expect(response.headers.get('content-length')).toBe(String(created!.size))
        const zipBytes = new Uint8Array(await response.arrayBuffer())
        expect(Array.from(zipBytes.slice(0, 4))).toEqual([0x50, 0x4b, 0x03, 0x04])

        const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'ppbase-backup-e2e-'))
        const downloadedPath = path.join(tempDir, 'downloaded.zip')
        try {
          await fs.writeFile(downloadedPath, zipBytes)
          expect((await fs.stat(downloadedPath)).size).toBe(zipBytes.byteLength)

          await pb.backups.delete(created!.key)
          expect((await pb.backups.getFullList()).some(item => item.key === created!.key)).toBe(false)

          await pb.backups.upload({
            file: new Blob([zipBytes], { type: 'application/zip' }),
          })
          const uploaded = (await pb.backups.getFullList()).find(
            item => item.key === created!.key,
          )
          expect(uploaded).toBeTruthy()
          expect(uploaded!.size).toBe(zipBytes.byteLength)
          await pb.backups.delete(created!.key)
        } finally {
          await fs.rm(tempDir, { recursive: true, force: true })
        }
      },
      120_000,
    )
  })
})
