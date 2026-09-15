#!/usr/bin/env python3
"""Reconstruct per-crawl provenance from the Hugging Face revision history.

The crawler did not record per-crawl history durably until 2026-09-15: metadata.json
is regenerated each CI run and was never restored, so it only ever held the current
run. But every published revision carries that file, and the Hugging Face mirror kept
every revision. Walking it recovers the history the database lost.

For each crawl this emits the true UTC crawl time (from inside the revision, not the
commit date, which is a push event), the corpus size, the request and error counts,
the revision holding that state, and which version of the collector was deployed.

Requires a clone of the HF dataset repo. GIT_LFS_SKIP_SMUDGE=1 is enough; no large
files are read.

Usage:
    python scripts/reconstruct_history.py <hf_clone> <out.csv> [--code-repo .]
"""
import argparse
import csv
import json
import subprocess
import sys


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True).stdout


def code_versions(repo):
    """Commits that changed the collector, newest first, as (iso_date, sha, subject)."""
    out = []
    for line in git(repo, "log", "--format=%cI|%h|%s", "--", "moltbook_crawler.py").splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            out.append(tuple(parts))
    return out


def version_at(versions, when):
    """The collector commit in force at a given time."""
    for date, sha, subject in versions:
        if date <= when:
            return sha, subject
    return "", ""


def walk(hf_repo, code_repo):
    # Only commits that touched metadata.json: one per crawl rather than the
    # dozen-plus per-file commits each publish creates.
    shas = git(hf_repo, "log", "--format=%H", "--", "raw/metadata.json").split()
    versions = code_versions(code_repo)
    print(f"{len(shas)} revisions touch raw/metadata.json", file=sys.stderr)

    crawls = {}
    for sha in shas:
        blob = git(hf_repo, "show", f"{sha}:raw/metadata.json")
        try:
            meta = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        when = meta.get("last_crawl")
        if not when:
            continue
        stats = (meta.get("crawl_history") or [{}])[0].get("stats") or {}
        code_sha, code_subject = version_at(versions, when)
        # git log is newest-first, so the first sighting of a crawl is its
        # latest published revision. Keep that one.
        crawls.setdefault(when, {
            "crawl_time_utc": when,
            "hf_revision": sha,
            "posts": stats.get("posts"),
            "posts_full": stats.get("posts_full"),
            "submolts": stats.get("submolts"),
            "requests": stats.get("requests"),
            "errors": stats.get("errors"),
            "error_rate": (round(stats["errors"] / stats["requests"], 6)
                           if stats.get("requests") else None),
            "collector_commit": code_sha,
            "collector_change": code_subject,
        })
    return [crawls[k] for k in sorted(crawls)]


def annotate_discontinuities(rows, drop_fraction=0.5):
    """Flag crawls where the corpus shrank sharply and stayed small.

    Requiring persistence matters: metadata.json was occasionally written before
    the crawl populated, producing a single zero row that recovers on the next
    crawl. That is a reporting artifact. A real loss is still there afterwards.
    """
    # metadata.json was not refreshed between 2026-02-16 and 2026-03-31, so it
    # repeats one reading for six weeks while the corpus was in fact growing.
    # Flag repeats instead of letting a reader take them as a flat corpus.
    prev_row = None
    for r in rows:
        keys = ("posts", "posts_full", "requests", "errors")
        r["stats_repeat_previous"] = bool(
            prev_row and all(r.get(k) == prev_row.get(k) for k in keys))
        prev_row = r

    prev = None
    for i, r in enumerate(rows):
        posts = r.get("posts") or 0
        r["corpus_delta"] = None if prev is None else posts - prev
        dropped = bool(prev and posts < prev * drop_fraction)
        nxt = rows[i + 1].get("posts") if i + 1 < len(rows) else None
        persisted = nxt is None or (prev and nxt < prev * drop_fraction)
        r["discontinuity"] = dropped and persisted
        prev = posts or prev
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hf_clone")
    ap.add_argument("out_csv")
    ap.add_argument("--code-repo", default=".")
    args = ap.parse_args()

    rows = annotate_discontinuities(walk(args.hf_clone, args.code_repo))
    if not rows:
        print("no crawls recovered", file=sys.stderr)
        return 1

    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    losses = [r for r in rows if r["discontinuity"]]
    print(f"{len(rows)} crawls from {rows[0]['crawl_time_utc'][:10]} "
          f"to {rows[-1]['crawl_time_utc'][:10]} -> {args.out_csv}")
    for r in losses:
        print(f"  DISCONTINUITY {r['crawl_time_utc'][:19]}  "
              f"{r['posts']} posts ({r['corpus_delta']:+d})")
    return 0


def _demo():
    rows = annotate_discontinuities([{"posts": 100}, {"posts": 110}, {"posts": 3}, {"posts": 9}])
    assert [r["discontinuity"] for r in rows] == [False, False, True, False]
    assert rows[1]["corpus_delta"] == 10
    rep = annotate_discontinuities([{"posts": 5, "requests": 1}, {"posts": 5, "requests": 1},
                                    {"posts": 9, "requests": 2}])
    assert [r["stats_repeat_previous"] for r in rep] == [False, True, False]
    vs = [("2026-07-17T00:00:00+00:00", "c6199f3", "engagement history"),
          ("2026-04-11T00:00:00+00:00", "ca5d52f", "submolt progress")]
    assert version_at(vs, "2026-06-01T00:00:00+00:00")[0] == "ca5d52f"
    assert version_at(vs, "2026-08-01T00:00:00+00:00")[0] == "c6199f3"
    assert version_at(vs, "2026-01-01T00:00:00+00:00")[0] == ""
    print("ok")


if __name__ == "__main__":
    sys.exit(_demo() if "--demo" in sys.argv else main())
