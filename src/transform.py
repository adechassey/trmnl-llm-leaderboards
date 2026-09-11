"""TRMNL Serverless function: builds the leaderboards shown by the plugin.

Run by TRMNL (Serverless tab, Python) on every refresh, and by trmnlp locally.

Sources, all public:
  - Arena (arena.ai, formerly LMArena): daily JSON snapshots mirrored on GitHub by
    oolong-tea-2026/arena-ai-leaderboards. TRMNL polls the `latest.json` pointer; the
    function fetches the snapshot it points to (one small file per board).
  - Epoch AI, Capabilities & Benchmarking hub (CC BY 4.0): `eci_scores.csv` for the Epoch
    Capabilities Index, `benchmark_data.zip` for the per-benchmark tables (GPQA Diamond,
    SWE-bench Verified, FrontierMath, Terminal-Bench, ...).
  - LiveBench: `table_<version>.csv` and `categories_<version>.json` on livebench.ai.
  - Artificial Analysis: `/api/v2/language/models/free`, with the user's free API key.
  - OpenRouter `/api/v1/models` (no key): input / output prices per million tokens, matched to
    the leaderboard rows by normalised model name, and the date a model was listed, used as
    its release date when the leaderboard has none (Arena, LiveBench).

Why the function fetches instead of TRMNL polling everything: the Arena snapshot lives at
a dated path only known from the pointer, the Epoch tables are inside a zip, and only the
boards the user selected should be downloaded. The function keeps a time budget well below
TRMNL's 5 s limit and reports a per-board error instead of failing the whole screen.

Input `input`: the plugin variables, including
  - the polled Arena pointer (`date`, `path`) merged at the top level by TRMNL
  - trmnl.plugin_settings.custom_fields_values: board_1..3, max_models, open_weights_only,
    show_prices, highlight, aa_api_key, livebench_version, language
  - `sources` (fixtures only): {url_or_zip_member: text} used instead of the network,
    `offline: true` to forbid any network call, `now` (epoch) to freeze the clock

Output: {"boards": [...], "error": str, "generated_at": epoch}
  each board: {"id", "title", "source" (domain, for attribution), "date" (ISO, may be ""),
               "unit", "has_price", "has_new", "error",
               "rows": [{"rank", "model", "org", "score", "open", "price", "released", "new"}]}
  rows are sorted best first; `rank` is the position in the full list before the
  open-weights filter, `score` and `price` ("10/50" = $ per million input / output tokens,
  "" when unknown) are already formatted, `open` is true/false/null (unknown), `new` is true
  when the model was released less than NEW_DAYS ago.
"""
import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime

USER_AGENT = "trmnl-llm-leaderboards/1.0 (+https://github.com/adechassey/trmnl-llm-leaderboards)"
TIME_BUDGET = 4.0    # seconds of network time per run (TRMNL stops the function at 5 s)
FETCH_TIMEOUT = 3.5  # seconds per request
MAX_BOARDS = 3
MAX_MODELS = 15
DEFAULT_MODELS = 10

ARENA_DATA = "https://raw.githubusercontent.com/oolong-tea-2026/arena-ai-leaderboards/main/data/"
ARENA_POINTER = ARENA_DATA + "latest.json"
EPOCH_ECI = "https://epoch.ai/data/eci_scores.csv"
EPOCH_ZIP = "https://epoch.ai/data/benchmark_data.zip"
EPOCH_BENCHMARK_META = "benchmark_metadata.csv"
EPOCH_MODEL_META = "model_metadata.csv"
LIVEBENCH = "https://livebench.ai/"
LIVEBENCH_VERSION = "2026_06_25"  # newest release known to this code; the field overrides it
AA_MODELS = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_MAX_PAGES = 3
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
NEW_DAYS = 14  # a model released less than this many days ago is flagged "new"

# Name tokens that distinguish runs of the same model, not the model itself (price lookups)
VARIANT_TOKENS = {"max", "xhigh", "high", "medium", "low", "minimal", "thinking", "nothinking", "effort",
                  "reasoning", "auto", "preview", "exp", "latest"}

SOURCES = {"arena": "arena.ai", "eci": "epoch.ai", "epoch": "epoch.ai",
           "livebench": "livebench.ai", "aa": "artificialanalysis.ai"}

