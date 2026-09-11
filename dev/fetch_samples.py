#!/usr/bin/env python3
"""Downloads a snapshot of every source into dev/samples, trimmed to the rows the boards can
show, so the fixture and the smoke test work offline and the repository stays small.

Usage: python3 dev/fetch_samples.py [arena] [epoch] [livebench] [openrouter]
       (no argument = everything; then python3 dev/make_fixture.py)

Kept per file: the Arena snapshots in full (a few KB each), the LiveBench table and category
map of the version known to src/transform.py, the Epoch Capabilities Index and each Epoch
benchmark table cut to its KEEP best rows, plus the Epoch metadata rows those tables need,
and the OpenRouter price entries of the models those boards can show.
"""
import csv
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "dev"))
import transform as tf  # noqa: E402

SAMPLES = ROOT / "dev" / "samples"
KEEP = 40  # best rows kept per Epoch table


def main():
    parts = set(sys.argv[1:]) or {"arena", "epoch", "livebench", "openrouter"}
    fetcher = tf.Fetcher(budget=120)
    arena_dir, epoch_dir, lb_dir = SAMPLES / "arena", SAMPLES / "epoch", SAMPLES / "livebench"
    for d in (arena_dir, epoch_dir, lb_dir):
        d.mkdir(parents=True, exist_ok=True)

    if "arena" in parts:
        fetch_arena(fetcher, arena_dir)
    if "epoch" in parts:
        fetch_epoch(fetcher, epoch_dir)
    if "livebench" in parts:
        fetch_livebench(fetcher, lb_dir)
    if "openrouter" in parts:
        fetch_openrouter(fetcher, SAMPLES / "openrouter")


def fetch_arena(fetcher, arena_dir):
    # Arena: pointer + one snapshot per board
    pointer = fetcher.text(tf.ARENA_POINTER)
    (arena_dir / "latest.json").write_text(pointer, encoding="utf-8")
    path = json.loads(pointer)["path"]
    for slug in sorted({s["slug"] for s in tf.BOARDS.values() if s["source"] == "arena"}):
        text = fetcher.text(tf.ARENA_DATA + path + "/" + slug + ".json")
        (arena_dir / (slug + ".json")).write_text(text, encoding="utf-8")
        print("arena/%s.json: %d models" % (slug, len(json.loads(text)["models"])))


def fetch_epoch(fetcher, epoch_dir):
    # Epoch Capabilities Index: best KEEP rows
    eci = list(csv.DictReader(io.StringIO(fetcher.text(tf.EPOCH_ECI))))
    eci.sort(key=lambda r: -float(r["eci"] or 0))
    write_csv(epoch_dir / "eci_scores.csv", eci[:KEEP])

    # Epoch benchmark tables: best KEEP rows of each board's file, then the metadata they use
    archive = zipfile.ZipFile(io.BytesIO(fetcher.bytes(tf.EPOCH_ZIP)))
    meta = {r["source_file"]: r for r in csv.DictReader(io.StringIO(read(archive, tf.EPOCH_BENCHMARK_META))) if r["source_file"]}
    versions = set()
    files = sorted({s["file"] for s in tf.BOARDS.values() if s["source"] == "epoch"})
    for name in files:
        rows = list(csv.DictReader(io.StringIO(read(archive, name))))
        spec = next(s for s in tf.BOARDS.values() if s.get("file") == name)
        column = tf._pick_column(list(rows[0].keys()), spec.get("column") or meta.get(name, {}).get("score_column"))
        rows.sort(key=lambda r: -(tf._num(r.get(column)) or 0))
        rows = rows[:KEEP]
        write_csv(epoch_dir / name, rows)
        versions.update(r["Model version"] for r in rows)
        print("epoch/%s: %d rows kept (score column %r)" % (name, len(rows), column))
    (epoch_dir / tf.EPOCH_BENCHMARK_META).write_text(read(archive, tf.EPOCH_BENCHMARK_META), encoding="utf-8")
    models = [r for r in csv.DictReader(io.StringIO(read(archive, tf.EPOCH_MODEL_META))) if r["model_version"] in versions]
    write_csv(epoch_dir / tf.EPOCH_MODEL_META, models)
    print("epoch/%s: %d rows kept" % (tf.EPOCH_MODEL_META, len(models)))


def fetch_livebench(fetcher, lb_dir):
    # LiveBench: the version known to the code
    for name in ("table_%s.csv" % tf.LIVEBENCH_VERSION, "categories_%s.json" % tf.LIVEBENCH_VERSION):
        (lb_dir / name).write_text(fetcher.text(tf.LIVEBENCH + name), encoding="utf-8")
        print("livebench/%s" % name)


def fetch_openrouter(fetcher, or_dir):
    # OpenRouter prices: only the models the sample boards can show, with the fields the
    # function reads (the full list is 700 KB)
    import make_fixture as fx  # noqa: E402 - reads the samples written above
    sources = fx.build_sources()
    keys = set()
    ids = [b for b in tf.BOARDS if not b.startswith("aa_")]
    for i in range(0, len(ids), tf.MAX_BOARDS):
        chunk = ids[i:i + tf.MAX_BOARDS] + ["none"] * tf.MAX_BOARDS
        fields = {"board_1": chunk[0], "board_2": chunk[1], "board_3": chunk[2], "max_models": str(tf.MAX_MODELS), "show_prices": "no"}
        out = tf.run({"sources": sources, "offline": True, "trmnl": {"plugin_settings": {"custom_fields_values": fields}}})
        for board in out["boards"]:
            for row in board["rows"]:
                keys.update((tf._norm_name(row["model"]), tf._norm_name(row["model"], strip_variants=True)))
    models = json.loads(fetcher.text(tf.OPENROUTER_MODELS))["data"]
    kept = []
    for m in models:
        if m.get("alias_target"):
            continue
        names = [m.get("name") or "", str(m.get("id") or "").split("/", 1)[-1]]
        if any(tf._norm_name(n, strip_variants=s) in keys for n in names for s in (False, True)):
            pricing = m.get("pricing") or {}
            kept.append({"id": m["id"], "name": m.get("name"), "created": m.get("created"),
                         "pricing": {"prompt": pricing.get("prompt"), "completion": pricing.get("completion")}})
    or_dir.mkdir(parents=True, exist_ok=True)
    (or_dir / "models.json").write_text(json.dumps({"data": kept}, indent=1), encoding="utf-8")
    print("openrouter/models.json: %d of %d models kept" % (len(kept), len(models)))


def read(archive, member):
    return archive.read(member).decode("utf-8", "replace")


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
