// dsh-web-search-brave — DeepSeek Harness (dsh) web plugin: register a Brave
// Search API provider into the ctx.web search registry.
//
// Provider id: 'brave'. This bundle's own cordis.patch.yml also repoints the
// `web` row's searchProvider at 'brave' (last write wins over dsh-base's
// 'deepseek-official'), so provider selection is unambiguous.
//
// The credential is resolved per search (never cached): first through the
// mounted credentials seam by the reference `BRAVE_API_KEY` — which covers
// `$DSH_HOME/.credentials.yaml` and the Web Models page credential store —
// then, without that seam, from the process environment. Storing the key in
// .credentials.yaml means a key change reaches the next search with no
// restart, and the value never appears in a config file.
//
// Zero external imports on purpose: the provider is plain ESM over global
// fetch, so the bundle needs no @deepseek-ai dependency in the profile.
//
// Brave Search API notes (free tier): endpoint
// GET https://api.search.brave.com/res/v1/web/search, auth header
// X-Subscription-Token, ~1 request/second — bursts surface as HTTP 429 and
// propagate as WEB_PROVIDER_ERROR.

export const name = 'web-search-brave'

/** The web seam this provider registers into. */
export const inject = ['web']

/** Stable id this provider registers under. */
export const BRAVE_PROVIDER_ID = 'brave'

/** Brave Search web endpoint. */
export const BRAVE_DEFAULT_BASE_URL = 'https://api.search.brave.com/res/v1/web/search'

/** Credential reference resolved per search. */
export const BRAVE_API_KEY_ENV = 'BRAVE_API_KEY'

/** Attribution header sent on every request. */
const USER_AGENT = 'deepseek-harness/0.0.1'

/** Brave caps `count` at 20 per request; the seam enforces the real bound. */
const BRAVE_MAX_COUNT = 20

/** Tag an error with a seam-routable code, mirroring @deepseek-ai/dsh-web WebError. */
function webError(message, code, cause) {
  const error = new Error(message, cause !== undefined ? { cause } : undefined)
  error.code = code
  return error
}

/** One Brave-backed search provider. */
class BraveSearchProvider {
  constructor(options) {
    this.options = options
  }

  get id() {
    return BRAVE_PROVIDER_ID
  }

  /** Cheap local usability check; must not make network calls. */
  available() {
    return typeof this.options.resolveApiKey === 'function'
      && typeof this.options.baseURL === 'string'
      && this.options.baseURL.length > 0
  }

  /** Run one search against the Brave Search API. */
  async search(request, signal) {
    const apiKey = await this.options.resolveApiKey()
    if (apiKey === undefined || apiKey.length === 0) {
      throw webError(
        `Brave search credential resolution failed: no value for ${BRAVE_API_KEY_ENV}`,
        'WEB_PROVIDER_CREDENTIAL_MISSING',
      )
    }

    const params = new URLSearchParams({ q: request.query })
    if (request.maxResults !== undefined) {
      params.set('count', String(Math.min(request.maxResults, BRAVE_MAX_COUNT)))
    }

    let response
    try {
      response = await fetch(`${this.options.baseURL}?${params}`, {
        method: 'GET',
        redirect: 'error',
        headers: {
          'accept': 'application/json',
          'accept-encoding': 'gzip',
          'x-subscription-token': apiKey,
          'user-agent': USER_AGENT,
        },
        ...(signal !== undefined ? { signal } : {}),
      })
    } catch (error) {
      if (error?.name === 'AbortError') {
        throw webError('Brave search aborted', 'WEB_ABORTED', error)
      }
      throw webError(`Brave search request failed: ${String(error)}`, 'WEB_PROVIDER_ERROR', error)
    }

    if (!response.ok) {
      throw webError(
        `Brave search request failed: HTTP ${response.status} ${response.statusText}`,
        'WEB_PROVIDER_ERROR',
      )
    }

    let body
    try {
      body = await response.json()
    } catch (error) {
      throw webError(
        `Brave search returned an unprocessable response body: ${String(error)}`,
        'WEB_PROVIDER_ERROR',
        error,
      )
    }

    const sources = (body?.web?.results ?? [])
      .filter(result => typeof result?.url === 'string' && result.url.length > 0)
      .map(result => ({
        url: result.url,
        ...(typeof result.title === 'string' && result.title.length > 0 ? { title: result.title } : {}),
        ...(typeof result.description === 'string' && result.description.length > 0 ? { snippet: result.description } : {}),
        ...(typeof result.page_age === 'string' && result.page_age.length > 0 ? { publishedAt: new Date(result.page_age).toISOString() } : {}),
      }))

    // Brave returns no generated answer, so `content` is omitted. The seam
    // owns the final maxResults truncation; report truncated: false.
    return { sources, truncated: false }
  }
}

/** Register the Brave search provider with ctx.web. */
export function apply(ctx, config = {}) {
  const apiKeyEnv = typeof config.apiKeyEnv === 'string' && config.apiKeyEnv.length > 0
    ? config.apiKeyEnv
    : BRAVE_API_KEY_ENV

  ctx.web.registerSearchProvider(new BraveSearchProvider({
    baseURL: typeof config.baseURL === 'string' && config.baseURL.length > 0
      ? config.baseURL
      : BRAVE_DEFAULT_BASE_URL,
    resolveApiKey: async () => {
      const credentials = ctx.get('credentials')
      if (credentials !== undefined) {
        const resolved = await credentials.resolve(apiKeyEnv)
        return resolved?.value
      }
      // Without the credentials seam the environment is the whole plane.
      const ambient = process.env[apiKeyEnv]
      return ambient !== undefined && ambient.length > 0 ? ambient : undefined
    },
  }))
}
