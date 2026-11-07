#!/usr/bin/env node
/**
 * The Qoder desktop app's own sign-in, read where it is kept.
 *
 * A Qoder account does not have to be a pasted PAT.  The desktop app signs in
 * with a browser OAuth device flow and keeps the result beside its profile:
 *
 *   macOS    ~/Library/Application Support/com.qodercn.app.stable/auth.v1.dat
 *            key: login keychain item "<product> App Safe Storage"
 *   Windows  %APPDATA%\com.qodercn.app.stable\auth.v1.dat
 *            key: Local State -> os_crypt.encrypted_key (DPAPI, current user)
 *
 * The file is an Electron/Chromium OSCrypt envelope (`v10` + ciphertext) whose
 * plaintext is
 *
 *   {schemaVersion, authenticationContext, token: "dt-…", refreshToken: "drt-…",
 *    expiresAt, refreshTokenExpiresAt, user: {id, name, …}}
 *
 * and `authenticationContext` names the gateway the session belongs to, so the
 * region is read from the file rather than guessed from the directory name.
 *
 * `dt-` is a device access token (30 days) and `drt-` its refresh token (one
 * year).  The cosy gateway accepts a device token as the account identity, so
 * the worker can serve chat and the model catalog with it — see
 * `qoder_worker.mjs` for how the upstream client is handed that identity
 * without reimplementing any of the protocol here.
 *
 * Everything in this module is read-only: no token is written anywhere, no
 * copy of the app's file is kept, and a decrypted session lives only in the
 * worker's memory (the caller decides for how long).
 *
 * macOS keychain access: `security find-generic-password` addresses one item by
 * service.  The first call from a new binary may show the keychain's own
 * permission sheet; a refusal is answered as "no session", never as a wrong
 * token.
 *
 * @module young_router/qoder_app_session
 */

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { createDecipheriv, pbkdf2Sync } from 'node:crypto'
import { homedir } from 'node:os'
import { join } from 'node:path'

const execFileAsync = promisify(execFile)

/** Where each region's app keeps its profile, when the bundle id is known. */
const KNOWN_PROFILES = Object.freeze([
  // The CN build ("Qoder CN"), product name used in its keychain service.
  { region: 'cn', dir: 'com.qodercn.app.stable', products: ['Qoder CN', 'QoderCN'] },
  // The international build.  Its profile directory is discovered by scan as
  // well, because the naming has changed between releases.
  { region: 'global', dir: 'com.qoder.app.stable', products: ['Qoder'] },
])

/** Chromium's fixed parameters for its Keychain-derived key. */
const CHROMIUM_SALT = 'saltysalt'
const CHROMIUM_ITERATIONS = 1003
const CHROMIUM_CBC_IV = Buffer.alloc(16, ' ')
/** The envelope's own prefix: three ASCII bytes of key version. */
const V10_PREFIX = Buffer.from('v10', 'ascii')

/** One region a session's own `authenticationContext` states. */
export function regionOfAuthenticationContext(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value ?? '')
  if (text.includes('qoder.com.cn')) return 'cn'
  if (text.includes('qoder.sh') || text.includes('qoder.com')) return 'global'
  return undefined
}

/**
 * The region a profile directory names, as a fallback only.
 *
 * The CN build ships as `com.qodercn.app.stable`; every other Qoder profile id
 * is the international product.  Anything else answers `undefined` rather than
 * defaulting to a gateway: a session filed under the wrong region fails as
 * "this account is broken" instead of "sign in".
 */
export function regionOfProfileDirectory(directory) {
  if (directory.includes('qodercn')) return 'cn'
  if (/^com\.qoder(\.|$)/.test(directory)) return 'global'
  return undefined
}

function applicationSupportRoot(platform, home) {
  if (platform === 'darwin') return join(home, 'Library', 'Application Support')
  if (platform === 'win32') return process.env.APPDATA || join(home, 'AppData', 'Roaming')
  return ''
}

/**
 * Every profile that carries an `auth.v1.dat`, in a stable order.
 *
 * The known bundle ids come first so a machine with several Qoder builds
 * answers deterministically; a directory the list does not name is still
 * found by the scan, which is what keeps a renamed profile working.
 */
export function profileCandidates({ platform = process.platform, home = homedir() } = {}) {
  const root = applicationSupportRoot(platform, home)
  if (root === '') return []
  const entries = []
  const seen = new Set()
  const push = (directory, products, regionHint) => {
    const profile = join(root, directory)
    if (seen.has(profile) || !existsSync(join(profile, 'auth.v1.dat'))) return
    seen.add(profile)
    entries.push({ directory, profile, products, regionHint })
  }
  for (const known of KNOWN_PROFILES) push(known.dir, known.products, known.region)
  let names = []
  try {
    names = readdirSync(root).sort()
  } catch {
    names = []
  }
  for (const name of names) {
    if (!/^com\.qoder/i.test(name)) continue
    push(name, KNOWN_PROFILES.find((entry) => entry.dir === name)?.products ?? ['Qoder CN', 'Qoder'], undefined)
  }
  return entries
}

