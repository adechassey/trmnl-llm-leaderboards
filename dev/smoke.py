#!/usr/bin/env python3
"""Smoke test: renders the 4 views in several configurations (board choices, filters, errors)
with `trmnlp build` on the offline samples and checks the produced text.

Usage: python3 dev/smoke.py          (exit 1 if a check fails)
"""
import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_fixture as fx  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "dev" / ".smoke" / "cases"

SOURCES = fx.build_sources()
ONLINE = fx.build_variables(SOURCES)
NO_SOURCES = fx.build_variables({}, offline=True)

# name -> (custom_fields, variables, expected in full, absent from full, expected row count in full or None)
# Row counts are the rows in the markup (every row the function returned, up to 60 per board);
# the Overflow engine hides the ones that do not fit when the screen is rendered.
CASES = {
    "default": (
        {}, ONLINE,
        ["Arena Text", "arena.ai", "Sep 2", "claude-fable-5", "Anthropic", "1507",
         "Epoch Capabilities Index", "epoch.ai", "GPT-6 Astra", "166.6", "Elo", "ECI", "LLM Leaderboards", "Updated 14:00",
         "$ in/out", "10/50", "5/25", "GPT-6 Astra OpenAI new"],
        ["Data unavailable", "Google DeepMind", "Claude Fable 5 Anthropic new"], 60),
    "no_prices": (
        {"show_prices": "no"}, ONLINE,
        ["Arena Text", "1507", "GPT-6 Astra OpenAI new"], ["$ in/out", "10/50"], 60),
    "prices_source_down": (
        {}, {**ONLINE, "sources": {k: v for k, v in SOURCES.items() if "openrouter" not in k}},
        ["Arena Text", "1507", "Epoch Capabilities Index", "GPT-6 Astra OpenAI new"], ["$ in/out", "10/50", "claude-fable-5 Anthropic new"], 60),
    "three_boards": (
        {"board_3": "epoch_gpqa"}, ONLINE,
        ["Arena Text", "Epoch Capabilities Index", "GPQA Diamond", "95.8"], ["Data unavailable"], 95),
    "livebench": (
        {"board_1": "livebench", "board_2": "livebench_agentic", "board_3": "epoch_metr"}, ONLINE,
        ["LiveBench", "livebench.ai", "Jun 25", "claude-fable-5-1-max-effort", "83.4",
         "LiveBench Agentic", "METR Time Horizon", "Claude Mythos Preview", "17.4"],
        ["Data unavailable"], 147),
    "epoch_tables": (
        {"board_1": "epoch_hle", "board_2": "epoch_terminalbench", "board_3": "epoch_swebench", "max_models": "8"}, ONLINE,
        ["Humanity's Last Exam", "Claude Fable 5.1", "46.5", "Terminal-Bench", "GPT-5.5", "84.7", "SWE-bench Verified", "Claude Opus 4.7", "83.5"],
        ["Data unavailable"], 24),
    "aa_no_key": (
        {"board_1": "aa_intelligence", "board_2": "arena_code"}, ONLINE,
        ["AA Intelligence", "Artificial Analysis API key", "Arena Code", "gpt-6-astra-max", "1796"], ["Data unavailable"], 20),
    "aa_with_key": (  # variants such as "(max)" collapse to the plain model name
        {"board_1": "aa_intelligence", "board_2": "aa_coding", "aa_api_key": "test-key", "max_models": "5"}, ONLINE,
        ["AA Intelligence", "artificialanalysis.ai", "Sample Model A Sample Lab", "78.4", "AA Coding", "Sample Model B Example AI new", "73.5", "Index",
         "$ in/out", "10/50", "8/40"],
        ["Data unavailable", "Sample Legacy", "(max)", "(xhigh)"], 10),
    "open_weights": (
        {"open_weights_only": "yes", "board_1": "epoch_eci", "board_2": "arena_text"}, ONLINE,
        ["11 Kimi K3"], ["GPT-6 Astra", "Claude Fable 5.1", "claude-fable-5 "], None),
    "highlight": (
        {"highlight": "Claude, gpt-6"}, ONLINE,
        ["label--filled"], [], 60),
    "max_models_3": (
        {"max_models": "3"}, ONLINE, ["claude-fable-5.1-max"], ["claude-opus-4-7-high"], 6),
    "custom_title": (
        {"title": "Frontier models"}, ONLINE, ["Frontier models"], ["LLM Leaderboards"], 60),
    "lang_fr": (
        {"language": "fr"}, ONLINE,
        ["Classements LLM", "Mis à jour 14:00", "2 Sep", "nouveau"], ["Updated", " new "], 60),
    "lang_auto_fr_locale": (
        {"language": "auto"}, {**ONLINE, "trmnl": {**ONLINE["trmnl"], "user": {"locale": "fr-FR"}}},
        ["Classements LLM", "nouveau"], [" new "], 60),
    "one_board": (
        {"board_2": "none"}, ONLINE, ["Arena Text"], ["Epoch Capabilities Index"], 20),
    "no_board": (
        {"board_1": "none", "board_2": "none"}, ONLINE, ["Data unavailable", "No leaderboard selected"], ["Arena Text"], 0),
    "unknown_board": (
        {"board_1": "made_up"}, ONLINE, ["Unknown leaderboard", "Epoch Capabilities Index"], ["Data unavailable"], 40),
    "source_down": (  # every board keeps its title and shows its own error
        {}, NO_SOURCES, ["Arena Text", "Epoch Capabilities Index", "offline"], ["Data unavailable"], 0),
    "one_source_down": (
        {}, {**ONLINE, "sources": {k: v for k, v in SOURCES.items() if "epoch.ai" not in k}},
        ["Arena Text", "claude-fable-5", "Epoch Capabilities Index", "offline"], ["Data unavailable"], 20),
    "no_transform_output": (
        {}, {**fx.FROZEN_CLOCK, "boards": None}, ["Data unavailable", "Serverless"], ["Arena Text"], 0),
    "livebench_version_field": (
        {"board_1": "livebench", "board_2": "none", "livebench_version": "2026-06-25"}, ONLINE,
        ["LiveBench", "Jun 25", "claude-fable-5-1-max-effort"], ["Data unavailable"], 57),
}

