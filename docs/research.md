# Research: TRMNL plugin "LLM Leaderboards"

Date: 2026-09-11. Every source below was probed live (`curl`, Python) unless marked "not verified".
Sizes and timings are from Paris on that day.

## 1. Decisions

- **Plugin type**: TRMNL *Private Plugin*, **Polling** strategy plus a **Serverless** function
  (Python). TRMNL polls a 50-byte pointer; the function fetches the selected leaderboards.
- **Sources kept**: Arena (GitHub mirror), Epoch AI (Capabilities Index + benchmark tables),
  LiveBench (site CSV), Artificial Analysis (free API, key required).
- **Local dev**: `trmnlp` (Ruby gem `trmnl_preview` 0.12, Ruby >= 4.0, or Docker image `trmnl/trmnlp`).

## 2. Why the function fetches

TRMNL polling URLs support the full Liquid library (`{% for %}`, `date` filters, form fields),
so a URL per selected board is possible. Two chains make it insufficient:

- Arena snapshots live at `data/<YYYY-MM-DD>/<board>.json`; `data/latest.json` says which date.
  A date computed in Liquid ("yesterday") works most days, but the mirror skipped 4 days between
  2026-03-19 and 2026-08-07, and the behaviour of TRMNL when one of several polling URLs returns
  404 is undocumented.
- Epoch's per-benchmark tables are only published inside `benchmark_data.zip`.

Also, `trmnlp` (which mirrors production) drops polled responses with an unknown content type
such as `text/csv`, and exposes `text/plain` bodies as `{"data": "<text>"}` only after a JSON
sniff. Fetching in the function means the exact same code path runs locally and on TRMNL.

Costs: TRMNL's checker (Chef) hints against HTTP calls in Serverless functions, and every call
counts against the 5 s limit. Mitigation: at most 2 calls per board, 4 s budget, per-request
timeout, per-board errors. Measured: 3 boards from 3 sources in 0.3 to 1.0 s.

The polled pointer is still useful: with a single polling URL TRMNL merges its JSON at the top
level (`date`, `path`), and the function seeds its cache with it (one call saved per Arena board).
If TRMNL does not recognise the `text/plain` JSON, the function fetches the pointer itself.

## 3. Sources kept

### Arena (arena.ai, formerly LMArena, renamed January 2026)

- No official API ("Please expose API endpoint" thread on the Hugging Face space, unanswered).
- Mirror: https://github.com/oolong-tea-2026/arena-ai-leaderboards (MIT). GitHub Action at
  01:37 UTC daily (observed `fetched_at` 06:36 UTC), pages read through Jina Reader and parsed
  by an LLM. Files: `data/latest.json` (`{"date","path"}`), `data/<date>/{text,code,vision,
  search,document,agent,text-to-image,...}.json`, 4 to 15 KB each, `application/json` via
  jsDelivr, `text/plain` via raw.githubusercontent.com.
- Schema: `meta.{leaderboard,source_url,fetched_at,last_updated ("Sep 8, 2026"),model_count}`,
  `models[].{rank,model,vendor,license ("proprietary"|"open"|null),score,ci,votes}`. The `agent`
  board has a different schema (`scores[]` with several dimensions) and is not offered.
- Hosted endpoint `api.wulong.dev/arena-ai-leaderboards/v1/leaderboard?name=text`: HTTP 429
  (Cloudflare error 1027, free-tier worker limit) on first call. Not used.

### Epoch AI, Capabilities & Benchmarking hub (CC BY 4.0)

- `https://epoch.ai/data/eci_scores.csv` (33 KB, `text/csv`, 264 models): `Model`, `Display name`,
  `eci`, `eci_ci_low/high`, `date`, `Organization`, `Accessibility group` (Open/Closed weights).
- `https://epoch.ai/data/benchmark_data.zip` (2.3 MB, 0.3 s; URL hard-coded in Epoch's own
  `eci-public` repository): ~80 CSVs, one per benchmark. Epoch-run tables have
  `Model version, mean_score, Best score (across scorers), Release date, Organization, ...,
  Started at`; external tables vary (`Accuracy`, `Score`, `Accuracy mean`, `Percent correct`...).
  `benchmark_metadata.csv` gives `source_file`, `score_column` and `scale` (1.0 = fractions,
  0.01 = percentages); `model_metadata.csv` maps `model_version` (e.g. `claude-fable-5-1_xhigh`)
  to `model_group` ("Claude Fable 5.1"), `display_name`, `organization`, `accessibility`.
- The function collapses rows to one per `model_group` (best score), which matches Epoch's own
  leaderboard pages ("best-performing agent-model combination for each model version").
  Terminal-Bench has up to 11 rows per model (one per agent).
- Individual CSVs are not served outside the zip (`/data/gpqa_diamond.csv` → 404).
- METR shows the 50 % time horizon in minutes; the board converts to hours.
- `live_bench_external.csv` in the zip is the 2024-11-25 LiveBench version: stale, not used.

### LiveBench

- Site: https://livebench.ai, React app. The current table is `https://livebench.ai/table_2026_06_25.csv`
  (9.6 KB, `text/csv`, 57 models) with `categories_2026_06_25.json` (7 categories → 23 tasks).
  Overall score = mean of the category averages, each the mean of its tasks (verified: matches
  the published 83.4 for the leader).
- The GitHub repository `LiveBench/livebench.github.io` lags behind the site: `main` has a
  28-model version of the same file, `gh-pages` stops at 2026-01-08. The built JS bundle
  (319 KB) does not contain the table file names. No stable way to discover the newest version:
  hard-coded in `src/transform.py`, overridable with the `livebench_version` field.

