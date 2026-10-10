# dsh-web-search-brave

Brave Search API provider for the DeepSeek Harness web capability seam
(`ctx.web`). A self-contained dsh bundle: mounting it registers a
`WebSearchProvider` with id `brave` and repoints the `web` row's
`searchProvider` at it (last write wins over `dsh-base`'s `deepseek-official`),
so the model-facing `web_search` tool runs through the [Brave Search API](https://api-dashboard.search.brave.com/app/keys).

## Install

1. Place this package somewhere stable (it lives beside `dsh-web-restart` in
   `~/Desktop/hermes btc/`).
2. In `~/.dsh/profiles/web/package.json` add the dependency and the bundle:

   ```json
   "dependencies": {
     "dsh-web-search-brave": "file:/Users/tangyijun/Desktop/hermes btc/dsh-web-search-brave"
   },
   "dsh": { "profile": { "bundles": [ /* ... */, "dsh-web-search-brave" ] } }
   ```

3. `pnpm install` in the profile directory.
4. Store the key in `$DSH_HOME/.credentials.yaml`:

   ```yaml
   BRAVE_API_KEY: <your-key>
   ```

   The key is resolved per search through the credentials seam (`$DSH_HOME/.credentials.yaml`
   or the Web Models page credential store); a changed key reaches the next
   search with no restart. Without that seam it falls back to the environment
   variable of the same name.
5. Restart `dsh web` (launchd label `local.dsh.web` will bring it back up).

## Behavior

- Endpoint: `GET https://api.search.brave.com/res/v1/web/search` with header
  `X-Subscription-Token`; results map `title`/`url`/`description` → source
  `title`/`url`/`snippet`, and `page_age` → `publishedAt`.
- The Brave free tier allows ~1 request/second: bursts surface as HTTP 429 and
  propagate as `WEB_PROVIDER_ERROR`; the seam keeps the stable `web_search`
  schema either way.
- Zero runtime imports: plain ESM over global `fetch`, so the profile needs no
  `@deepseek-ai` dependency for this bundle.

## Rollback

Remove the bundle from `~/.dsh/profiles/web/package.json`, run `pnpm install`,
restart the server. The `web` row falls back to `searchProvider: deepseek-official`.
