# google_search: Google through the shared browser

`google_search` is for a user who explicitly asks for a Google search (「用 Google 搜」,
"google it"). Every other web lookup goes to `research`, which already runs its own
Google query (q4); the extension's `RESEARCH_FIRST` guard blocks `google_search` calls
without an explicit request.

## Flow

1. `index.ts` sends `google-search [{"direction", "query"}, …]` (1–4 directions) to the
   worker socket.
2. `worker.py` runs every query in parallel with `search_one`: a temporary background
   tab, `https://www.google.com/search?q=…&hl=zh-TW&gl=tw&filter=0`, wait until the
   page is ready, give up on Google's anti-bot or consent page, read the result cards
   with `pibrowser/js/google_results.js` (title, URL, snippet), close the tab. Each
   search has an 8 s limit.
3. The groups are merged round robin (every direction's 1st result, then its 2nd, …)
   into one de-duplicated top 10. Google's opaque `/goto` and `/url` links are first
   resolved to their 302 target with `research.jobs.resolve_google_redirect` (one
   request, no redirect followed; any failure keeps the link), so the same page under
   two links counts once.
4. The reply is Markdown (title, URL, direction and query, snippet) plus the rows and the
   per-direction results.

`research` uses the same `search_one` for its Google query and the same redirect
resolution (`research/providers.py`); see
[research-streaming-pipeline.md](research-streaming-pipeline.md).