### Artificial Analysis (key required, not verified live)

- Docs: https://artificialanalysis.ai/data-api/docs and /api-reference. Free tier: 100 requests
  per 24 h, endpoint `GET /api/v2/language/models/free`, header `x-api-key`. Envelope:
  `{tier, intelligence_index_version, pagination{page,page_size (200),total_pages,has_more},
  data[]}`; each model: `name`, `slug`, `release_date`, `model_creator.name`,
  `evaluations.{artificial_analysis_intelligence_index, _coding_index, _agentic_index}`,
  `pricing`, `performance`. `licensing.is_open_weights` is Pro-only.
- Attribution is required on all tiers ("a visible byline or footer link is sufficient").
- Without a key the endpoint answers `401 {"error":"API key is required"}` (verified).
- The older path `/api/v2/data/llms/models` still appears in the API reference.

### OpenRouter (prices and listing dates, no key)

- `https://openrouter.ai/api/v1/models`: 733 KB, 0.2 s, 443 models on 2026-09-11. Fields used:
  `name` ("Anthropic: Claude Opus 4.6"), `id` ("anthropic/claude-opus-4.6"), `created` (epoch
  seconds, the day the model was listed), `pricing.prompt` / `pricing.completion` ($ per token,
  strings). Entries with `alias_target` (rolling aliases) and `:free` ids are skipped.
- Matching by normalised name (lowercase, vendor prefix, parentheses, dates and variant tokens
  such as high/xhigh/max/thinking/effort/preview removed): 91 of 96 rows across 8 boards
  matched on 2026-09-11. Misses: models absent from OpenRouter (`smaug-agentic`,
  `qwen3.8-flash-next`) and Gemini "Pro" entries listed as "Preview" with a different suffix.
- Alternatives considered: BenchLM (inputPrice/outputPrice, but 3 s delay and estimated data),
  Artificial Analysis (accurate, but a key is required; used for its own boards), LiteLLM's
  `model_prices_and_context_window.json` on GitHub (1.5 MB, keyed by API ids that differ per
  provider, harder to match).

## 4. Rejected sources

| Source | Verdict | Reason |
|---|---|---|
| SWE-bench leaderboard (`swe-bench.github.io/data/leaderboards.json`) | Too big | 4.1 MB with per-instance results; entries are agent scaffolds. Epoch's SWE-bench Verified table (models, 484 tasks) is used instead |
| BenchLM.ai `/api/data/leaderboard` | Not used | Free JSON, but a deliberate 3 s delay on the free tier eats the Serverless budget; scores partly "estimated" |
| Aider polyglot YAML on GitHub | Stale | Last entry October 2025; Epoch's mirror of the same data is used |
| Hugging Face Open LLM Leaderboard | Archived | Discontinued in 2025 |
| LLM-Stats, Vellum, Klu, Scale SEAL | No API | Web pages only |
| OpenRouter rankings | Not a benchmark | Token-usage market share |
| Terminal-Bench on tbench.ai / Hugging Face dataset | Raw | Per-trial `result.json` files; Epoch's table already aggregates the leaderboard |
| Epoch `benchmark_data.zip` via TRMNL polling | Unusable | Binary zip; only the function can read it |

## 5. TRMNL constraints kept in mind

- Merge data cap 100 KB: a board is ~1 KB per 10 rows.
- Serverless: Python, 5 s, 128 MB, network access included (`requests` preinstalled; the function
  uses `urllib` from the standard library so it also runs unchanged under trmnlp).
- `trmnlp` quirk: `{{ env.X }}` is only rendered in polling URL/headers/body, not in the field
  values passed to the transform; hence the `AA_API_KEY` environment fallback in the function.
- Chef hints: no inline styles, no `<style>`, no HTTP in the function (see §2). Layouts use
  framework classes only; the outermost node of every view is the `layout` element.
- Framework classes used (checked in `https://trmnl.com/css/latest/plugins.css`): `layout--row`,
  `layout--stretch-x`, `gap--large`, `portrait:flex--col`, `flex--between`, `flex--bottom`,
  `table--small`, `w--8`, `w--16`, `text--right`, `label--filled`, `label--gray` (same rule as
  the older `label--gray-out`), `label--underline`, `value--xxsmall`, `value--tnums`.
  `label--xsmall` and `title--xsmall` do not exist. `layout--stretch > *` already gives the
  children `flex: 1 1 0%`, so a `stretch-x` on them is redundant (Chef's review, 2026-09-11).
- `{% template %}` partials take explicit arguments with `{% render %}`; loops inside work.
- The framework's table overflow engine (`data-table-limit="true"`, in `plugins.js`) hides
  rows beyond `table.parentElement.clientHeight` minus the `thead` height. Inside a board
  column that height is the content height, so nothing is hidden: each layout caps the rows
  in Liquid instead (measured on the OG: 12 rows per board on the full layout, 11 when the
  three-board header wraps to two lines, 5 rows on the half and quadrant layouts) and gives
  the extra rows `hidden lg:table-row`, so the TRMNL X shows up to 15 / 8. `layout--top`
  leaves short content top-aligned but the columns unstretched; `layout--stretch` stretches
  them and their `flex--col` content still starts at the top.

## 6. Ideas not done

- A `latest/` directory in the Arena mirror (pull request upstream) would remove the pointer chain.
- TRMNL X (`lg:`) could show more rows in the quadrant and half layouts.
- More Epoch tables are one line away in `BOARDS` (Vending-Bench 2, GDPval, Cybench, SciCode,
  WebDev Arena...).
