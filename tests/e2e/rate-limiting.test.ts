import { describe, it, expect, beforeAll, afterAll } from 'vitest'
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

describe('Rate Limiting', () => {
  let adminPb: PocketBase | null = null
  let previousRateLimiting: Record<string, unknown> = {}
  let previousTrustedProxy: Record<string, unknown> = {}

  beforeAll(async () => {
    adminPb = await getAdminPb()
    const settings = await adminPb.settings.getAll()
    previousRateLimiting = {
      ...((settings.rateLimiting || {}) as Record<string, unknown>),
    }
    previousTrustedProxy = {
      ...((settings.trustedProxy || {}) as Record<string, unknown>),
    }
  })

  afterAll(async () => {
    const pb = adminPb
    if (!pb) {
      return
    }
    await delay(1200)
    await pb.settings.update({
      rateLimiting: previousRateLimiting,
      trustedProxy: previousTrustedProxy,
    })
  })

  it('should enforce the configured global API rate limit', async () => {
    const pb = adminPb
    if (!pb) {
      throw new Error('Admin client is not initialized.')
    }

    await pb.settings.update({
      rateLimiting: {
        enabled: true,
        maxRequests: 1,
        window: 1,
      },
      trustedProxy: {
        headers: [],
        useLeftmostIP: false,
      },
    })

    try {
      const first = await pb.send('/api/collections?page=1&perPage=1', {
        method: 'GET',
      })
      expect(first).toBeTruthy()

      const err = await expectPbError(
        () =>
          pb.send('/api/collections?page=1&perPage=1', {
            method: 'GET',
          }),
        429,
      )
      expect(err.response?.message).toBe('Too many requests.')
    } finally {
      await delay(1200)
      await pb.settings.update({
        rateLimiting: previousRateLimiting,
        trustedProxy: previousTrustedProxy,
      })
    }
  })
})