/** One keychain service name to try, per product of one profile. */
function keychainServices(product) {
  return [`${product} App Safe Storage`, `${product} Safe Storage`]
}

async function keychainPassword(service) {
  try {
    const { stdout } = await execFileAsync('/usr/bin/security', [
      'find-generic-password',
      '-s',
      service,
      '-w',
    ])
    return stdout.trim()
  } catch {
    return ''
  }
}

/** The Chromium key a Keychain password derives (AES-128-CBC, `saltysalt`). */
export function chromiusKeyFromPassword(password) {
  return pbkdf2Sync(password, CHROMIUM_SALT, CHROMIUM_ITERATIONS, 16, 'sha1')
}

/**
 * Decrypt one OSCrypt `v10` envelope.
 *
 * Two layouts are accepted because the two platforms differ: macOS derives an
 * AES-128-CBC key from its Keychain password and pads with PKCS#7, while a
 * modern Windows profile hands out a 32-byte key used with AES-256-GCM
 * (`v10` + 12-byte nonce + ciphertext + 16-byte tag).  A blob that matches
 * neither answers `undefined` rather than a half-decoded buffer.
 */
export function decryptEnvelope(blob, key) {
  if (!Buffer.isBuffer(blob) || blob.length < 19) return undefined
  const offset = blob.subarray(0, 3).equals(V10_PREFIX) ? 3 : 0
  const body = blob.subarray(offset)
  try {
    if (key.length === 16) {
      if (body.length % 16 !== 0) return undefined
      const decipher = createDecipheriv('aes-128-cbc', key, CHROMIUM_CBC_IV)
      const plain = Buffer.concat([decipher.update(body), decipher.final()])
      return plain
    }
    if (key.length === 32) {
      if (body.length < 16 + 16) return undefined
      const nonce = body.subarray(0, 12)
      const tag = body.subarray(body.length - 16)
      const ciphertext = body.subarray(12, body.length - 16)
      const decipher = createDecipheriv('aes-256-gcm', key, nonce)
      decipher.setAuthTag(tag)
      return Buffer.concat([decipher.update(ciphertext), decipher.final()])
    }
  } catch {
    return undefined
  }
  return undefined
}

function parseSession(plain) {
  if (!plain) return undefined
  let document
  try {
    document = JSON.parse(plain.toString('utf8'))
  } catch {
    return undefined
  }
  if (document === null || typeof document !== 'object') return undefined
  const token = typeof document.token === 'string' ? document.token : ''
  const refreshToken = typeof document.refreshToken === 'string' ? document.refreshToken : ''
  if (token === '' && refreshToken === '') return undefined
  const user = document.user !== null && typeof document.user === 'object' ? document.user : {}
  const expiresAt = typeof document.expiresAt === 'string' ? Date.parse(document.expiresAt) : 0
  return {
    token,
    refreshToken,
    expiresAt: Number.isFinite(expiresAt) ? expiresAt : 0,
    userId: typeof user.id === 'string' ? user.id : '',
    userName: typeof user.name === 'string' ? user.name : '',
    userEmail: typeof user.email === 'string' ? user.email : '',
    context: typeof document.authenticationContext === 'string' ? document.authenticationContext : '',
    region: regionOfAuthenticationContext(document.authenticationContext),
  }
}

/** The DPAPI-wrapped Chromium key one Windows profile stores. */
async function windowsChromiumKey(profile) {
  let state
  try {
    state = JSON.parse(readFileSync(join(profile, 'Local State'), 'utf8'))
  } catch {
    return undefined
  }
  const encoded = state?.os_crypt?.encrypted_key
  if (typeof encoded !== 'string' || encoded === '') return undefined
  const blob = Buffer.from(encoded, 'base64')
  // "DPAPI" is Chromium's own marker in front of the protected bytes.
  const protectedBytes = blob.subarray(0, 5).toString('ascii') === 'DPAPI' ? blob.subarray(5) : blob
  try {
    const script = [
      '$b=[Convert]::FromBase64String($args[0]);',
      "[Security.Cryptography.ProtectedData]::Unprotect($b,$null,'CurrentUser')",
      ' | ForEach-Object { [Convert]::ToBase64String($_) }',
    ].join('')
    const { stdout } = await execFileAsync(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-Command', script, protectedBytes.toString('base64')],
      { windowsHide: true },
    )
    const key = Buffer.from(stdout.trim(), 'base64')
    return key.length === 16 || key.length === 32 ? key : undefined
  } catch {
    return undefined
  }
}

/**
 * Read one profile's sign-in, or `undefined` when it cannot be read.
 *
 * `undefined` covers every reason a session is not usable: no file, no key,
 * an envelope that does not decrypt, and a session whose region cannot be
 * stated.  `platform`, `home`, and `keychain` are seams: the tests drive the
 * whole envelope, the region decision and the "keychain refused" answer
 * without a desktop app installed.
 */