# Board id -> how to build it. Titles are what the screen shows.
BOARDS = {
    # Arena: human preference Elo
    "arena_text": {"source": "arena", "slug": "text", "title": "Arena Text", "unit": "Elo"},
    "arena_code": {"source": "arena", "slug": "code", "title": "Arena Code", "unit": "Elo"},
    "arena_vision": {"source": "arena", "slug": "vision", "title": "Arena Vision", "unit": "Elo"},
    "arena_search": {"source": "arena", "slug": "search", "title": "Arena Search", "unit": "Elo"},
    # Epoch AI: aggregate index, then one table per benchmark (file inside benchmark_data.zip)
    "epoch_eci": {"source": "eci", "title": "Epoch Capabilities Index", "unit": "ECI"},
    "epoch_gpqa": {"source": "epoch", "file": "gpqa_diamond.csv", "title": "GPQA Diamond", "unit": "%"},
    "epoch_hle": {"source": "epoch", "file": "hle_external.csv", "title": "Humanity's Last Exam", "unit": "%"},
    "epoch_swebench": {"source": "epoch", "file": "swe_bench_verified.csv", "title": "SWE-bench Verified", "unit": "%"},
    "epoch_terminalbench": {"source": "epoch", "file": "terminalbench_external.csv", "title": "Terminal-Bench", "unit": "%"},
    "epoch_aider": {"source": "epoch", "file": "aider_polyglot_external.csv", "title": "Aider Polyglot", "unit": "%"},
    "epoch_frontiermath": {"source": "epoch", "file": "frontiermath_tiers_1_3_v2.csv", "title": "FrontierMath T1-3", "unit": "%"},
    "epoch_frontiermath_t4": {"source": "epoch", "file": "frontiermath_tier_4_v2.csv", "title": "FrontierMath Tier 4", "unit": "%"},
    "epoch_arcagi2": {"source": "epoch", "file": "arc_agi_2_external.csv", "title": "ARC-AGI-2", "unit": "%"},
    "epoch_simpleqa": {"source": "epoch", "file": "simpleqa_verified.csv", "title": "SimpleQA Verified", "unit": "%"},
    "epoch_aime": {"source": "epoch", "file": "otis_mock_aime_2024_2025.csv", "title": "OTIS Mock AIME", "unit": "%"},
    "epoch_osworld": {"source": "epoch", "file": "osworld_2_external.csv", "title": "OSWorld 2.0", "unit": "%"},
    "epoch_metr": {"source": "epoch", "file": "metr_time_horizons_external.csv", "title": "METR Time Horizon",
                   "unit": "h", "column": "Time horizon", "kind": "minutes"},
    # LiveBench: overall score, or one category (matched on the category name, case-insensitive)
    "livebench": {"source": "livebench", "category": "", "title": "LiveBench", "unit": "%"},
    "livebench_reasoning": {"source": "livebench", "category": "reasoning", "title": "LiveBench Reasoning", "unit": "%"},
    "livebench_coding": {"source": "livebench", "category": "coding", "title": "LiveBench Coding", "unit": "%"},
    "livebench_agentic": {"source": "livebench", "category": "agentic coding", "title": "LiveBench Agentic", "unit": "%"},
    "livebench_math": {"source": "livebench", "category": "mathematics", "title": "LiveBench Math", "unit": "%"},
    # Artificial Analysis: headline indices of the free tier
    "aa_intelligence": {"source": "aa", "key": "artificial_analysis_intelligence_index", "title": "AA Intelligence", "unit": "Index"},
    "aa_coding": {"source": "aa", "key": "artificial_analysis_coding_index", "title": "AA Coding", "unit": "Index"},
    "aa_agentic": {"source": "aa", "key": "artificial_analysis_agentic_index", "title": "AA Agentic", "unit": "Index"},
}

# Epoch tables: score column when benchmark_metadata.csv does not name one
SCORE_COLUMNS = ["Best score (across scorers)", "mean_score", "Score", "Accuracy", "Accuracy mean",
                 "Percent correct", "Binary accuracy", "Overall score", "Overall", "Average"]
# Epoch tables: columns that date a row, most specific first
DATE_COLUMNS = ["Started at", "Run date", "Date of evaluation", "Evaluation date", "Last updated",
                "Date added", "Created", "Graded at", "Release date"]
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

