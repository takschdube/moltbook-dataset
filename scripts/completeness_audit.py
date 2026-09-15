"""Partition posts_full.json into fetch-completeness classes.

posts_full.json is written one post per line (see export_posts_full_json),
so it streams without loading 478MB into memory.

Classes:
  placeholder   comment_count > 0 but comments == []  -> provably never fetched
                (ensure_all_posts_in_full backfills these)
  empty_ok      comment_count == 0 and comments == []  -> consistent with a true empty
  short         0 < tree_size < comment_count          -> fetched but under-count
  complete      tree_size >= comment_count             -> consistent with complete

Only empty_ok and complete support an absence claim.

Usage: python completeness_audit.py path/to/posts_full.json [cohort.csv]
"""
import json
import sys
from collections import Counter


def tree_size(comments):
    return sum(1 + tree_size(c.get("replies")) for c in comments or [])


def classify(post):
    n = tree_size(post.get("comments"))
    claimed = post.get("comment_count") or 0
    if n == 0:
        return "empty_ok" if claimed == 0 else "placeholder"
    return "complete" if n >= claimed else "short"


def main(path, out=None):
    counts = Counter()
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if not line or line in "[]":
                continue
            post = json.loads(line)
            cls = classify(post)
            counts[cls] += 1
            rows.append((post["id"], post.get("created_at", ""), cls,
                         tree_size(post.get("comments")), post.get("comment_count") or 0))

    total = sum(counts.values())
    for cls, n in counts.most_common():
        print(f"{cls:12s} {n:7d}  {100 * n / total:5.1f}%")
    usable = counts["empty_ok"] + counts["complete"]
    print(f"\nusable for absence claims: {usable}/{total} ({100 * usable / total:.1f}%)")

    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write("post_id,created_at,class,tree_size,comment_count\n")
            for r in rows:
                f.write(",".join(map(str, r)) + "\n")
        print(f"wrote {out}")


def _demo():
    assert classify({"comment_count": 2, "comments": []}) == "placeholder"
    assert classify({"comment_count": 0, "comments": []}) == "empty_ok"
    assert classify({"comment_count": 5, "comments": [{"replies": []}]}) == "short"
    assert classify({"comment_count": 1, "comments": [{"replies": [{"replies": []}]}]}) == "complete"
    assert tree_size([{"replies": [{"replies": []}, {"replies": []}]}]) == 3
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        _demo()
    else:
        main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
