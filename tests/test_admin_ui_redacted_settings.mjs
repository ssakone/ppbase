import assert from 'node:assert/strict'
import { register } from 'node:module'
import test from 'node:test'

import { SECRET_MASK, filterRedactedProps } from '../admin-ui/src/lib/redacted-settings.ts'

// The dashboard sources use extensionless relative imports (resolved by Vite);
// map them to their .ts files so the real API endpoint module can be loaded.
register(
  'data:text/javascript,' +
    encodeURIComponent(`
export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context)
  } catch (error) {
    if (specifier.startsWith('.') && !/\\.[cm]?[jt]sx?$/.test(specifier)) {
      return next(specifier + '.ts', context)
    }
    throw error
  }
}
`),
)

test('masked secrets are removed, real values and empty strings are kept', () => {
  const form = {
    smtp: { host: 'smtp.example.com', port: 587, username: 'mailer', password: SECRET_MASK, tls: true },
    s3: { bucket: 'files', secret: 'new-s3-secret' },
    backups: { cron: '0 0 * * *', s3: { secret: SECRET_MASK, accessKey: 'key' } },
    recordAuthToken: { secret: '', duration: 1209600 },
    trustedProxy: { headers: [SECRET_MASK, 'X-Real-IP'] },
  }

  const filtered = filterRedactedProps(form)

  assert.equal('password' in filtered.smtp, false)
  assert.equal(filtered.smtp.username, 'mailer')
  assert.equal(filtered.s3.secret, 'new-s3-secret')
  assert.equal('secret' in filtered.backups.s3, false)
  assert.equal(filtered.backups.s3.accessKey, 'key')
  // An empty string still clears a secret, as in PocketBase 0.22.34.
  assert.equal(filtered.recordAuthToken.secret, '')
  // Same traversal as PocketBase: array items equal to the mask are removed too.
  assert.deepEqual(filtered.trustedProxy.headers.filter(Boolean), ['X-Real-IP'])
  // The form state itself is untouched.
  assert.equal(form.smtp.password, SECRET_MASK)
})

test('the dashboard settings save never sends the mask', async () => {
  const sent = []
  globalThis.window = { location: { origin: 'http://ppbase.test' } }
  globalThis.localStorage = {
    getItem: () => 'admin-token',
    setItem: () => {},
    removeItem: () => {},
  }
  globalThis.fetch = async (url, init) => {
    sent.push({ url, init })
    return new Response(JSON.stringify({ smtp: { password: SECRET_MASK } }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  const { updateSettings } = await import('../admin-ui/src/api/endpoints/settings.ts')
  // What the SMTP tab sends when only the host changed (settings-form.tsx).
  await updateSettings({
    smtp: { host: 'smtp2.example.com', port: 587, username: 'mailer', password: SECRET_MASK, tls: true },
  })
  // What the S3 tab sends when the secret was retyped.
  await updateSettings({ s3: { bucket: 'files', secret: 'retyped-secret' } })

  assert.equal(sent.length, 2)
  assert.equal(sent[0].url, 'http://ppbase.test/api/settings')
  assert.equal(sent[0].init.method, 'PATCH')
  assert.equal(sent[0].init.body.includes(SECRET_MASK), false)
  assert.deepEqual(JSON.parse(sent[0].init.body), {
    smtp: { host: 'smtp2.example.com', port: 587, username: 'mailer', tls: true },
  })
  assert.deepEqual(JSON.parse(sent[1].init.body), {
    s3: { bucket: 'files', secret: 'retyped-secret' },
  })
})
