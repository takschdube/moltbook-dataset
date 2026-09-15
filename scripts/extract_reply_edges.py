#!/usr/bin/env python3
"""Flatten the comment forest into one row per comment, with full lineage.

Every comment is emitted with its root post, its parent comment, the post
author, the comment author, and timestamps, so reply ties can be computed over
any window without re-walking the nested JSON.

Depth is computed from the nesting rather than read from the record: the
platform added an explicit depth field partway through collection, so older
records do not carry it. parent_comment_id is empty for a top-level comment,
whose parent is the post itself; treating that as a missing parent is what made
the published reply graph drop every reply made to a poster.

Restricting to a completeness cohort matters for absence claims. A post whose
comments were never retrieved looks exactly like a post nobody replied to, so
threads outside the cohort must not be read as evidence that a reply did not
happen.

Usage:
    python scripts/extract_reply_edges.py posts_full.json out.csv \\
        [--cohort cohort.csv] [--classes complete,never_had_comments] \\
        [--since 2026-01-27] [--until 2026-03-01]
"""
import argparse
import csv
import json
import sys

FIELDS = ["comment_id", "root_post_id", "parent_comment_id", "depth",
          "author_id", "author_name", "created_at", "updated_at", "is_deleted",
          "root_author_id", "root_author_name", "root_created_at",
          "root_completeness", "submolt", "content_chars"]


def load_cohort(path, keep_classes):
    """post_id -> completeness class, restricted to the classes we keep."""
    cohort = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not keep_classes or row["class"] in keep_classes:
                cohort[row["post_id"]] = row["class"]
    return cohort


def stream(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if line and line not in "[]":
                yield json.loads(line)


def walk(comments, root, depth, parent_id, out):
    for c in comments or []:
        author = c.get("author") or {}
        out.append([
            c.get("id"), root["post_id"], parent_id or "", depth,
            c.get("author_id") or author.get("id") or "", author.get("name") or "",
            c.get("created_at") or "", c.get("updated_at") or "",
            "" if c.get("is_deleted") is None else int(bool(c.get("is_deleted"))),
            root["author_id"], root["author_name"], root["created_at"],
            root["completeness"], root["submolt"],
            len(c.get("content") or ""),
        ])
        walk(c.get("replies"), root, depth + 1, c.get("id"), out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("posts_full")
    ap.add_argument("out_csv")
    ap.add_argument("--cohort")
    ap.add_argument("--classes", default="",
                    help="comma-separated completeness classes to keep")
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    args = ap.parse_args()

    keep = {c.strip() for c in args.classes.split(",") if c.strip()}
    cohort = load_cohort(args.cohort, keep) if args.cohort else None
    if cohort is not None:
        print(f"cohort: {len(cohort)} posts", file=sys.stderr)

    posts = kept = 0
    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for p in stream(args.posts_full):
            posts += 1
            created = p.get("created_at") or ""
            if args.since and created[:10] < args.since:
                continue
            if args.until and created[:10] >= args.until:
                continue
            if cohort is not None and p["id"] not in cohort:
                continue
            author = p.get("author") or {}
            # Older records store submolt as an object; newer ones as a name.
            submolt = p.get("submolt")
            if isinstance(submolt, dict):
                submolt = submolt.get("name") or submolt.get("display_name") or ""
            root = {
                "post_id": p["id"],
                "author_id": p.get("author_id") or author.get("id") or "",
                "author_name": author.get("name") or "",
                "created_at": created,
                "completeness": (cohort or {}).get(p["id"], ""),
                "submolt": submolt or "",
            }
            rows = []
            walk(p.get("comments"), root, 0, None, rows)
            w.writerows(rows)
            kept += len(rows)
            if posts % 50000 == 0:
                print(f"  {posts} posts, {kept} comments", file=sys.stderr)

    print(f"{kept} comments from {posts} posts scanned -> {args.out_csv}", file=sys.stderr)


def _demo():
    root = {"post_id": "p1", "author_id": "u-poster", "author_name": "poster",
            "created_at": "2026-02-01T00:00:00Z", "completeness": "complete",
            "submolt": "general"}
    tree = [{"id": "c1", "author_id": "u-a", "author": {"name": "alice"},
             "created_at": "t1", "content": "hello",
             "replies": [{"id": "c2", "author_id": "u-b", "author": {"name": "bob"},
                          "created_at": "t2", "content": "hi", "is_deleted": False,
                          "replies": []}]}]
    out = []
    walk(tree, root, 0, None, out)
    assert len(out) == 2
    c1, c2 = out
    # top-level comment: no parent comment, parent is the post author
    assert c1[FIELDS.index("parent_comment_id")] == ""
    assert c1[FIELDS.index("depth")] == 0
    assert c1[FIELDS.index("root_author_id")] == "u-poster"
    # nested reply: parent is the comment above it, depth increments
    assert c2[FIELDS.index("parent_comment_id")] == "c1"
    assert c2[FIELDS.index("depth")] == 1
    assert c2[FIELDS.index("is_deleted")] == 0
    # absent deletion marker stays empty rather than defaulting to false
    assert c1[FIELDS.index("is_deleted")] == ""
    assert c1[FIELDS.index("content_chars")] == 5
    assert c1[FIELDS.index("submolt")] == "general"
    print("ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        main()