# Model name prefix -> organisation, for sources that do not say (LiveBench)
ORG_PREFIXES = [
    ("claude", "Anthropic"), ("gpt", "OpenAI"), ("chatgpt", "OpenAI"), ("o1", "OpenAI"), ("o3", "OpenAI"),
    ("o4", "OpenAI"), ("gemini", "Google"), ("gemma", "Google"), ("deepseek", "DeepSeek"), ("qwen", "Alibaba"),
    ("qwq", "Alibaba"), ("kimi", "Moonshot"), ("grok", "xAI"), ("llama", "Meta"), ("muse", "Meta"),
    ("mistral", "Mistral"), ("magistral", "Mistral"), ("devstral", "Mistral"), ("glm", "Z.ai"),
    ("minimax", "MiniMax"), ("nemotron", "NVIDIA"), ("command", "Cohere"), ("phi", "Microsoft"),
    ("hunyuan", "Tencent"), ("doubao", "ByteDance"), ("seed", "ByteDance"), ("step", "StepFun"),
]


# Organisation names as the sources spell them -> short form for the screen
SHORT_ORGS = {"google deepmind": "Google", "meta ai": "Meta", "z.ai (zhipu ai)": "Z.ai", "moonshot ai": "Moonshot",
              "alibaba cloud": "Alibaba", "mistral ai": "Mistral", "microsoft research": "Microsoft",
              "nvidia corporation": "NVIDIA", "amazon web services": "Amazon", "bytedance seed": "ByteDance"}


class FetchError(Exception):
    """A source could not be read; the message is shown on the board."""

    def __init__(self, message, status=None):
        Exception.__init__(self, message)
        self.status = status


def run(input):
    if not isinstance(input, dict):
        input = {}
    if "boards" in input:
        return input  # already transformed data (test fixtures)

    cf = _custom_fields(input)
    result = {"boards": [], "error": "", "generated_at": int(time.time())}

    ids = _selected_boards(cf)
    if not ids:
        result["error"] = "No leaderboard selected: pick one in the plugin settings."
        return result

    ctx = Context(
        fetcher=Fetcher(seed=_seed_sources(input), offline=bool(input.get("offline")), budget=TIME_BUDGET),
        max_models=_clamp(_to_int(cf.get("max_models"), DEFAULT_MODELS), 1, MAX_MODELS),
        open_only=_yes(cf.get("open_weights_only"), False),
        prices=_yes(cf.get("show_prices"), True),
        aa_key=_aa_key(cf),
        livebench_version=_livebench_version(cf.get("livebench_version")),
        now=_to_int(input.get("now"), 0) or int(time.time()),
    )
    for board_id in ids:
        result["boards"].append(_build_board(board_id, ctx))
    if all(b["error"] for b in result["boards"]):
        result["error"] = result["boards"][0]["error"]
    return result


# --- boards -------------------------------------------------------------------------------------

class Context(object):
    def __init__(self, fetcher, max_models, open_only, prices, aa_key, livebench_version, now):
        self.fetcher = fetcher
        self.max_models = max_models
        self.open_only = open_only
        self.prices = prices
        self.aa_key = aa_key
        self.livebench_version = livebench_version
        self.now = now
        self._epoch_meta = None
        self._epoch_models = None
        self._price_book = None

    def price_book(self):
        """Normalised model name -> {"in", "out", "released"} from OpenRouter; {} when the
        prices are disabled or OpenRouter cannot be read (prices are optional)."""
        if self._price_book is None:
            self._price_book = {}
            if self.prices:
                try:
                    self._price_book = _openrouter_prices(_json(self.fetcher.text(OPENROUTER_MODELS), "openrouter.ai"))
                except FetchError:
                    pass
        return self._price_book

    def epoch_benchmark_meta(self):
        """source_file -> {score_column, scale, ...} from benchmark_metadata.csv."""
        if self._epoch_meta is None:
            self._epoch_meta = {}
            try:
                for r in _csv_rows(self.fetcher.zip_member(EPOCH_ZIP, EPOCH_BENCHMARK_META)):
                    if r.get("source_file"):
                        self._epoch_meta[r["source_file"]] = r
            except FetchError:
                pass  # optional: SCORE_COLUMNS is the fallback
        return self._epoch_meta

    def epoch_model_meta(self):
        """model_version -> {model_group, display_name, organization, accessibility}."""
        if self._epoch_models is None:
            self._epoch_models = {}
            try:
                for r in _csv_rows(self.fetcher.zip_member(EPOCH_ZIP, EPOCH_MODEL_META)):
                    if r.get("model_version"):
                        self._epoch_models[r["model_version"]] = r
            except FetchError:
                pass  # optional: names fall back to the raw version id
        return self._epoch_models


