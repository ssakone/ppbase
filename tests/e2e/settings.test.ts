/**
 * PocketBase SDK E2E Tests: Settings API
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { createServer } from 'node:net'
import type { AddressInfo, Server, Socket } from 'node:net'
import type PocketBase from 'pocketbase'
import { getAdminPb } from './helpers'

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function expectPbError(action: () => Promise<unknown>, status: number): Promise<any> {
  try {
    await action()
    throw new Error(`Expected request to fail with status ${status}`)
  } catch (err: any) {
    expect(err).toBeTruthy()
    expect(err.status).toBe(status)
    return err
  }
}

interface TestSmtpServer {
  port: number
  messages: string[]
  close: () => Promise<void>
}

async function startTestSmtpServer(): Promise<TestSmtpServer> {
  const messages: string[] = []
  const sockets = new Set<Socket>()

  const server: Server = createServer((socket) => {
    sockets.add(socket)
    socket.setEncoding('utf8')
    socket.write('220 localhost ESMTP PPBase Test\r\n')

    let buffer = ''
    let inData = false
    let dataLines: string[] = []

    const send = (line: string) => socket.write(`${line}\r\n`)

    socket.on('data', (chunk: string) => {
      buffer += chunk

      while (true) {
        const newlineIndex = buffer.indexOf('\n')
        if (newlineIndex === -1) {
          break
        }

        let line = buffer.slice(0, newlineIndex)
        buffer = buffer.slice(newlineIndex + 1)
        line = line.replace(/\r$/, '')

        if (inData) {
          if (line === '.') {
            messages.push(dataLines.join('\n'))
            dataLines = []
            inData = false
            send('250 2.0.0 queued')
          } else {
            dataLines.push(line)
          }
          continue
        }

        const upper = line.toUpperCase()
        if (upper.startsWith('EHLO') || upper.startsWith('HELO')) {
          socket.write('250-localhost\r\n250 SIZE 35882577\r\n')
          continue
        }
        if (upper.startsWith('MAIL FROM:')) {
          send('250 2.1.0 OK')
          continue
        }
        if (upper.startsWith('RCPT TO:')) {
          send('250 2.1.5 OK')
          continue
        }
        if (upper === 'DATA') {
          inData = true
          send('354 End data with <CR><LF>.<CR><LF>')
          continue
        }
        if (upper === 'QUIT') {
          send('221 2.0.0 Bye')
          socket.end()
          continue
        }
        if (upper === 'NOOP' || upper === 'RSET') {
          send('250 2.0.0 OK')
          continue
        }

        send('250 2.0.0 OK')
      }
    })

    socket.on('close', () => {
      sockets.delete(socket)
    })
    socket.on('error', () => {
      sockets.delete(socket)
    })
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject)
      resolve()
    })
  })

  const address = server.address() as AddressInfo

  return {
    port: address.port,
    messages,
    close: async () => {
      for (const socket of sockets) {
        socket.destroy()
      }
      await new Promise<void>((resolve, reject) => {
        server.close((err) => {
          if (err) {
            reject(err)
            return
          }
          resolve()
        })
      })
    },
  }
}

async function waitForMessage(messages: string[], timeoutMs: number = 3000): Promise<string> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (messages.length > 0) {
      return messages[messages.length - 1]
    }
    await delay(50)
  }
  throw new Error(`No SMTP message received within ${timeoutMs}ms`)
}

describe('Settings API', () => {
  let adminPb: PocketBase | null = null
  let previousMeta: Record<string, unknown> = {}
  let previousSmtp: Record<string, unknown> = {}
  let previousS3: Record<string, unknown> = {}

  beforeAll(async () => {
    adminPb = await getAdminPb()
    const settings = await adminPb.settings.getAll()
    previousMeta = { ...((settings.meta || {}) as Record<string, unknown>) }
    previousSmtp = { ...((settings.smtp || {}) as Record<string, unknown>) }
    previousS3 = { ...((settings.s3 || {}) as Record<string, unknown>) }
  })

  afterAll(async () => {
    const pb = adminPb
    if (!pb) {
      return
    }
    await pb.settings.update({
      meta: previousMeta,
      smtp: previousSmtp,
      s3: previousS3,
    })
  })

  it('should validate required payload fields for settings test email', async () => {
    const pb = adminPb
    if (!pb) {
      throw new Error('Admin client is not initialized.')
    }

    const err = await expectPbError(
      () =>
        pb.send('/api/settings/test/email', {
          method: 'POST',
          body: {},
        }),
      400,
    )

    expect(err.response?.message).toBe('Failed to send the test email.')
    expect(err.response?.data?.email?.code).toBe('validation_required')
    expect(err.response?.data?.template?.code).toBe('validation_required')
  })

  it('should fail when SMTP host is not configured', async () => {
    const pb = adminPb
    if (!pb) {
      throw new Error('Admin client is not initialized.')
    }

    await pb.settings.update({
      smtp: {
        host: '',
        port: 587,
        username: '',
        password: '',
        tls: true,
      },
    })

    const err = await expectPbError(
      () =>
        pb.send('/api/settings/test/email', {
          method: 'POST',
          body: {
            email: 'qa@example.com',
            template: 'verification',
          },
        }),
      400,
    )

    expect(err.response?.data?.smtp?.code).toBe('validation_required')
  })

  it('should send test email through configured SMTP server', async () => {
    const pb = adminPb
    if (!pb) {
      throw new Error('Admin client is not initialized.')
    }

    const smtpServer = await startTestSmtpServer()
    try {
      await pb.settings.update({
        meta: {
          ...previousMeta,
          appName: 'PPBase E2E',
          senderName: 'PPBase QA',
          senderAddress: 'noreply@e2e.test',
        },
        smtp: {
          host: '127.0.0.1',
          port: smtpServer.port,
          username: '',
          password: '',
          tls: false,
        },
      })

      const result = await pb.send('/api/settings/test/email', {
        method: 'POST',
        body: {
          email: 'dev@example.com',
          template: 'email-change',
          collection: 'users',
        },
      })
      // PocketBase JS SDK normalizes empty 204 JSON bodies to {}.
      expect(result).toEqual({})

      const rawMessage = await waitForMessage(smtpServer.messages, 5000)
      expect(rawMessage).toContain('To: dev@example.com')
      expect(rawMessage).toContain('From: PPBase QA <noreply@e2e.test>')
      expect(rawMessage).toContain('Subject: PPBase E2E test email: email change')
      expect(rawMessage).toContain("This is a PPBase SMTP test message for template 'email-change'.")
      expect(rawMessage).toContain('Collection: users')
    } finally {
      await smtpServer.close()
    }
  })

  it('should persist S3 settings fields', async () => {
    const pb = adminPb
    if (!pb) {
      throw new Error('Admin client is not initialized.')
    }

    const payload = {
      endpoint: 'https://example-r2.invalid',
      bucket: 'bucket-e2e',
      region: 'auto',
      accessKey: 'r2-access',
      secret: 'r2-secret',
      forcePathStyle: true,
    }

    const updated = await pb.settings.update({ s3: payload })
    expect(updated?.s3?.endpoint).toBe(payload.endpoint)
    expect(updated?.s3?.bucket).toBe(payload.bucket)
    expect(updated?.s3?.region).toBe(payload.region)
    expect(updated?.s3?.accessKey).toBe(payload.accessKey)
    expect(updated?.s3?.secret).toBe(payload.secret)
    expect(Boolean(updated?.s3?.forcePathStyle)).toBe(true)

    const fetched = await pb.settings.getAll()
    expect(fetched?.s3?.endpoint).toBe(payload.endpoint)
    expect(fetched?.s3?.bucket).toBe(payload.bucket)
    expect(fetched?.s3?.region).toBe(payload.region)
    expect(fetched?.s3?.accessKey).toBe(payload.accessKey)
    expect(fetched?.s3?.secret).toBe(payload.secret)
    expect(Boolean(fetched?.s3?.forcePathStyle)).toBe(true)
  })
})