export async function readProfileSession(profile, options = {}) {
  const platform = options.platform ?? process.platform
  const readKeychain = options.keychain ?? keychainPassword
  const path = join(profile.profile, 'auth.v1.dat')
  let blob
  try {
    blob = readFileSync(path)
  } catch {
    return undefined
  }
  const keys = []
  if (platform === 'win32') {
    const key = await windowsChromiumKey(profile.profile)
    if (key) keys.push(key)
  } else if (platform === 'darwin') {
    for (const product of profile.products) {
      for (const service of keychainServices(product)) {
        const password = await readKeychain(service)
        if (password === '') continue
        keys.push(chromiusKeyFromPassword(password))
      }
    }
  }
  for (const key of keys) {
    const session = parseSession(decryptEnvelope(blob, key))
    if (session === undefined) continue
    const region = session.region ?? profile.regionHint ?? regionOfProfileDirectory(profile.directory)
    // A session this module cannot attribute to a gateway is not a session it
    // has: filing it under whichever region the caller happened to ask about
    // fails later as "this account is broken" instead of "sign in".
    if (region === undefined) return undefined
    return { ...session, region, profile: profile.directory }
  }
  return undefined
}

/**
 * Every session this machine's desktop apps are signed into, by region.
 *
 * A profile whose region cannot be stated is skipped: a token filed under the
 * wrong gateway is worse than no token, because it fails as "the account is
 * broken" instead of "sign in".
 */
export async function readDesktopSessions(options = {}) {
  const sessions = {}
  for (const profile of profileCandidates(options)) {
    const session = await readProfileSession(profile, options)
    if (session === undefined || !session.region) continue
    if (sessions[session.region] === undefined) sessions[session.region] = session
  }
  return sessions
}

/** The session one region's desktop app is signed into, or `undefined`. */
export async function readDesktopSession(region, options = {}) {
  const sessions = await readDesktopSessions(options)
  return sessions[region]
}

/**
 * Refresh an expired device token over the openapi surface.
 *
 * `POST {openapi}/api/v1/deviceToken/refresh` takes the long-lived `drt-`
 * refresh token and answers a new `dt-` (the same call the desktop app makes
 * when it wakes up).  No cosy signature is involved: it is the plain bearer
 * surface.  Answers `undefined` when the pair is refused — the caller then
 * reports the app as signed out and the user signs in there again.
 */
export async function refreshDesktopSession(session, openapi, options = {}) {
  const fetchImpl = options.fetch ?? globalThis.fetch
  if (typeof session?.refreshToken !== 'string' || !session.refreshToken.startsWith('drt-')) {
    return undefined
  }
  try {
    const response = await fetchImpl(`${openapi}/api/v1/deviceToken/refresh`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify({ refresh_token: session.refreshToken }),
    })
    if (!response.ok) return undefined
    const document = await response.json()
    const token = typeof document?.token === 'string' ? document.token : ''
    if (token === '') return undefined
    const expiresIn = Number(document?.expires_in)
    return {
      ...session,
      token,
      refreshToken:
        typeof document?.refresh_token === 'string' && document.refresh_token !== ''
          ? document.refresh_token
          : session.refreshToken,
      expiresAt: Number.isFinite(expiresIn) && expiresIn > 0
        ? Date.now() + expiresIn
        : session.expiresAt,
    }
  } catch {
    return undefined
  }
}

/** The cosy gateway one region's session is signed in to. */
export const OPENAPI_BY_REGION = Object.freeze({
  cn: 'https://openapi.qoder.com.cn',
  global: 'https://openapi.qoder.sh',
})

/** The bundle id each region's desktop app installs under. */
const APP_BUNDLE_IDS = Object.freeze({
  cn: ['com.qodercn.app', 'com.aliyun.lingma.ide'],
  global: ['com.qoder.app'],
})

/**
 * The desktop app one region's sign-in belongs to, when it is installed.
 *
 * The pane offers to open this for a signed-out region: the sign-in happens in
 * that app, and this one can only hand the user over.  Discovery reads the
 * bundle id rather than a product name, because the CN build has been shipped
 * under two names and a guessed path finds neither.
 */
export function desktopApp(region, { platform = process.platform, home = homedir() } = {}) {
  const bundleIds = APP_BUNDLE_IDS[region] ?? []
  const roots = platform === 'darwin'
    ? ['/Applications', join(home, 'Applications')]
    : []
  for (const root of roots) {
    let names = []
    try {
      names = readdirSync(root).filter((name) => name.endsWith('.app')).sort()
    } catch {
      names = []
    }
    for (const name of names) {
      const bundle = join(root, name)
      const info = join(bundle, 'Contents', 'Info.plist')
      if (!existsSync(info)) continue
      let manifest = ''
      try {
        manifest = readFileSync(info, 'utf8')
      } catch {
        continue
      }
      if (!bundleIds.some((id) => manifest.includes(`<string>${id}</string>`))) continue
      return { appName: name.replace(/\.app$/, ''), bundle, product: region === 'cn' ? 'Qoder CN' : 'Qoder' }
    }
  }
  return undefined
}