def _build_board(board_id, ctx):
    spec = BOARDS.get(board_id)
    board = {"id": board_id, "title": spec["title"] if spec else board_id, "source": "", "date": "",
             "unit": spec["unit"] if spec else "", "has_price": False, "has_new": False, "rows": [], "error": ""}
    if not spec:
        board["error"] = "Unknown leaderboard '%s'." % board_id
        return board
    board["source"] = SOURCES[spec["source"]]
    try:
        entries, date = LOADERS[spec["source"]](spec, ctx)
    except FetchError as e:
        board["error"] = str(e)
        return board
    except Exception as e:  # noqa: BLE001 - unexpected data shape must not kill the other boards
        board["error"] = "Unexpected %s data (%s)." % (board["source"], e.__class__.__name__)
        return board
    board["date"] = date
    board["rows"] = _rank(entries, ctx)
    board["has_price"] = any(r["price"] for r in board["rows"])
    board["has_new"] = any(r["new"] for r in board["rows"])
    if not board["rows"] and not entries:
        board["error"] = "%s returned no models." % board["source"]
    return board


def _rank(entries, ctx):
    """Best first; rank = position in the full list, so a filtered board keeps real ranks.
    Adds the price and release date (from the source itself, else from OpenRouter)."""
    entries = sorted(entries, key=lambda e: -e["value"])
    rows = []
    for position, e in enumerate(entries, 1):
        if ctx.open_only and e["open"] is False:
            continue
        listed = _price_lookup(e["model"], ctx.price_book()) if ctx.prices else None
        price_in, price_out = e.get("price_in"), e.get("price_out")
        if ctx.prices and price_in is None and listed:
            price_in, price_out = listed["in"], listed["out"]
        released = e.get("released") or (listed["released"] if listed else "")
        rows.append({"rank": position, "model": _clean(e["model"]), "org": _short_org(e["org"]),
                     "score": e["score"], "open": e["open"],
                     "price": _fmt_price_pair(price_in, price_out) if ctx.prices else "",
                     "released": released, "new": _is_new(released, ctx.now)})
        if len(rows) >= ctx.max_models:
            break
    return rows


# --- loaders: each returns (entries, iso_date) -----------------------------------------------------
# entry: {"model": str, "org": str, "value": float, "open": True/False/None, "score": str}

def _load_arena(spec, ctx):
    pointer = _json(ctx.fetcher.text(ARENA_POINTER), "arena.ai pointer")
    path = str(pointer.get("path") or pointer.get("date") or "").strip("/ ")
    if not path:
        raise FetchError("arena.ai: the snapshot pointer is empty.")
    data = _json(ctx.fetcher.text(ARENA_DATA + path + "/" + spec["slug"] + ".json"), "arena.ai snapshot")
    entries = []
    for m in data.get("models") or []:
        score = _num(m.get("score"))
        if score is None:
            continue
        lic = str(m.get("license") or "").lower()
        entries.append({"model": m.get("model") or "", "org": m.get("vendor") or "", "value": score,
                        "open": True if lic == "open" else False if lic == "proprietary" else None,
                        "score": "%d" % round(score)})
    meta = data.get("meta") or {}
    return entries, _iso_date(meta.get("last_updated")) or _iso_date(meta.get("fetched_at"))


def _load_eci(spec, ctx):
    entries, dates = [], []
    for r in _csv_rows(ctx.fetcher.text(EPOCH_ECI)):
        v = _num(r.get("eci"))
        if v is None:
            continue
        entries.append({"model": r.get("Display name") or r.get("Model") or "", "org": r.get("Organization") or "",
                        "value": v, "open": _open_from_accessibility(r.get("Accessibility group") or r.get("Model accessibility")),
                        "score": "%.1f" % v, "released": _iso_date(r.get("date"))})
        dates.append(_iso_date(r.get("date")))
    return entries, max(dates) if dates else ""


