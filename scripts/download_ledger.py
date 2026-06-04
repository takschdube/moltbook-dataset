#!/usr/bin/env python3
"""
Maintain a persistent, monotonic all-time download ledger.

The per-platform APIs are inconsistent. Hugging Face's default `downloads`
field is a rolling 30-day count, not a cumulative one, and any platform fetch
can transiently fail. A naive sum therefore wobbles and undercounts. This
ledger folds each fresh reading into a persistent high-water mark per platform
(carry forward on a failed read, never drop to zero) and checkpoints the
cumulative totals at each month boundary, so a month's downloads are a simple
difference of two checkpoints.

Cumulative, all-time inputs expected per platform:
  zenodo       sum of stats.downloads across versions
  huggingface  downloadsAllTime   (NOT the rolling `downloads` field)
  github       sum of asset.download_count across releases
  kaggle       totalDownloads

Usage:
  uv run python scripts/download_ledger.py
      Fold data/derived/download_stats.json into the ledger.

  uv run python scripts/download_ledger.py --backfill-git --hf-alltime 3370
      Rebuild the monthly history of the cumulative platforms from the git log
      of README.md, then seed the Hugging Face all-time baseline.
"""

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

DERIVED_DIR = Path("data/derived")
# data/ is gitignored and rebuilt each CI run, so the ledger lives at the repo
# root (like ZENODO.json) where it persists across runs.
LEDGER_PATH = Path("download_ledger.json")
STATS_PATH = DERIVED_DIR / "download_stats.json"

PLATFORMS = ["zenodo", "huggingface", "github", "kaggle"]


def load_ledger():
    if LEDGER_PATH.exists():
        with open(LEDGER_PATH) as f:
            return json.load(f)
    return {"all_time": {}, "monthly": {}, "monthly_new": {}, "urls": {},
            "total_all_time": 0, "updated_at": None, "notes": {}}


