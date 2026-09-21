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

- Merge data cap 100 KB: a board is ~1 KB per 10 rows, so 3 boards of 60 rows are ~20 KB.
- Serverless: Python, 5 s, 128 MB, network access included (`requests` preinstalled; the function
  uses `urllib` from the standard library so it also runs unchanged under trmnlp).
- `trmnlp` quirk: `{{ env.X }}` is only rendered in polling URL/headers/body, not in the field
  values passed to the transform; hence the `AA_API_KEY` environment fallback in the function.
- Chef hints: no inline styles, no `<style>`, no HTTP in the function (see §2). Layouts use
  framework classes only; the outermost node of every view is the `layout` element.
- Framework classes used (checked in `https://trmnl.com/css/latest/plugins.css`): `layout--row`,
  `layout--col`, `layout--stretch`, `portrait:layout--col`, `gap--large`, `gap--small`,
  `gap--xsmall`, `flex--between`, `flex--bottom`, `flex--center-y`, `grow`,
  `shrink-0`, `w--full`, `w--5`, `lg:w--7`, `w--16`, `lg:w--20`, `h--full`, `h--[48cqh]`, `portrait:h--[32cqh]`,
  `text--right`, `hidden`, `lg:inline`, `lg:block`, `lg:hidden`, `lg:portrait:hidden`,
  `columns`, `column`, `item`, `content`, `label--filled`, `label--gray`, `1bit:text--black`,
  `label--underline`, `lg:label--xlarge`, `lg:label--large`, `lg:title--base`, `lg:portrait:title--small`,
  `value--xxsmall`, `lg:value--small`, `value--tnums`, `text--bold`. `label--xsmall` and `title--xsmall`
  do not exist. Small gray text is hard to read on a 1-bit palette, so every `label--gray` /
  `text--gray` also carries `1bit:text--black` (review by Mario at TRMNL, 2026-09-17); on the
  4-bit TRMNL X the label stays gray. `layout--stretch > *` already gives the children `flex: 1 1 0%`, so a
  `stretch-x` on them is redundant (Chef's review, 2026-09-11).
- Framework typography grew overnight (noticed 2026-09-21): trmnl.com now serves TRMNL's
  custom fonts (`NicoClean` labels, `BlockKie` titles, `TRMNL12/16/21` pixel bundles) and the
  defaults render `label--small` at 16 px and `title--small` at 26 px, against the ~12-13 px
  Inter of the Sep 11-17 renders (Chef screenshot, README screenshots). The CSS 3.3.1 → 3.3.2
  delta is 28 bytes and `plugins.js` is byte-identical, so the change is consistent with the
  custom-font rollout rather than the CSS version bump (exact server-side trigger unconfirmed; `screen--fonts-classic` / `screen--fonts-trmnl` do not change the computed
  styles in trmnlp previews. Rows designed for the old metrics wrap and collide. Mitigation:
  `data-clamp="1"` (framework Clamp engine, docs 3.3) on every row label and header
  title/meta, so a label ellipsizes instead of wrapping; verified across OG / TRMNL X
  landscape / portrait. The local overflow-engine behaviour under the new fonts is
  nondeterministic in trmnlp previews (occasional collapsed screens) — re-check on the device
  before drawing conclusions from a single preview.
- `{% template %}` partials take explicit arguments with `{% render %}`; loops inside work.
- Rows are framework `item`s in a `columns` container with `data-overflow-max-cols` (base,
  `-lg`, `-lg-portrait`): the Overflow engine (`plugins.js`) measures the items, hides those
  that exceed the container's height budget and, when the board is wide enough, spreads them
  over up to N columns (best fit: the plan showing the most rows). Findings while adopting it
  (framework 3.3, 2026-09-16, review by Mario at TRMNL: "wasted space on TRMNL X, use smart
  columns and `lg:` sizes"):
  - The budget is the parent's content box below the `columns` element, minus the height of
    every following sibling (the engine assumes a vertical flow). So each board is wrapped in
    its own `flex flex--col` (header, then the `columns` as last child) and the wrappers are
    the `layout--stretch` children: side by side they are stretched to the layout height.
  - Stacked (`layout--col`: half vertical, full in portrait), a wrapper is a flex item on the
    main axis and `min-height: auto` keeps it as tall as its rows, so nothing is hidden and
    the layout is cut. An explicit height fixes it: `h--full` for one board, `h--[48cqh]` /
    `h--[32cqh]` for two / three (container-query units relative to the layout, which is a
    `container-type: size`). With `flex: 1 1 0%` the boards then share the height equally.
  - The engine measures in staging columns that are plain `.column` elements: a gap class on
    the real `.column` is ignored there and the plan ends two rows short. Keep the default
    column gap.
  - The previous approach, a `table` with `data-table-limit`, had no usable budget inside a
    board (parent height = content height) and needed Liquid row caps per layout and screen
    (`hidden lg:table-row`), i.e. the artificial limits the review objected to.
  - Group headers (`data-group-header` labels) are duplicated as text-only gray labels in the
    next column, so a multi-cell column head cannot be a group header; the unit and the price
    legend moved to the board's header line instead, and `%` / `h` scores carry their unit.
  - Flex children get `min-width: 0` from the framework: the score and price cells need
    `shrink-0`, otherwise a long model name squeezes the number onto its neighbour.
  - `.column` has `justify-content: center` and `.flex--col` has `align-items: center`:
    give full-width children `w--full`.
  - The item's `.meta` bar with an `.index` is 10 px wide: two-digit ranks overflow it (and
    look cramped on the X), so the rows are simple items with the rank as a bold,
    right-aligned label (`text--bold`) in a `w--5 lg:w--7` cell. An outlined badge
    (`label--outline`) was tried and rejected: boxy, and 1 px taller than the score.
- Sizes: OG `label--small` (12 px) rows and `value--xxsmall` scores, TRMNL X `lg:label--xlarge`
  model names, `lg:label--large` organisations, `lg:value--small` scores, `lg:title--base` board
  titles, `lg:gap--xsmall` between the columns (upscaling and gap reduction applied by Mario at
  TRMNL, 2026-09-17); in the narrow mashup
  slots in portrait `lg:portrait:title--small`. The organisation is shown only where a row
  has the width (TRMNL X, landscape, one or two boards): `hidden lg:inline lg:portrait:hidden`.
- Device classes seen by the views (trmnl.com/api/models): TRMNL OG `screen--md screen--1bit`
  800x480; TRMNL X (`v2`) `screen--lg screen--4bit screen--density-2x`, 1040x780 CSS px
  (1872x1404 physical, scale 1.8), `screen--portrait` swaps the two. trmnlp renders any of
  them with `/render/<view>.html?screen_classes=...&width=...&height=...`.

## 6. Ideas not done

- A `latest/` directory in the Arena mirror (pull request upstream) would remove the pointer chain.
- The Overflow engine fills columns in order (the first column full, the rest in the
  second): a single Arena board (20 rows in the mirror) leaves the second column short.
- More Epoch tables are one line away in `BOARDS` (Vending-Bench 2, GDPval, Cybench, SciCode,
  WebDev Arena...).
