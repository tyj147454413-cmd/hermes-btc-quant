// dsh-web-restart — DeepSeek Harness (dsh) web plugin: a "Restart GUI" button
// at the sidebar foot whose click restarts this very dsh web server process.
//
// Host half. The browser half (dsh/client.js) renders the sidebar button and
// POSTs to /__dsh_restart/verify; that route replies 202 BEFORE tearing the
// process down, so a restarted page never sees a torn-down server. The actual
// restart is a self-signal: the supervisor that launched `dsh web` (a shell
// wrapper / claude session / launchd) observes our exit and brings it straight
// back up, the same mechanism the CLI uses for clean SIGTERM shutdown.
//
// This plugin mirrors the modlens bundle shape exactly:
//   - package.json `dsh.bundle.patch` -> mounts `dsh/index.js` as a host plugin
//   - package.json exports["./client"] -> `dsh/client.js` (browser bundle)
//   - cordis.patch.yml inserts the host row by package name.
// webServer exists only under the web profile and cordis has no optional-inject
// form for the top-level row, so the route rides a scoped `ctx.inject` like
// modlens does: the closure runs when the webServer service appears and never
// where it does not (headless is untouched).
//
// Trust: the dsh web server binds loopback by default, and the only writes the
// route accepts are same-origin JSON POSTs carrying our own plugin id. This
// mirrors the trust stance the in-repo config routes use for loopback writes.
import { fileURLToPath } from 'node:url'

export const name = 'dsh-web-restart'
export const inject = []

const EXPECTED_ID = 'dsh-web-restart'

/** Self-signal a clean process exit; the supervisor restarts `dsh web`. */
function performRestart() {
  // Give the HTTP layer a beat to flush the 202 first.
  setTimeout(() => {
    try {
      process.kill(process.pid, 'SIGTERM')
    } catch {
      // Fall back to a hard exit if signals are not available (very unusual).
      process.exit(0)
    }
  }, 400)
}

/**
 * Register verify-and-restart routes on the web server.
 *   GET  /__dsh_restart/status -> { ok: true, plugin, pid } for installer
 *        self-check and for the client's restart detection (the pid lets the
 *        browser tell the old process from the relaunched one).
 *   POST /__dsh_restart/verify  -> body { id: 'dsh-web-restart' }
 *        id mismatch        -> 400
 *        non-POST           -> 405
 *        accepted           -> 202 then process restart.
 */
function registerRoutes(ctx) {
  ctx.webServer.register({
    name: 'dsh-web-restart-status',
    kind: 'exact',
    path: '/__dsh_restart/status',
    handler: async (req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ ok: true, plugin: name, pid: process.pid }))
    },
  })

  ctx.webServer.register({
    name: 'dsh-web-restart',
    kind: 'exact',
    path: '/__dsh_restart/verify',
    handler: async (req, res) => {
      const send = (code, body) => {
        res.writeHead(code, { 'content-type': 'application/json' })
        res.end(JSON.stringify(body))
      }
      if (req.method !== 'POST') {
        send(405, { error: 'method not allowed' })
        return
      }
      let raw = ''
      try {
        for await (const chunk of req) {
          raw += chunk
          if (raw.length > 4096) throw new Error('request too large')
        }
      } catch (error) {
        send(400, { error: `bad request: ${error?.message ?? String(error)}` })
        return
      }
      let body
      try {
        body = JSON.parse(raw || '{}')
      } catch {
        send(400, { error: 'invalid json' })
        return
      }
      if (body?.id !== EXPECTED_ID) {
        send(400, { error: `unknown id (expected ${EXPECTED_ID})` })
        return
      }
      send(202, { ok: true, restarting: true })
      performRestart()
    },
  })
}

export function apply(ctx) {
  if (typeof ctx.inject === 'function') {
    ctx.inject(['webServer'], (scope) => {
      try {
        registerRoutes(scope)
      } catch (error) {
        console.error(`[dsh-web-restart] route registration skipped: ${error}`)
      }
    })
  }
}