def _load_epoch(spec, ctx):
    rows = _csv_rows(ctx.fetcher.zip_member(EPOCH_ZIP, spec["file"]))
    meta = ctx.epoch_benchmark_meta().get(spec["file"], {})
    header = list(rows[0].keys()) if rows else []
    column = _pick_column(header, spec.get("column") or meta.get("score_column"))
    scale = _num(meta.get("scale")) or 1.0  # 1.0 = fractions, 0.01 = already percentages
    models = ctx.epoch_model_meta()
    best, dates = {}, []
    for r in rows:
        v = _num(r.get(column))
        if v is None:
            continue
        version = (r.get("Model version") or "").strip()
        m = models.get(version) or {}
        # one row per model: reasoning-effort variants and agent scaffolds collapse to their best run
        group = _clean(m.get("model_group") or r.get("Name") or _pretty_version(version))
        if not group:
            continue
        if spec.get("kind") == "minutes":
            shown, score = v / 60.0, "%.1f" % (v / 60.0)
        else:
            shown = v * scale * 100.0
            score = "%.1f" % shown
        entry = {"model": group, "org": m.get("organization") or r.get("Organization") or r.get("Model Org") or "",
                 "value": shown, "open": _open_from_accessibility(m.get("accessibility")), "score": score,
                 "released": _iso_date(m.get("date")) or _iso_date(r.get("Release date"))}
        if group not in best or shown > best[group]["value"]:
            best[group] = entry
        dates.append(_row_date(r))
    return list(best.values()), max(dates) if dates else ""


def _load_livebench(spec, ctx):
    version = ctx.livebench_version
    table = _csv_rows(ctx.fetcher.text(LIVEBENCH + "table_%s.csv" % version))
    cats = _json(ctx.fetcher.text(LIVEBENCH + "categories_%s.json" % version), "livebench.ai categories")
    wanted = (spec.get("category") or "").lower()
    if wanted:
        key = next((k for k in cats if k.lower() == wanted), None) or next((k for k in cats if k.lower().startswith(wanted)), None)
        if not key:
            raise FetchError("LiveBench %s has no '%s' category." % (version.replace("_", "-"), wanted))
        cats = {key: cats[key]}
    entries = []
    for r in table:
        # official score: mean of the category averages, each the mean of its tasks
        averages = []
        for tasks in cats.values():
            values = [x for x in (_num(r.get(t)) for t in tasks) if x is not None]
            if values:
                averages.append(sum(values) / len(values))
        if not averages:
            continue
        v = sum(averages) / len(averages)
        name = r.get("model") or ""
        entries.append({"model": name, "org": _guess_org(name), "value": v, "open": None, "score": "%.1f" % v})
    return entries, version.replace("_", "-")


def _load_aa(spec, ctx):
    if not ctx.aa_key:
        raise FetchError("Add your free Artificial Analysis API key in the plugin settings.")
    entries, page = [], 1
    while page <= AA_MAX_PAGES:
        url = AA_MODELS + ("?page=%d" % page if page > 1 else "")
        try:
            data = _json(ctx.fetcher.text(url, headers={"x-api-key": ctx.aa_key}), "artificialanalysis.ai")
        except FetchError as e:
            if e.status in (401, 403):
                raise FetchError("artificialanalysis.ai refused the API key (HTTP %d)." % e.status, e.status)
            raise
        for m in data.get("data") or []:
            v = _num(_dig(m, "evaluations", spec["key"]))
            if v is None:
                continue
            entries.append({"model": m.get("name") or m.get("slug") or "", "org": _dig(m, "model_creator", "name") or "",
                            "value": v, "open": _aa_open(m), "score": "%.1f" % v,
                            "price_in": _num(_dig(m, "pricing", "price_1m_input_tokens")),
                            "price_out": _num(_dig(m, "pricing", "price_1m_output_tokens")),
                            "released": _iso_date(m.get("release_date"))})
        if not (data.get("pagination") or {}).get("has_more"):
            break
        page += 1
    return entries, ""


LOADERS = {"arena": _load_arena, "eci": _load_eci, "epoch": _load_epoch, "livebench": _load_livebench, "aa": _load_aa}


# --- prices and release dates (OpenRouter) ----------------------------------------------------