VIEWS = ["full", "half_horizontal", "half_vertical", "quadrant"]


def trmnlp_cmd():
    gem_bin = subprocess.run(["gem", "environment", "gemdir"], capture_output=True, text=True).stdout.strip()
    os.environ["PATH"] = f"{gem_bin}/bin:{os.environ['PATH']}"
    return [str(ROOT / "bin" / "trmnlp")]


def text_of(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    i = raw.find('class="layout')
    seg = raw[i:] if i >= 0 else raw
    seg = seg.split("</body>")[0]
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", seg)))


def rows_of(path: Path) -> int:
    """Leaderboard rows in the markup (the Overflow engine hides the ones that do not fit at render time)."""
    raw = path.read_text(encoding="utf-8")
    return len(re.findall(r'<div class="item">', raw))


def main() -> int:
    cmd = trmnlp_cmd()
    shutil.rmtree(WORK, ignore_errors=True)
    failures = 0
    for name, (fields, variables, expected, absent, nrows) in CASES.items():
        case_dir = WORK / name
        case_dir.mkdir(parents=True)
        (case_dir / "src").symlink_to(ROOT / "src")
        cfg = fx.render_config({**fx.DEFAULT_FIELDS, **fields}, variables, watch=())
        (case_dir / ".trmnlp.yml").write_text(cfg, encoding="utf-8")
        rel = case_dir.relative_to(ROOT)
        run = subprocess.run(cmd + ["build", "--quiet", "--dir", str(rel)], cwd=ROOT, capture_output=True, text=True)
        if run.returncode != 0:
            print(f"✗ {name}: trmnlp build failed\n{run.stdout}{run.stderr}")
            failures += 1
            continue
        problems = []
        for view in VIEWS:
            out = case_dir / "_build" / f"{view}.html"
            if not out.exists():
                problems.append(f"{view}: missing file")
                continue
            raw = out.read_text(encoding="utf-8")
            if re.search(r"Liquid (error|syntax error)", raw):
                problems.append(f"{view}: Liquid error")
        full = case_dir / "_build" / "full.html"
        if full.exists():
            raw = full.read_text(encoding="utf-8")
            t = text_of(full)
            for e in expected:
                if e not in t and e not in raw:
                    problems.append(f"expected text missing: {e!r}")
            problems += [f"unexpected text present: {a!r}" for a in absent if a in t]
            if nrows is not None and rows_of(full) != nrows:
                problems.append(f"{rows_of(full)} rows instead of {nrows}")
        status = "✓" if not problems else "✗"
        print(f"{status} {name}")
        for p in problems:
            print(f"    - {p}")
        failures += bool(problems)
    print(f"\n{len(CASES) - failures}/{len(CASES)} cases OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
