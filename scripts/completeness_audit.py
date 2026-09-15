"""Partition posts_full.json by whether its comment layer can be trusted.

posts_full.json is written one post per line (see export_posts_full_json), so it
streams without loading the whole file.

comment_count is the platform's own figure and comments is what the collector
retrieved, so the two together classify every post without needing fetch logs:

  not_fetched        claimed > 0, retrieved 0. Proof of non-retrieval. A reply
                     could have existed and been missed.
  never_had_comments claimed 0, retrieved 0. Consistent with a thread nobody
                     replied to.
  complete           retrieved >= claimed.
  near_complete      short by at most 2, and at least 90 percent retrieved.
                     Consistent with a comment deleted after the count was
                     taken rather than with truncation.
  partial            short by more. The tree was truncated.

Only never_had_comments and complete support an unqualified absence claim.
near_complete supports one if a single deleted comment is tolerable; that is the
caller's judgement, which is why it is a separate class rather than folded in.

Usage: python completeness_audit.py path/to/posts_full.json [cohort.csv]
"""
import json
import sys
from collections import Counter

NEAR_COMPLETE_GAP = 2
NEAR_COMPLETE_RATIO = 0.9


def tree_size(comments):
    """Total comments in the nested tree. comment_count is a whole-tree figure,
    so comparing against len(comments) would undercount every threaded post."""
    return sum(1 + tree_size(c.get("replies")) for c in comments or [])


def classify(retrieved, claimed):
    if retrieved == 0:
        return "never_had_comments" if claimed == 0 else "not_fetched"
    if retrieved >= claimed:
        return "complete"
    gap = claimed - retrieved
    if gap <= NEAR_COMPLETE_GAP and retrieved >= claimed * NEAR_COMPLETE_RATIO:
        return "near_complete"
    return "partial"


def stream(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if line and line not in "[]":
                yield json.loads(line)


def main(path, out=None):
    counts = Counter()
    rows = []
    for post in stream(path):
        retrieved = tree_size(post.get("comments"))
        claimed = post.get("comment_count") or 0
        cls = classify(retrieved, claimed)
        counts[cls] += 1
        # Records collected before the platform's schema change carry the
        # author id only inside the nested author object.
        author = post.get("author") or {}
        author_id = post.get("author_id") or author.get("id") or ""
        rows.append((post["id"], post.get("created_at", ""), author_id,
                     author.get("name", ""), cls, retrieved, claimed,
                     max(0, claimed - retrieved), post.get("comments_fetched_at", "")))

    total = sum(counts.values())
    for cls in ("complete", "near_complete", "partial", "never_had_comments", "not_fetched"):
        n = counts[cls]
        print(f"{cls:20s} {n:8d}  {100 * n / total:5.1f}%")

    strict = counts["complete"] + counts["never_had_comments"]
    relaxed = strict + counts["near_complete"]
    print(f"\nusable for absence claims")
    print(f"  strict  (complete + never_had_comments)  {strict:8d}  {100 * strict / total:5.1f}%")
    print(f"  relaxed (+ near_complete)                {relaxed:8d}  {100 * relaxed / total:5.1f}%")

    if out:
        import csv
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["post_id", "created_at", "author_id", "author_name", "class",
                        "comments_retrieved", "comments_claimed", "shortfall",
                        "comments_fetched_at"])
            w.writerows(rows)
        print(f"wrote {out}")


def _demo():
    assert classify(0, 2) == "not_fetched"
    assert classify(0, 0) == "never_had_comments"
    assert classify(5, 5) == "complete"
    assert classify(6, 5) == "complete"
    # one comment deleted after the count was taken
    assert classify(49, 50) == "near_complete"
    # short by 2 but only 60 percent retrieved: truncation, not a deletion
    assert classify(3, 5) == "partial"
    assert classify(38, 2565) == "partial"
    assert tree_size([{"replies": [{"replies": []}, {"replies": []}]}]) == 3
    assert tree_size(None) == 0
    print("ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
