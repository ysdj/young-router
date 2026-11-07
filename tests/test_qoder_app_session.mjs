/**
 * Reading the Qoder desktop app's own sign-in.
 *
 * The desktop app keeps its session in a Chromium OSCrypt envelope, and the
 * key never sits beside it: macOS puts the password in the login keychain,
 * Windows protects it with DPAPI.  These tests drive the envelope, the region
 * decision and the "the keychain refused" answer against fixtures, so the
 * reader can be changed without a desktop app installed — and so the failure
 * that matters most (a session attributed to the wrong gateway) is pinned.
 */

import assert from 'node:assert/strict'
import { createCipheriv, pbkdf2Sync, randomBytes } from 'node:crypto'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const sessions = await import(
  pathToFileURL(join(import.meta.dirname, '../young_router/adapters/qoder_app_session.mjs')).href
)

/** The envelope the desktop app writes, for a given key shape. */
function seal(plaintext, key) {
  if (key.length === 16) {
    const iv = Buffer.alloc(16, ' ')
    const cipher = createCipheriv('aes-128-cbc', key, iv)
    return Buffer.concat([Buffer.from('v10'), cipher.update(plaintext), cipher.final()])
  }
  const nonce = randomBytes(12)
  const cipher = createCipheriv('aes-256-gcm', key, nonce)
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return Buffer.concat([Buffer.from('v10'), nonce, ciphertext, cipher.getAuthTag()])
}

const PAYLOAD = {
  schemaVersion: 1,
  authenticationContext: '["https://qoder.com.cn","https://openapi.qoder.com.cn","uid","prod"]',
  token: 'dt-example-access-token',
  refreshToken: 'drt-example-refresh-token',
  expiresAt: '2099-01-01T00:00:00Z',
  refreshTokenExpiresAt: '2099-12-01T00:00:00Z',
  user: { id: 'uid-example', name: 'Example User', email: '' },
}

// -- envelope -----------------------------------------------------------------

const macKey = pbkdf2Sync('keychain-password', 'saltysalt', 1003, 16, 'sha1')
const macBlob = seal(Buffer.from(JSON.stringify(PAYLOAD)), macKey)
assert.equal(
  JSON.parse(sessions.decryptEnvelope(macBlob, macKey).toString('utf8')).token,
  'dt-example-access-token',
)
// A key derived from the wrong password is "no session", never a crash or a
// half-decoded buffer that would later be parsed into a session.
assert.equal(
  sessions.decryptEnvelope(macBlob, sessions.chromiusKeyFromPassword('another-password')),
  undefined,
)

const gcmKey = randomBytes(32)
const gcmBlob = seal(Buffer.from(JSON.stringify(PAYLOAD)), gcmKey)
assert.equal(
  JSON.parse(sessions.decryptEnvelope(gcmBlob, gcmKey).toString('utf8')).refreshToken,
  'drt-example-refresh-token',
)
// A tampered tag must not decrypt: GCM's whole point is that it cannot.
const tampered = Buffer.from(gcmBlob)
tampered[tampered.length - 1] ^= 0xff
assert.equal(sessions.decryptEnvelope(tampered, gcmKey), undefined)

// -- region -------------------------------------------------------------------

assert.equal(sessions.regionOfAuthenticationContext(PAYLOAD.authenticationContext), 'cn')
assert.equal(
  sessions.regionOfAuthenticationContext('["https://qoder.sh","https://openapi.qoder.sh"]'),
  'global',
)
assert.equal(sessions.regionOfAuthenticationContext(''), undefined)
assert.equal(sessions.regionOfProfileDirectory('com.qodercn.app.stable'), 'cn')
assert.equal(sessions.regionOfProfileDirectory('com.qoder.app.stable'), 'global')

// -- one profile --------------------------------------------------------------

