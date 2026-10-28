#!/usr/bin/env python3
"""Fill the months the offset-paged crawler reached only in part, as monthly shards beside the working corpus.

Until 2026-10-28 the crawler paged Moltbook's listings by offset, which the API ignores, so the corpus
holds every post to 7 February 2026 and a small share of later ones. The shards hold every post the API
serves for each month from February to October 2026, with each post's comment tree, and keep the working
database inside its 2 GB release asset and the runner's disk.

    next      print the next unfinished step as "<month> <step>", or nothing once every month is done
    posts     walk /posts?sort=new by cursor from the end of the month to its start, resuming below the
              oldest post already written (a sort=new cursor is base64 JSON {createdAt, id}, so a walk
              can start at any time)
    comments  fetch the whole comment tree of every post in the month's shard that reports comments,
              skipping threads already written; a failed fetch is left for the next run

Shards in history/: posts-YYYY-MM.jsonl (one post per line), comments-YYYY-MM.jsonl (one thread per line:
post_id, fetched_at, comment_count, comments) and manifest-YYYY-MM.json. The workflow compresses them with
zstd and keeps them on the `history` release.

    uv run python scripts/backfill_history.py next
    uv run python scripts/backfill_history.py posts 2026-03 --time-budget 300
    uv run python scripts/backfill_history.py comments 2026-03 --time-budget 300
"""
import argparse
import base64
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import moltbook_crawler as mc  # noqa: E402

MONTHS = [f"2026-{m:02d}" for m in range(2, 11)]
END = "2026-10-28T12:00:00.000Z"   # the fixed crawler's first walk reaches back past this
HIST = Path("history")
BATCH = 200


def when(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def bounds(month):
    y, m = map(int, month.split("-"))
    start = f"{month}-01T00:00:00.000Z"
    end = END if month == MONTHS[-1] else f"{y + m // 12}-{m % 12 + 1:02d}-01T00:00:00.000Z"
    return start, end


def cursor(created_at, post_id):
    return base64.b64encode(json.dumps({"createdAt": created_at, "id": post_id}).encode()).decode()


def lines(path):
    """The shard's records, one at a time: a month of posts or threads runs to gigabytes."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:   # a line cut short when a run stopped mid-write
                pass


def manifest(month):
    p = HIST / f"manifest-{month}.json"
    return json.loads(p.read_text()) if p.exists() else {"month": month, "posts": 0, "posts_done": False, "threads": 0, "threads_done": False}


def save(month, m):
    (HIST / f"manifest-{month}.json").write_text(json.dumps(m, indent=1) + "\n")


def next_step():
    for month in MONTHS:
        m = manifest(month)
        if not m["posts_done"]:
            return f"{month} posts"
        if not m["threads_done"]:
            return f"{month} comments"
    return ""


def posts(month):
    start, end = bounds(month)
    path, m = HIST / f"posts-{month}.jsonl", manifest(month)
    seen, oldest = set(), None
    for p in lines(path):
        seen.add(p["id"])
        if oldest is None or (when(p["created_at"]), p["id"]) < (when(oldest[0]), oldest[1]):
            oldest = (p["created_at"], p["id"])
    cur = cursor(*oldest) if oldest else cursor(end, "ffffffff-ffff-ffff-ffff-ffffffffffff")
    done = False
    with open(path, "a", encoding="utf-8") as f:
        while mc.has_time(reserve_minutes=10):
            resp = mc.make_request("/posts", {"sort": "new", "limit": 50, "cursor": cur})
            if not resp or not resp.get("success"):
                break   # left for the next run, which resumes below the oldest post written
            batch = resp.get("posts") or []
            for p in batch:
                if when(p["created_at"]) < when(start):
                    done = True
                    break
                if p["id"] not in seen and when(p["created_at"]) < when(end):
                    seen.add(p["id"])
                    f.write(json.dumps(p, ensure_ascii=False) + "\n")
            cur = resp.get("next_cursor")
            if done or not batch or not (resp.get("has_more") and cur):
                done = True
                break
            time.sleep(mc.REQUEST_DELAY)
    m.update(posts=len(seen), posts_done=done)
    save(month, m)
    mc.logger.log(f"{month} posts: {len(seen)} written, {'complete' if done else 'to be resumed'}")


def comments(month):
    m = manifest(month)
    if not m["posts_done"]:
        return posts(month)
    threads = [(p["id"], p.get("comment_count") or 0) for p in lines(HIST / f"posts-{month}.jsonl") if (p.get("comment_count") or 0) > 0]
    path = HIST / f"comments-{month}.jsonl"
    done = {t["post_id"] for t in lines(path)}
    todo = [t for t in threads if t[0] not in done]
    failed = 0
    with open(path, "a", encoding="utf-8") as f, ThreadPoolExecutor(mc.COMMENT_WORKERS) as ex:
        for i in range(0, len(todo), BATCH):
            if not mc.has_time(reserve_minutes=10):
                break
            chunk = todo[i:i + BATCH]
            for (pid, claimed), (tree, _) in zip(chunk, ex.map(mc.fetch_comments_only, [pid for pid, _ in chunk])):
                if tree is None:
                    failed += 1
                    continue
                done.add(pid)
                f.write(json.dumps({"post_id": pid, "fetched_at": datetime.now(timezone.utc).isoformat(),
                                    "comment_count": claimed, "comments": tree}, ensure_ascii=False) + "\n")
            f.flush()
    m.update(threads=len(done), threads_done=all(pid in done for pid, _ in threads))
    save(month, m)
    mc.logger.log(f"{month} comments: {len(done)} of {len(threads)} threads, {failed} failed this run")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["next", "posts", "comments"])
    ap.add_argument("month", nargs="?", choices=MONTHS)
    ap.add_argument("--time-budget", type=int, default=0, help="minutes; 0 runs to completion")
    a = ap.parse_args()
    HIST.mkdir(exist_ok=True)
    if a.step == "next":
        print(next_step())
        sys.exit(0)
    if not a.month:
        ap.error("posts and comments take a month")
    mc.TIME_BUDGET_MINUTES, mc._start_time = a.time_budget, datetime.now(timezone.utc)
    {"posts": posts, "comments": comments}[a.step](a.month)
