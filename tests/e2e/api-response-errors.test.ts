import { describe, expect, it } from 'vitest'

import {
  buildApiResponseError,
  parseResponsePayload,
} from '../../admin-ui/src/api/response'

describe('Admin API response errors', () => {
  it('preserves a PocketBase JSON error', () => {
    const payload = parseResponsePayload(JSON.stringify({
      status: 400,
      message: 'Failed to authenticate.',
      data: {
        identity: {
          code: 'validation_invalid_credentials',
          message: 'Invalid login credentials.',
        },
      },
    }))

    expect(buildApiResponseError(400, payload, 'application/json')).toEqual({
      status: 400,
      message: 'Failed to authenticate.',
      data: {
        identity: {
          code: 'validation_invalid_credentials',
          message: 'Invalid login credentials.',
        },
      },
    })
  })

  it('does not expose an HTML proxy error in the UI', () => {
    const html = '<!DOCTYPE html><html><body>Cloudflare 520</body></html>'
    const error = buildApiResponseError(
      520,
      parseResponsePayload(html),
      'text/html; charset=UTF-8',
    )

    expect(error).toEqual({
      status: 520,
      message: 'The server is temporarily unavailable. Please try again.',
    })
    expect(error.message).not.toContain('<html>')
  })

  it('keeps a short plain-text error but bounds oversized responses', () => {
    expect(buildApiResponseError(400, 'Invalid request.', 'text/plain')).toEqual({
      status: 400,
      message: 'Invalid request.',
    })
    expect(buildApiResponseError(502, 'x'.repeat(501), 'text/plain')).toEqual({
      status: 502,
      message: 'The server is temporarily unavailable. Please try again.',
    })
  })
})