def _openrouter_prices(data):
    """Normalised name -> {"in", "out", "released"} ($ per million tokens, ISO date the model was
    listed). Each model is indexed under its display name and its slug, raw and without variant
    tokens; the first listing wins so a plain model beats its ':free' or aliased variants."""
    book = {}
    for m in data.get("data") or []:
        if not isinstance(m, dict) or m.get("alias_target") or str(m.get("id", "")).endswith(":free"):
            continue
        pricing = m.get("pricing") or {}
        price_in, price_out = _num(pricing.get("prompt")), _num(pricing.get("completion"))
        if price_in is None or price_out is None:
            continue
        created = _num(m.get("created"))
        entry = {"in": price_in * 1e6, "out": price_out * 1e6,
                 "released": datetime.utcfromtimestamp(created).strftime("%Y-%m-%d") if created else ""}
        names = [m.get("name") or "", str(m.get("id") or "").split("/", 1)[-1]]
        for name in names:
            for key in (_norm_name(name), _norm_name(name, strip_variants=True)):
                if key and key not in book:
                    book[key] = entry
    return book


def _price_lookup(model, book):
    if not book:
        return None
    return book.get(_norm_name(model)) or book.get(_norm_name(model, strip_variants=True))


def _norm_name(name, strip_variants=False):
    """'Anthropic: Claude Opus 4.6' / 'claude-opus-4-6-high' / 'Claude Opus 4.6 (high)' ->
    'claudeopus46' (with strip_variants) so the same model matches across sources."""
    text = str(name or "").lower().split(": ", 1)[-1]  # OpenRouter prefixes the vendor
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\b20\d{6}\b|-\d{4}(?!-\d)\b", " ", text)  # dated snapshots
    tokens = re.split(r"[^a-z0-9.]+", text)
    if strip_variants:
        tokens = [t for t in tokens if t not in VARIANT_TOKENS]
    return re.sub(r"[^a-z0-9]", "", "".join(tokens))


def _fmt_price_pair(price_in, price_out):
    if price_in is None or price_out is None:
        return ""
    return "%s/%s" % (_fmt_price(price_in), _fmt_price(price_out))


def _fmt_price(value):
    """$ per million tokens, compact: 0.75, 1.25, 5, 10, 180."""
    text = "%.2f" % value if value < 10 else "%.0f" % value
    return text.rstrip("0").rstrip(".") if "." in text else text


def _is_new(released, now):
    if not released:
        return False
    try:
        age = now - int((datetime.strptime(released[:10], "%Y-%m-%d") - datetime(1970, 1, 1)).total_seconds())
    except ValueError:
        return False
    return 0 <= age < NEW_DAYS * 86400


# --- fetching --------------------------------------------------------------------------------------

class Fetcher(object):
    """HTTP GET with a shared time budget. `seed` maps a URL (or "<zip url>#<member>") to text
    already available: the polled Arena pointer, or fixture data in tests. `offline` forbids
    network calls, so fixtures fail loudly when they miss a file."""

    def __init__(self, seed=None, offline=False, budget=TIME_BUDGET):
        self.cache = dict(seed or {})
        self.offline = offline
        self.deadline = time.monotonic() + budget
        self._zips = {}

    def text(self, url, headers=None):
        if url not in self.cache:
            self.cache[url] = self.bytes(url, headers).decode("utf-8", "replace")
        return self.cache[url]

    def zip_member(self, url, member):
        key = url + "#" + member
        if key not in self.cache:
            if url not in self._zips:
                try:
                    self._zips[url] = zipfile.ZipFile(io.BytesIO(self.bytes(url)))
                except zipfile.BadZipFile:
                    raise FetchError("%s sent an unreadable archive." % _host(url))
            try:
                self.cache[key] = self._zips[url].read(member).decode("utf-8", "replace")
            except KeyError:
                raise FetchError("%s no longer contains %s." % (_host(url), member))
        return self.cache[key]

    def bytes(self, url, headers=None):
        if self.offline:
            raise FetchError("offline: no fixture for %s" % url)
        remaining = self.deadline - time.monotonic()
        if remaining < 0.3:
            raise FetchError("Out of time before fetching %s." % _host(url))
        req = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=min(FETCH_TIMEOUT, remaining)) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            raise FetchError("%s: HTTP %d" % (_host(url), e.code), e.code)
        except Exception as e:  # noqa: BLE001 - URLError, socket timeout, SSL...
            reason = getattr(e, "reason", None) or e.__class__.__name__
            raise FetchError("%s unreachable (%s)." % (_host(url), str(reason)[:60]))


