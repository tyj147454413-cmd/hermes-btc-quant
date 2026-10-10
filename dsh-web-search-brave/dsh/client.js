// dsh-web-search-brave — browser half. Replaces the shipped, hardcoded
// "DeepSeek 搜索提供方" web-search settings card with a Brave provider card:
//   * shadows slot 'settings.plugin.item' cell 'web-search' (priority -1,
//     lowest renders) so the DeepSeek card disappears;
//   * registers its own 'web-search-brave' card showing the Brave provider,
//     the BRAVE_API_KEY credential state (credentials.describe), and a key
//     input that writes the credential (credentials.set).
//
// Hand-written in the lazy-CJS bundle protocol (window.__ModuleLoader__.load
// with a factory returning cordis-plugin exports), same zero-build stance as
// the modlens / dsh-web-restart client bundles. React is provided by the web
// client module loader at runtime (require('react')), the slots service is
// resolved through ctx.inject(['slots']), and the connection handle is read
// lazily per call via ctx.get('connection').

window.__ModuleLoader__.load({
  id: 'dsh-web-search-brave',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports

    var SLOT = 'settings.plugin.item'
    var REF = 'BRAVE_API_KEY'
    var STYLE_ID = 'dsh-web-search-brave-styles'

    var COPY = {
      en: {
        title: 'Web search',
        description: 'Brave search provider',
        keySet: 'A key is configured.',
        keyUnset: 'No key configured',
        apiKeyLabel: 'API Key',
        apiKeyPlaceholder: 'Paste your Brave Search API key',
        apiKeyHint: 'Not written to settings files. Leave empty to keep the current key.',
        save: 'Save',
        saving: 'Saving',
        saveFailed: 'Save failed',
        providerNote: 'Search backend: Brave Search API (api.search.brave.com)',
        expand: 'expand',
        collapse: 'collapse',
      },
      zh: {
        title: '网页搜索',
        description: 'Brave 搜索提供方',
        keySet: '已配置密钥',
        keyUnset: '未配置密钥',
        apiKeyLabel: 'API Key',
        apiKeyPlaceholder: '粘贴 Brave 搜索 API Key',
        apiKeyHint: '不写入设置文件。留空表示保持当前密钥。',
        save: '保存',
        saving: '保存中',
        saveFailed: '保存失败',
        providerNote: '搜索后端：Brave Search API（api.search.brave.com）',
        expand: '展开',
        collapse: '收起',
      },
    }

    function labels(activeLang) {
      var lang = String(activeLang || '').toLowerCase()
      return lang.indexOf('zh') === 0 ? COPY.zh : COPY.en
    }

    // One-time <style> injection; self-contained so the card needs no stylesheet
    // channel. Uses the shell's --dsw-alias tokens with fallbacks, like the
    // dsh-web-restart button, so it reads fine on the dark theme.
    function ensureStyles() {
      if (document.getElementById(STYLE_ID)) return
      var style = document.createElement('style')
      style.id = STYLE_ID
      style.textContent = [
        '.dsh-bsb-card { display: block; list-style: none; }',
        '.dsh-bsb-header { display: flex; align-items: center; gap: 8px; width: 100%; padding: 10px 12px; background: transparent; border: none; border-bottom: 1px solid rgba(128,128,128,0.18); color: var(--dsw-alias-label-primary, #ddd); font: inherit; text-align: left; cursor: pointer; }',
        '.dsh-bsb-headtext { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }',
        '.dsh-bsb-name { font-size: 13px; font-weight: 600; color: var(--dsw-alias-label-primary, #eee); }',
        '.dsh-bsb-desc { font-size: 12px; color: var(--dsw-alias-label-secondary, #999); }',
        '.dsh-bsb-badge { flex: none; font-size: 11px; padding: 1px 8px; border-radius: 999px; border: 1px solid rgba(128,128,128,0.35); color: var(--dsw-alias-label-secondary, #aaa); }',
        '.dsh-bsb-badge-ok { border-color: rgba(46,160,67,0.5); color: #2ea043; }',
        '.dsh-bsb-chevron { flex: none; font-size: 10px; color: var(--dsw-alias-label-secondary, #888); transition: transform 120ms ease; }',
        '.dsh-bsb-chevron-open { transform: rotate(180deg); }',
        '.dsh-bsb-body { padding: 12px; border-bottom: 1px solid rgba(128,128,128,0.18); }',
        '.dsh-bsb-label { display: block; font-size: 12px; color: var(--dsw-alias-label-secondary, #aaa); margin-bottom: 4px; }',
        '.dsh-bsb-input { width: 100%; box-sizing: border-box; padding: 6px 8px; border-radius: 6px; border: 1px solid rgba(128,128,128,0.4); background: rgba(128,128,128,0.08); color: var(--dsw-alias-label-primary, #ddd); font: inherit; font-size: 12px; }',
        '.dsh-bsb-input:focus { outline: none; border-color: rgba(88,140,255,0.8); }',
        '.dsh-bsb-hint { margin: 6px 0 0; font-size: 11px; color: var(--dsw-alias-label-secondary, #888); }',
        '.dsh-bsb-failed { margin: 8px 0 0; font-size: 11px; color: #f85149; }',
        '.dsh-bsb-footer { margin-top: 10px; text-align: right; }',
        '.dsh-bsb-save { padding: 5px 16px; border-radius: 6px; border: none; background: rgba(88,140,255,0.9); color: #fff; font: inherit; font-size: 12px; cursor: pointer; }',
        '.dsh-bsb-save:disabled { opacity: 0.5; cursor: not-allowed; }',
        '.dsh-bsb-note { margin: 10px 0 0; font-size: 11px; color: var(--dsw-alias-label-secondary, #888); }',
      ].join('\n')
      document.head.appendChild(style)
    }

    /** The Brave card component (react-bound factory). */
    function BraveCard(react, getApi, localeRef) {
      var h = react.createElement
      var useState = react.useState
      var useEffect = react.useEffect

      function statusOf(view) {
        if (!view) return 'unconfigured'
        return view.configured ? 'configured' : 'unconfigured'
      }

      return function BraveCardInner() {
        ensureStyles()
        var t = labels(typeof localeRef === 'function' ? localeRef() : null)

        var openBox = useState(false)
        var open = openBox[0]
        var setOpen = openBox[1]
        var keyBox = useState('')
        var keyText = keyBox[0]
        var setKeyText = keyBox[1]
        // 'unknown' | 'configured' | 'unconfigured'
        var statusBox = useState('unknown')
        var status = statusBox[0]
        var setStatus = statusBox[1]
        var savingBox = useState(false)
        var saving = savingBox[0]
        var setSaving = savingBox[1]
        var failedBox = useState(false)
        var failed = failedBox[0]
        var setFailed = failedBox[1]

        function refreshStatus() {
          var api = getApi()
          if (!api) return
          setStatus('unknown')
          api.credentials.describe({ refs: [REF] })
            .then(function (res) {
              var ok = res && res.result && res.result.ok
              if (!ok) return
              var creds = res.result.value && res.result.value.credentials
              setStatus(statusOf(creds ? creds[REF] : undefined))
            })
            .catch(function () {
              setStatus('unknown')
            })
        }

        useEffect(function () { refreshStatus() }, [])

        function onSave() {
          var api = getApi()
          if (!api || !keyText) return
          setSaving(true)
          setFailed(false)
          api.credentials.set({ ref: REF, value: keyText })
            .then(function (res) {
              setSaving(false)
              if (res && res.result && res.result.ok) {
                setKeyText('')
                refreshStatus()
              } else {
                setFailed(true)
              }
            })
            .catch(function () {
              setSaving(false)
              setFailed(true)
            })
        }

        var badge = status === 'configured'
          ? h('span', { className: 'dsh-bsb-badge dsh-bsb-badge-ok' }, t.keySet)
          : status === 'unconfigured'
            ? h('span', { className: 'dsh-bsb-badge' }, t.keyUnset)
            : null

        return h(
          'li',
          { className: 'dsh-bsb-card' },
          h(
            'button',
            {
              type: 'button',
              className: 'dsh-bsb-header',
              'aria-expanded': open,
              'aria-label': (open ? t.collapse : t.expand) + ': ' + t.description,
              onClick: function () {
                setOpen(!open)
                if (!open) refreshStatus()
              },
            },
            h(
              'span',
              { className: 'dsh-bsb-headtext' },
              h('span', { className: 'dsh-bsb-name' }, t.title),
              h('span', { className: 'dsh-bsb-desc' }, t.description)
            ),
            badge,
            h('span', { className: 'dsh-bsb-chevron' + (open ? ' dsh-bsb-chevron-open' : '') }, '▾')
          ),
          open
            ? h(
              'div',
              { className: 'dsh-bsb-body' },
              h('label', { className: 'dsh-bsb-label', htmlFor: 'dsh-bsb-key' }, t.apiKeyLabel),
              h('input', {
                id: 'dsh-bsb-key',
                type: 'password',
                className: 'dsh-bsb-input',
                placeholder: t.apiKeyPlaceholder,
                value: keyText,
                onChange: function (event) {
                  setKeyText(event.target.value)
                  setFailed(false)
                },
              }),
              h('p', { className: 'dsh-bsb-hint' }, t.apiKeyHint),
              failed ? h('p', { className: 'dsh-bsb-failed', role: 'status' }, t.saveFailed) : null,
              h(
                'div',
                { className: 'dsh-bsb-footer' },
                h(
                  'button',
                  { type: 'button', className: 'dsh-bsb-save', disabled: saving || !keyText, onClick: onSave },
                  saving ? t.saving : t.save
                )
              ),
              h('p', { className: 'dsh-bsb-note' }, t.providerNote)
            )
            : null
        )
      }
    }

    /** Null renderer: shadows the shipped DeepSeek web-search card. */
    function NullCard() {
      return null
    }

    function mountCards(scope, getApi, localeRef) {
      var react
      try {
        react = require('react')
      } catch (error) {
        console.error('[dsh-web-search-brave] settings card skipped: ' + error)
        return
      }
      var braveCard = BraveCard(react, getApi, localeRef)
      scope.slots.inject(SLOT, function* () {
        yield scope.slots.register(
          { name: SLOT, id: 'web-search', order: 20, priority: -1, label: 'web-search' },
          NullCard
        )
        yield scope.slots.register(
          { name: SLOT, id: 'web-search-brave', order: 20, label: 'Brave Search' },
          braveCard
        )
      })
    }

    var localeRef = null

    function apply(ctx) {
      if (ctx && typeof ctx.get === 'function') {
        try {
          var localeSvc = ctx.get('locale')
          if (localeSvc && typeof localeSvc.active === 'function') {
            localeRef = function () { return localeSvc.active() }
          }
        } catch (_) { /* no locale service -> browser-language fallback */ }
      }
      ctx.inject(['slots'], function (scope) {
        try {
          mountCards(scope, function () {
            try {
              var conn = ctx.get('connection')
              return conn ? conn.api : null
            } catch (_) {
              return null
            }
          }, localeRef)
        } catch (error) {
          console.error('[dsh-web-search-brave] settings cards skipped: ' + error)
        }
      })
    }

    exports.apply = apply
    exports.inject = []
    return module.exports
  },
})
