import fs from 'node:fs/promises'
import path from 'node:path'
import { describe, it, expect, beforeAll } from 'vitest'

const BASE_URL = process.env.PPBASE_URL || 'http://localhost:8090'
const PUBLIC_DIR = process.env.PPBASE_PUBLIC_DIR

const describePublic = PUBLIC_DIR ? describe : describe.skip

describePublic('Public directory static hosting', () => {
  const publicDir = PUBLIC_DIR as string
  const indexMarker = 'PPBASE_PUBLIC_E2E_INDEX'

  beforeAll(async () => {
    await fs.mkdir(publicDir, { recursive: true })
    await fs.writeFile(
      path.join(publicDir, 'index.html'),
      `<html><body><h1>${indexMarker}</h1></body></html>`,
    )
    await fs.mkdir(path.join(publicDir, 'assets'), { recursive: true })
    await fs.writeFile(path.join(publicDir, 'assets', 'hello.txt'), 'hello-public')
  })

  it('should serve index.html at root when present', async () => {
    const response = await fetch(`${BASE_URL}/`)
    const body = await response.text()

    expect(response.status).toBe(200)
    expect(body).toContain(indexMarker)
  })

  it('should serve files without enabling directory listing', async () => {
    const fileResponse = await fetch(`${BASE_URL}/assets/hello.txt`)
    const directoryResponse = await fetch(`${BASE_URL}/assets/`)

    expect(fileResponse.status).toBe(200)
    expect(await fileResponse.text()).toBe('hello-public')

    expect(directoryResponse.status).toBe(404)
    expect(await directoryResponse.text()).not.toContain('Index of')
  })
})