const root = mkdtempSync(join(tmpdir(), 'qoder-app-session-'))
try {
  /** A profile directory carrying one session envelope. */
  const profile = (directory, key, payload = PAYLOAD) => {
    const path = join(root, directory)
    mkdirSync(path, { recursive: true })
    writeFileSync(join(path, 'auth.v1.dat'), seal(Buffer.from(JSON.stringify(payload)), key))
    return { directory, profile: path, products: ['Qoder CN'], regionHint: undefined }
  }

  const cnProfile = profile('com.qodercn.app.stable', macKey)
  const read = await sessions.readProfileSession(cnProfile, {
    platform: 'darwin',
    keychain: async () => 'keychain-password',
  })
  assert.equal(read.region, 'cn')
  assert.equal(read.token, 'dt-example-access-token')
  assert.equal(read.userName, 'Example User')
  assert.equal(read.profile, 'com.qodercn.app.stable')

  // The keychain sheet being refused is "no session", not "a broken account".
  assert.equal(
    await sessions.readProfileSession(cnProfile, {
      platform: 'darwin',
      keychain: async () => '',
    }),
    undefined,
  )

  // A context that names neither gateway falls back to the profile directory's
  // own name, which is the international build here.
  const namedProfile = profile('com.qoder.app.stable', macKey, {
    ...PAYLOAD,
    authenticationContext: '["https://example.invalid"]',
  })
  const namedSession = await sessions.readProfileSession(namedProfile, {
    platform: 'darwin',
    keychain: async () => 'keychain-password',
  })
  assert.equal(namedSession.region, 'global')

  // A directory this build does not recognise claims no region, so the session
  // is never filed under a gateway it may not belong to.
  assert.equal(sessions.regionOfProfileDirectory('acme.qoder-clone.app'), undefined)
  const stranger = profile('acme.qoder-clone.app', macKey, {
    ...PAYLOAD,
    authenticationContext: '["https://example.invalid"]',
  })
  assert.equal(
    await sessions.readProfileSession(stranger, {
      platform: 'darwin',
      keychain: async () => 'keychain-password',
    }),
    undefined,
  )

  // Nothing under a home with no Application Support root answers a session.
  const none = await sessions.readDesktopSessions({
    platform: 'darwin',
    home: root,
    keychain: async () => 'keychain-password',
  })
  assert.deepEqual(Object.keys(none), [])

  // -- refresh ---------------------------------------------------------------

  const refreshed = await sessions.refreshDesktopSession(
    { refreshToken: 'drt-old', expiresAt: 0 },
    'https://openapi.qoder.com.cn',
    {
      fetch: async (url, init) => {
        assert.equal(url, 'https://openapi.qoder.com.cn/api/v1/deviceToken/refresh')
        assert.deepEqual(JSON.parse(init.body), { refresh_token: 'drt-old' })
        return new Response(
          JSON.stringify({ token: 'dt-new', refresh_token: 'drt-new', expires_in: 3600_000 }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      },
    },
  )
  assert.equal(refreshed.token, 'dt-new')
  assert.equal(refreshed.refreshToken, 'drt-new')
  assert.ok(refreshed.expiresAt > Date.now())

  // A refused refresh is "sign in again", and a session with no refresh token
  // never reaches the network at all.
  assert.equal(
    await sessions.refreshDesktopSession(
      { refreshToken: 'drt-old', expiresAt: 0 },
      'https://openapi.qoder.com.cn',
      { fetch: async () => new Response('{}', { status: 401 }) },
    ),
    undefined,
  )
  assert.equal(
    await sessions.refreshDesktopSession(
      { refreshToken: '', expiresAt: 0 },
      'https://openapi.qoder.com.cn',
      { fetch: async () => { throw new Error('must not be called') } },
    ),
    undefined,
  )
} finally {
  rmSync(root, { recursive: true, force: true })
}

// -- a host's exported session -------------------------------------------------
// A gateway has no app to read, so the machine that has one exports it.  The
// reader must answer with the same shape a profile does, refuse a session that
// names another gateway, and treat "no credential" as no session at all.
{
  const dir = mkdtempSync(join(tmpdir(), 'qoder-seed-session-'))
  try {
    const file = join(dir, 'qoder-desktop.info')
    writeFileSync(
      file,
      JSON.stringify({
        version: 1,
        regions: {
          cn: {
            token: 'dt-seeded',
            refreshToken: 'drt-seeded',
            expiresAt: 4102444800000,
            userId: 'uid-seeded',
            userName: 'Seeded User',
            userEmail: '',
            context: PAYLOAD.authenticationContext,
            profile: 'com.qodercn.app.stable',
          },
          // States the other gateway, so it is not this file's `global`.
          global: {
            token: 'dt-elsewhere',
            refreshToken: '',
            expiresAt: 0,
            userId: 'uid-elsewhere',
            userName: 'Elsewhere',
            userEmail: '',
            context: PAYLOAD.authenticationContext,
            profile: 'com.qoder.app.stable',
          },
          // Neither a token nor a refresh token: not a session.
          empty: { token: '   ', refreshToken: '' },
        },
      }),
      { mode: 0o600 },
    )
    const seeded = sessions.readSeededSessions({ file })
    assert.deepEqual(Object.keys(seeded), ['cn'])
    assert.equal(seeded.cn.token, 'dt-seeded')
    assert.equal(seeded.cn.refreshToken, 'drt-seeded')
    assert.equal(seeded.cn.expiresAt, 4102444800000)
    assert.equal(seeded.cn.userName, 'Seeded User')
    assert.equal(seeded.cn.profile, 'com.qodercn.app.stable')
    assert.equal(seeded.cn.region, 'cn')
    assert.equal(sessions.readSeededSession('cn', { file }).token, 'dt-seeded')
    assert.equal(sessions.readSeededSession('global', { file }), undefined)

    // The variable is how a deployment states the file; an unset or unreadable
    // one is "this machine holds no exported session", never a throw.
    const previous = process.env[sessions.SESSION_FILE_ENV]
    try {
      process.env[sessions.SESSION_FILE_ENV] = file
      assert.equal(sessions.readSeededSession('cn').token, 'dt-seeded')
      process.env[sessions.SESSION_FILE_ENV] = join(dir, 'missing.info')
      assert.deepEqual(sessions.readSeededSessions(), {})
    } finally {
      if (previous === undefined) delete process.env[sessions.SESSION_FILE_ENV]
      else process.env[sessions.SESSION_FILE_ENV] = previous
    }

    // An ISO expiry (a hand-written export) reads as the same instant, and an
    // absent profile is an empty string rather than a missing field: the
    // status document reports it as `appProfile`.
    const isoFile = join(dir, 'iso.info')
    writeFileSync(
      isoFile,
      JSON.stringify({
        version: 1,
        regions: { global: { token: 'dt-iso', refreshToken: '', expiresAt: '2099-01-01T00:00:00Z' } },
      }),
    )
    const iso = sessions.readSeededSession('global', { file: isoFile })
    assert.equal(iso.expiresAt, Date.parse('2099-01-01T00:00:00Z'))
    assert.equal(iso.profile, '')
    assert.equal(iso.region, 'global')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

console.log('Qoder desktop session tests OK')
