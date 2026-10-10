// dsh-web-restart — browser half. Renders a "Restart GUI" button at the
// sidebar foot (sidebar.footer.action list slot) and POSTs to the host route
// /__dsh_restart/verify on click.
//
// Hand-written in the lazy-CJS bundle protocol (window.__ModuleLoader__.load
// with a factory returning cordis-plugin exports), same zero-build / zero-dsh
// dependency stance as the modlens client bundle. React and the ui-primitives
// namespace are provided by the web client module loader at runtime, so this
// file neither imports them at build time nor ships them.
//
// Safety: the button is intentionally labelled exactly "Restart GUI" and
// POSTing is the ONLY action — nothing destructive happens client-side. The
// host route owns verification and the actual process restart.
//
// Restart detection: the 202 alone only says "the host accepted the signal".
// The old process still answers for ~400ms (grace flush) and launchd needs
// several seconds to bring the new one up, so the button baselines the host
// pid before POSTing and, while 'restarting', polls /__dsh_restart/status
// every second. A DIFFERENT pid answering means the restart completed ->
// window.location.reload(); the mount-time sessionStorage check then flashes
// "Restarted". No new pid within MAX_RESTART_SECONDS is a genuine failure.

window.__ModuleLoader__.load({
  id: 'dsh-web-restart',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports

    var SLOT = 'sidebar.footer.action'
    var ROUTE = '/__dsh_restart/verify'
    var STATUS = '/__dsh_restart/status'
    var TRIGGER_KEY = 'dsh-web-restart-triggered-at'
    var SUCCESS_FLASH_MS = 5000
    // Circuit breaker, generously padded: launchd's ThrottleInterval (10s)
    // plus a cold boot can push a back-to-back restart past 30s, and a false
    // "failed" on a restart that is still in flight is exactly the bug this
    // button exists not to have.
    var MAX_RESTART_SECONDS = 60
    var TEXT = {
      en: {
        label: 'Restart GUI',
        restarting: 'Restarting',
        restartingSuffix: 's',
        failNote: 'Restart request failed',
        successNote: 'Restarted',
      },
      zh: {
        label: '重启界面',
        restarting: '正在重启',
        restartingSuffix: '秒',
        failNote: '重启请求失败',
        successNote: '已重启',
      },
    }

    function labels() {
      var lang = (document.documentElement.lang || navigator.language || 'en').toLowerCase()
      return lang.indexOf('zh') === 0 ? TEXT.zh : TEXT.en
    }

    // One-time inject: spin keyframes + button hover/state styles. The web
    // shell does not hand this plugin a stylesheet channel, so the button
    // owns its own <style> tag and self-removes on module teardown.
    function ensureStyles() {
      if (document.getElementById('dsh-web-restart-styles')) return
      var style = document.createElement('style')
      style.id = 'dsh-web-restart-styles'
      style.textContent = [
        '@keyframes dsh-wr-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }',
        '@keyframes dsh-wr-flash { 0% { background-color: rgba(46, 160, 67, 0.25); } 100% { background-color: transparent; } }',
        '.dsh-wr-btn { transition: background-color 120ms ease, color 120ms ease; }',
        '.dsh-wr-btn:hover:not(:disabled) { background-color: rgba(255, 255, 255, 0.06); color: var(--dsw-alias-label-primary, #ddd); }',
        '.dsh-wr-btn:disabled { cursor: not-allowed; }',
        '.dsh-wr-btn[data-state="restarting"] { color: var(--dsw-alias-label-primary, #ddd); }',
        '.dsh-wr-btn[data-state="restarting"] .dsh-wr-icon { animation: dsh-wr-spin 0.9s linear infinite; display: inline-block; }',
        '.dsh-wr-btn[data-state="failed"] { color: #f85149; }',
        '.dsh-wr-btn[data-state="success"] { color: #2ea043; animation: dsh-wr-flash 800ms ease-out; }',
      ].join('\n')
      document.head.appendChild(style)
    }

    function readTriggerTs() {
      try {
        var raw = window.sessionStorage.getItem(TRIGGER_KEY)
        if (!raw) return 0
        var n = Number(raw)
        return Number.isFinite(n) ? n : 0
      } catch (_) {
        return 0
      }
    }
    function writeTriggerTs(ts) {
      try { window.sessionStorage.setItem(TRIGGER_KEY, String(ts)) } catch (_) { /* ignore */ }
    }

    function RestartButton(react, localeRef) {
      var h = react.createElement
      var useState = react.useState
      var useEffect = react.useEffect
      var useRef = react.useRef
      var useCallback = react.useCallback

      return function RestartButtonInner(_props) {
        ensureStyles()
        var active = typeof localeRef === 'function' ? localeRef() : null
        var t = labels(active)

        // 'idle' | 'restarting' | 'failed' | 'success'
        var stateBox = useState('idle')
        var state = stateBox[0]
        var setState = stateBox[1]
        var elapsedBox = useState(0)
        var elapsed = elapsedBox[0]
        var setElapsed = elapsedBox[1]
        var timerRef = useRef(null)
        var successTimerRef = useRef(null)
        // Host pid captured before the restart request; the poll loop only
        // trusts an answer from a DIFFERENT pid (the old process keeps
        // answering during its ~400ms grace flush).
        var prePidRef = useRef(null)
        // Fallback signal when no pid baseline exists: the route was seen
        // going down (network error) before coming back ok.
        var sawDownRef = useRef(false)
        // Latches the outcome so a late poll cannot reload the page after
        // the circuit breaker already flipped the button to 'failed'.
        var doneRef = useRef(false)

        // Detect a recent restart on mount: sessionStorage survives the page
        // reload because the host serves the same origin, so this is a clean
        // signal that the just-reloaded page is the result of a click we
        // already sent.
        useEffect(function () {
          var triggeredAt = readTriggerTs()
          if (!triggeredAt) return
          var ageMs = Date.now() - triggeredAt
          // Only celebrate within a sane window — a stale trigger from a prior
          // tab/session should not light up green.
          if (ageMs > MAX_RESTART_SECONDS * 1000) return
          setState('success')
          successTimerRef.current = setTimeout(function () {
            setState('idle')
            writeTriggerTs(0)
          }, SUCCESS_FLASH_MS)
          return function () {
            if (successTimerRef.current !== null) clearTimeout(successTimerRef.current)
          }
        }, [])

        // While 'restarting': tick the elapsed counter every second and poll
        // the status route. The tick doubles as the circuit breaker — no new
        // pid within MAX_RESTART_SECONDS flips to 'failed' so the user is
        // never stuck on a spinning spinner.
        useEffect(function () {
          if (state !== 'restarting') return undefined
          var ticks = 0
          timerRef.current = setInterval(function () {
            ticks += 1
            setElapsed(ticks)
            if (ticks >= MAX_RESTART_SECONDS) {
              doneRef.current = true
              writeTriggerTs(0)
              setState('failed')
              if (timerRef.current !== null) clearInterval(timerRef.current)
              timerRef.current = null
              return
            }
            fetch(STATUS, { cache: 'no-store' })
              .then(function (res) {
                if (!res.ok) return null
                return res.json()
              })
              .then(function (body) {
                if (doneRef.current || !body || !body.ok) return
                var pid = body.pid
                var back
                if (typeof pid === 'number' && typeof prePidRef.current === 'number') {
                  back = pid !== prePidRef.current
                } else {
                  // No pid signal (older host half, or the baseline fetch
                  // failed): settle for seeing the route go down and come
                  // back.
                  back = sawDownRef.current
                }
                if (!back) return
                doneRef.current = true
                if (timerRef.current !== null) clearInterval(timerRef.current)
                timerRef.current = null
                window.location.reload()
              })
              .catch(function () {
                // The route refusing connections is the server being down —
                // exactly what a restart in progress looks like.
                sawDownRef.current = true
              })
          }, 1000)
          return function () {
            if (timerRef.current !== null) clearInterval(timerRef.current)
            timerRef.current = null
          }
        }, [state])

        var onClick = useCallback(function () {
          if (state === 'restarting' || state === 'success') return
          setState('restarting')
          setElapsed(0)
          writeTriggerTs(Date.now())
          prePidRef.current = null
          sawDownRef.current = false
          doneRef.current = false
          // Baseline the host pid BEFORE sending the restart request, so the
          // poll loop above can tell the old process from the new one.
          fetch(STATUS, { cache: 'no-store' })
            .then(function (res) {
              if (!res.ok) return null
              return res.json()
            })
            .catch(function () { return null })
            .then(function (body) {
              if (body && typeof body.pid === 'number') prePidRef.current = body.pid
              return fetch(ROUTE, {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ id: 'dsh-web-restart' }),
              })
            })
            .then(function (res) {
              if (res.status === 202) {
                // Accepted: server is restarting. The poll loop reloads the
                // page once the new pid answers; the mount check then flashes
                // success.
                return
              }
              // 200/4xx/5xx means the host explicitly refused the restart.
              // Reset the trigger so a stale sessionStorage does not light up
              // green on the next reload.
              doneRef.current = true
              writeTriggerTs(0)
              setState('failed')
              console.error('[dsh-web-restart] restart refused', res.status)
            })
            .catch(function () {
              // Network error here is almost always because the server is
              // tearing itself down in response to our POST. Stay on
              // 'restarting' — the poll loop decides between the reload and
              // the 30s circuit breaker.
            })
        }, [state])

        var label
        var icon
        if (state === 'success') {
          label = (t && t.successNote) || 'Restarted'
          icon = '✓'
        } else if (state === 'failed') {
          label = (t && t.failNote) || 'Restart request failed'
          icon = '⚠'
        } else if (state === 'restarting') {
          label = (t && t.restarting) || 'Restarting'
          icon = '⟳'
        } else {
          label = (t && t.label) || 'Restart GUI'
          icon = '⟳'
        }

        var suffix = state === 'restarting' ? (' ' + elapsed + (t && t.restartingSuffix) || 's') : ''

        var btnColor
        if (state === 'success') btnColor = '#2ea043'
        else if (state === 'failed') btnColor = '#f85149'
        else if (state === 'restarting') btnColor = 'var(--dsw-alias-label-primary, #ddd)'
        else btnColor = 'var(--dsw-alias-label-secondary, #888)'

        return h(
          'button',
          {
            type: 'button',
            className: 'dsh-wr-btn',
            'data-state': state,
            disabled: state === 'restarting' || state === 'success',
            onClick: onClick,
            title: state === 'success'
              ? ((t && t.successNote) || 'Restarted')
              : state === 'failed'
                ? ((t && t.failNote) || 'Restart request failed')
                : ((t && t.label) || 'Restart GUI'),
            style: {
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              width: '100%',
              background: 'transparent',
              border: 'none',
              color: btnColor,
              font: 'inherit',
              fontSize: 12,
              lineHeight: '36px',
              padding: '0 12px',
              cursor: state === 'restarting' || state === 'success' ? 'not-allowed' : 'pointer',
              whiteSpace: 'nowrap',
            },
          },
          h(
            'span',
            { className: 'dsh-wr-icon', 'aria-hidden': true, style: { display: 'inline-block', width: 14, textAlign: 'center' } },
            icon
          ),
          h(
            'span',
            { className: 'dsh-wr-label' },
            label + suffix
          )
        )
      }
    }

    // `slots` is an optional service on the web client plugin graph: the plugin
    // must resolve it through the scoped inject (ctx.inject(['slots'], cb)) —
    // the apply ctx does not expose ctx.slots directly. Matching the modlens
    // client bundle, which resolves the same optional slot system this way.
    function mountButton(scope) {
      var react
      try {
        react = require('react')
      } catch (error) {
        console.error(`[dsh-web-restart] sidebar button skipped: ${error}`)
        return
      }
      var Button = RestartButton(react, localeRef)
      scope.slots.inject(SLOT, function* () {
        yield scope.slots.register(
          { name: SLOT, id: 'dsh-web-restart', key: 'dsh-web-restart', order: 10, label: 'Restart GUI' },
          Button
        )
      })
    }

    // Only mount the button when the host actually serves the restart route:
    // a 404 means no host half (headless, older profile), where the button
    // would be dead weight. A network/5xx failure is treated as absent too,
    // conservative in the safe direction (no button beats a button that
    // cannot do anything).
    function registerButton(ctx) {
      ctx.inject(['slots'], (scope) => {
        fetch(STATUS)
          .then(function (res) {
            if (res.status === 404) return
            if (!res.ok) return
            try {
              mountButton(scope)
            } catch (error) {
              console.error(`[dsh-web-restart] sidebar button skipped: ${error}`)
            }
          })
          .catch(function () {
            /* host absent — leave the sidebar untouched */
          })
      })
    }

    // Resolve the active interface language lazily if the locale service is
    // present; absent, fall back to document/navigator only.
    var localeRef = null
    // eslint-disable-next-line no-unused-vars
    var react = null

    function apply(ctx) {
      // Try to capture dsh's locale service for label language.
      if (ctx && typeof ctx.get === 'function') {
        try {
          var svc = ctx.get('locale')
          if (svc && typeof svc.active === 'function') {
            localeRef = function () { return svc.active() }
          }
        } catch { /* no locale service -> skip */ }
      }
      registerButton(ctx)
      if (typeof ctx.effect === 'function') {
        ctx.effect(
          () => () => { /* dispose is automatic on plugin teardown */ },
          'dsh-web-restart: sidebar button'
        )
      }
    }

    exports.apply = apply
    exports.inject = []
    return module.exports
  },
})