def save_ledger(ledger):
    tmp = str(LEDGER_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
    Path(tmp).replace(LEDGER_PATH)


def month_of(iso):
    return iso[:7]


def fold_reading(ledger, reading, month):
    """Fold one {platform: count_or_None} reading into the ledger.

    Monotonic high-water mark per platform; a None (failed fetch) carries the
    last known value forward instead of dropping the platform to zero. The
    month checkpoint records the cumulative all-time per platform as of the
    latest reading seen in that month.
    """
    all_time = ledger["all_time"]
    for p in PLATFORMS:
        value = reading.get(p)
        if value is not None:
            all_time[p] = max(all_time.get(p, 0), int(value))
    checkpoint = ledger["monthly"].setdefault(month, {})
    for p in PLATFORMS:
        if p in all_time:
            checkpoint[p] = all_time[p]
    ledger["total_all_time"] = sum(all_time.values())


def recompute_monthly_new(ledger):
    """Derive per-month new downloads from consecutive month checkpoints.

    A platform contributes to a month only when both that month and the prior
    month have a real checkpoint for it, so a platform seeded as a baseline
    (e.g. Hugging Face) does not show a phantom spike in its first month.
    """
    months = sorted(ledger["monthly"])
    monthly_new = {}
    for i, m in enumerate(months):
        prev = ledger["monthly"][months[i - 1]] if i > 0 else {}
        cur = ledger["monthly"][m]
        delta = {}
        for p in PLATFORMS:
            if p in cur and p in prev:
                delta[p] = cur[p] - prev[p]
            elif p in cur and i == 0:
                delta[p] = cur[p]  # first tracked month = baseline-in
            else:
                delta[p] = None
        delta["total"] = sum(v for v in delta.values() if v is not None)
        monthly_new[m] = delta
    ledger["monthly_new"] = monthly_new


# --- backfill from the git history of README.md -----------------------------

LABELS = {"zenodo": "Zenodo", "huggingface": "Hugging Face",
          "github": "GitHub", "kaggle": "Kaggle"}


def _downloads_block(readme):
    m = re.search(r"DOWNLOADS_START(.*?)DOWNLOADS_END", readme, re.S)
    return m.group(1) if m else None


def parse_counts(readme):
    block = _downloads_block(readme)
    if block is None:
        return None
    out = {}
    for key, label in LABELS.items():
        mm = re.search(rf"\|\s*\[?{label}[^|]*\]?[^|]*\|\s*([\d,]+)\s*\|", block)
        out[key] = int(mm.group(1).replace(",", "")) if mm else None
    return out


def parse_urls(readme):
    block = _downloads_block(readme)
    urls = {}
    if block is None:
        return urls
    for key, label in LABELS.items():
        mm = re.search(rf"\[{label}[^\]]*\]\((https?://[^)]+)\)", block)
        if mm:
            urls[key] = mm.group(1)
    return urls


def backfill_from_git(ledger, hf_alltime):
    log = subprocess.run(
        ["git", "log", "--reverse", "--format=%H|%cI", "--", "README.md"],
        capture_output=True, text=True,
    ).stdout.splitlines()

    snapshots = 0
    for line in log:
        h, iso = line.split("|")
        readme = subprocess.run(
            ["git", "show", f"{h}:README.md"], capture_output=True, text=True
        ).stdout
        counts = parse_counts(readme)
        if counts is None:
            continue
        # Accumulate URLs across snapshots; a later snapshot that drops a row
        # (e.g. a failed Hugging Face fetch) keeps the earlier URL.
        ledger["urls"].update(parse_urls(readme))
        # Cumulative platforms only. Hugging Face is rolling-30 in history and
        # is not cumulative, so it is excluded here and seeded separately below.
        reading = {p: counts.get(p) for p in ("zenodo", "github", "kaggle")}
        fold_reading(ledger, reading, month_of(iso))
        snapshots += 1

    if hf_alltime is not None:
        ledger["all_time"]["huggingface"] = int(hf_alltime)
        ledger["total_all_time"] = sum(ledger["all_time"].values())
        ledger["notes"]["huggingface"] = (
            f"all-time baseline {int(hf_alltime)} seeded from downloadsAllTime; "
            "monthly tracking starts at the next reading. History excluded "
            "because the README stored the rolling 30-day field."
        )
    recompute_monthly_new(ledger)
    return snapshots


# --- forward path: fold today's download_stats.json -------------------------

def fold_stats_json(ledger):
    if not STATS_PATH.exists():
        raise SystemExit(f"{STATS_PATH} not found; run download_stats.py first")
    with open(STATS_PATH) as f:
        stats = json.load(f)
    platforms = stats.get("platforms", {})
    reading = {}
    for p in PLATFORMS:
        if p in platforms:
            reading[p] = platforms[p].get("downloads")
            if platforms[p].get("url"):
                ledger["urls"][p] = platforms[p]["url"]
        else:
            reading[p] = None
    month = month_of(stats.get("updated_at") or datetime.now(timezone.utc).isoformat())
    fold_reading(ledger, reading, month)
    recompute_monthly_new(ledger)
    return reading


def fmt(n):
    return "      --" if n is None else f"{n:>8,}"


def print_report(ledger):
    at = ledger["all_time"]
    print("All-time downloads")
    print("-" * 34)
    for p in PLATFORMS:
        print(f"  {LABELS[p]:<14} {at.get(p, 0):>10,}")
    print(f"  {'Total':<14} {ledger['total_all_time']:>10,}")
    print("\nNew downloads by month")
    print("-" * 60)
    print(f"  {'month':<9}" + "".join(f"{LABELS[p][:8]:>9}" for p in PLATFORMS) + f"{'total':>9}")
    for m in sorted(ledger["monthly_new"]):
        row = ledger["monthly_new"][m]
        print(f"  {m:<9}" + "".join(fmt(row.get(p)) + " " for p in PLATFORMS) + fmt(row.get("total")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-git", action="store_true",
                    help="rebuild monthly history from the README git log")
    ap.add_argument("--hf-alltime", type=int, default=None,
                    help="Hugging Face downloadsAllTime baseline to seed")
    args = ap.parse_args()

    ledger = load_ledger()
    if args.backfill_git:
        n = backfill_from_git(ledger, args.hf_alltime)
        print(f"Backfilled {n} snapshots from git history\n")
    else:
        reading = fold_stats_json(ledger)
        ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
        print(f"Folded reading: {reading}\n")

    save_ledger(ledger)
    print_report(ledger)
    print(f"\nSaved to {LEDGER_PATH}")


if __name__ == "__main__":
    main()
