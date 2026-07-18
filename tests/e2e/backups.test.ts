import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { getAdminPb, getFreshPb } from './helpers'

const runConfiguredLifecycle = process.env.PPBASE_BACKUP_E2E === '1' ? it : it.skip

// Partial transport compatibility only: restore() and the Dashboard workflow
// are intentionally not delivered in this tranche.
describe('Partial PocketBase BackupService v0.27 transport compatibility', () => {
  it('builds the exact tokenized PPBase download URL', () => {
    const pb = getFreshPb()
    const url = pb.backups.getDownloadURL('file token/+', 'backup key.zip')

    expect(url).toContain('/api/backups/backup%20key.zip?token=file%20token%2F%2B')
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
        expect(created?.key).toBeTruthy()
        expect(typeof created?.size).toBe('number')
        expect(created!.size).toBeGreaterThan(0)
        expect(typeof created?.modified).toBe('string')

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