def _seed_sources(input):
    """Fixture sources, plus the Arena pointer polled by TRMNL (top level with one polling URL,
    under IDX_0 with several, or as a `data` string when the JSON was not recognised)."""
    seeds = dict(input.get("sources") or {})
    if ARENA_POINTER not in seeds:
        for candidate in (input, input.get("IDX_0")):
            pointer = _pointer_in(candidate)
            if pointer:
                seeds[ARENA_POINTER] = json.dumps(pointer)
                break
    return seeds


def _pointer_in(obj):
    if not isinstance(obj, dict):
        return None
    if obj.get("path") or obj.get("date"):
        return {"path": obj.get("path") or obj.get("date"), "date": obj.get("date")}
    data = obj.get("data")
    if isinstance(data, str) and data.strip().startswith("{"):
        try:
            return _pointer_in(json.loads(data))
        except ValueError:
            return None
    return None


# --- helpers -----------------------------------------------------------------------------------

def _custom_fields(input):
    cf = _dig(input, "trmnl", "plugin_settings", "custom_fields_values")
    if isinstance(cf, dict) and cf:
        return cf
    keys = ("board_1", "board_2", "board_3", "max_models", "open_weights_only", "show_prices", "highlight",
            "aa_api_key", "livebench_version")
    return {k: input.get(k) for k in keys if k in input}


def _yes(value, default):
    text = str(value if value is not None else "").strip().lower()
    if text in ("yes", "true", "1", "on"):
        return True
    if text in ("no", "false", "0", "off"):
        return False
    return default


def _selected_boards(cf):
    ids = []
    for key in ("board_1", "board_2", "board_3"):
        value = str(cf.get(key) or "").strip().lower()
        if value and value != "none" and value not in ids:
            ids.append(value)
    return ids[:MAX_BOARDS]


def _aa_key(cf):
    key = str(cf.get("aa_api_key") or "").strip()
    if "{{" in key:  # unrendered Liquid placeholder from a local .trmnlp.yml
        key = ""
    return key or os.environ.get("AA_API_KEY", "").strip()


def _livebench_version(raw):
    text = re.sub(r"[^0-9]+", "_", str(raw or "")).strip("_")
    return text if re.match(r"^\d{4}_\d{2}_\d{2}$", text) else LIVEBENCH_VERSION


def _open_from_accessibility(text):
    text = str(text or "").lower()
    if not text:
        return None
    return text.startswith("open")


def _aa_open(model):
    for path in (("licensing", "is_open_weights"), ("is_open_weights",), ("open_weights",)):
        value = _dig(model, *path)
        if isinstance(value, bool):
            return value
    return None


def _pick_column(header, preferred):
    for name in [preferred] + SCORE_COLUMNS:
        if name and name in header:
            return name
    raise FetchError("No score column found (%s)." % ", ".join(header[:4]))


def _row_date(row):
    for column in DATE_COLUMNS:
        date = _iso_date(row.get(column))
        if date:
            return date
    return ""


def _iso_date(value):
    """'2026-09-03', '2026-09-03T10:00:00Z' or 'Sep 3, 2026' -> '2026-09-03', else ''."""
    text = str(value or "").strip()
    if ISO_DATE.match(text):
        return text[:10]
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _pretty_version(version):
    """Epoch model version id without its reasoning-effort suffix: 'gpt-5.5_high' -> 'gpt-5.5'."""
    return version.split("_", 1)[0] if version else ""


def _guess_org(name):
    lowered = str(name or "").lower()
    for prefix, org in ORG_PREFIXES:
        if lowered.startswith(prefix):
            return org
    return ""


def _short_org(org):
    """Screen-friendly organisation name: 'Google DeepMind' -> 'Google', 'Z.ai (Zhipu AI)' -> 'Z.ai'."""
    org = _clean(org)
    return SHORT_ORGS.get(org.lower(), re.sub(r"\s*\(.*\)$", "", org))


def _csv_rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def _json(text, what):
    try:
        data = json.loads(text)
    except ValueError:
        raise FetchError("%s sent something that is not JSON." % what)
    if not isinstance(data, dict):
        raise FetchError("%s sent an unexpected JSON shape." % what)
    return data


def _clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _host(url):
    return url.split("/")[2] if url.count("/") >= 2 else url


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def _dig(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, low, high):
    return max(low, min(high, value))
