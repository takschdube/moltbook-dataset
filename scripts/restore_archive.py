#!/usr/bin/env python3
"""Merge an archived corpus snapshot into the working database.

Union, never overwrite. A post present in both keeps the newer record's fields,
because those carry current engagement figures and the newer platform schema,
but keeps whichever comment tree is larger, because an older snapshot often
holds a fuller thread than a later partial re-fetch. Restored comment trees are
stamped with `comments_source` naming the snapshot they came from, so a record's
provenance stays legible after the merge.

Runs in constant memory: the snapshot is streamed, and only the working
database is written.

Usage:
    python scripts/restore_archive.py <working.db> <posts_full.json> \\
        --source-label "0f8f0a37d15e@2026-06-19T14:47:50Z" [--posts posts.json]
        [--dry-run]
"""
import argparse
import json
import sqlite3
import sys


def tree_size(comments):
    return sum(1 + tree_size(c.get("replies")) for c in comments or [])


def stream_posts(path):
    """Yield posts from an export in either of the two formats used over time."""
    with open(path, encoding="utf-8") as f:
        first = f.readline()
        f.seek(0)
        # Post-2026-03-31 exports write one post per line; earlier ones are
        # pretty-printed and need a streaming parser.
        if first.strip() in ("[", "[]"):
            second = ""
            with open(path, encoding="utf-8") as g:
                g.readline()
                second = g.readline()
            if second.strip().startswith("{") and second.rstrip().endswith(("}", "},")):
                for line in f:
                    line = line.strip().rstrip(",")
                    if line and line not in "[]":
                        yield json.loads(line)
                return
    import ijson
    with open(path, "rb") as f:
        yield from ijson.items(f, "item")


def ensure_tables(db):
    db.execute("CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS posts_full (id TEXT PRIMARY KEY, data TEXT NOT NULL)")


def merge(db, snapshot_path, label, dry_run=False, batch=2000):
    stats = {"seen": 0, "added": 0, "comments_replaced": 0, "kept_current": 0}
    pending = 0

    for post in stream_posts(snapshot_path):
        stats["seen"] += 1
        pid = post.get("id")
        if not pid:
            continue
        incoming = post.get("comments") or []
        row = db.execute("SELECT data FROM posts_full WHERE id = ?", (pid,)).fetchone()

        if row is None:
            post["comments_source"] = label
            if not dry_run:
                db.execute("INSERT INTO posts_full (id, data) VALUES (?, ?)",
                           (pid, json.dumps(post, ensure_ascii=False)))
                if db.execute("SELECT 1 FROM posts WHERE id = ?", (pid,)).fetchone() is None:
                    bare = {k: v for k, v in post.items() if k != "comments"}
                    db.execute("INSERT INTO posts (id, data) VALUES (?, ?)",
                               (pid, json.dumps(bare, ensure_ascii=False)))
            stats["added"] += 1
        else:
            current = json.loads(row[0])
            if tree_size(incoming) > tree_size(current.get("comments")):
                # Keep the current record's fields; take the fuller thread.
                current["comments"] = incoming
                current["comments_source"] = label
                if not dry_run:
                    db.execute("UPDATE posts_full SET data = ? WHERE id = ?",
                               (json.dumps(current, ensure_ascii=False), pid))
                stats["comments_replaced"] += 1
            else:
                stats["kept_current"] += 1

        pending += 1
        if pending >= batch:
            if not dry_run:
                db.commit()
            pending = 0
            print(f"  {stats['seen']:,} scanned, {stats['added']:,} added, "
                  f"{stats['comments_replaced']:,} threads restored", file=sys.stderr, flush=True)

    if not dry_run:
        db.commit()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("database")
    ap.add_argument("snapshot")
    ap.add_argument("--source-label", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.database)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    ensure_tables(db)

    before = db.execute("SELECT COUNT(*) FROM posts_full").fetchone()[0]
    stats = merge(db, args.snapshot, args.source_label, args.dry_run)
    after = db.execute("SELECT COUNT(*) FROM posts_full").fetchone()[0]

    print(f"\nsnapshot posts scanned   {stats['seen']:,}")
    print(f"posts added              {stats['added']:,}")
    print(f"threads restored         {stats['comments_replaced']:,}")
    print(f"current record kept      {stats['kept_current']:,}")
    print(f"posts_full {before:,} -> {after:,}")
    if args.dry_run:
        print("(dry run, nothing written)")
    db.close()
    return 0


def _demo():
    db = sqlite3.connect(":memory:")
    ensure_tables(db)
    # a post already held with a thin thread, and newer scalar fields
    db.execute("INSERT INTO posts_full (id, data) VALUES (?, ?)",
               ("p1", json.dumps({"id": "p1", "score": 99, "comments": [{"replies": []}]})))
    db.execute("INSERT INTO posts (id, data) VALUES (?, ?)", ("p1", json.dumps({"id": "p1"})))
    db.commit()

    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write("[\n")
        f.write(json.dumps({"id": "p1", "score": 1,
                            "comments": [{"replies": [{"replies": []}]}, {"replies": []}]}) + ",\n")
        f.write(json.dumps({"id": "p2", "comments": [{"replies": []}]}) + "\n]")

    stats = merge(db, path, "snap@t0")
    os.unlink(path)

    p1 = json.loads(db.execute("SELECT data FROM posts_full WHERE id='p1'").fetchone()[0])
    # fuller thread taken, newer scalar kept, provenance stamped
    assert tree_size(p1["comments"]) == 3, p1
    assert p1["score"] == 99
    assert p1["comments_source"] == "snap@t0"
    p2 = json.loads(db.execute("SELECT data FROM posts_full WHERE id='p2'").fetchone()[0])
    assert p2["comments_source"] == "snap@t0"
    # a post absent from posts gets a comment-free record there too
    bare = json.loads(db.execute("SELECT data FROM posts WHERE id='p2'").fetchone()[0])
    assert "comments" not in bare
    assert stats == {"seen": 2, "added": 1, "comments_replaced": 1, "kept_current": 0}, stats
    print("ok")


if __name__ == "__main__":
    sys.exit(_demo() if "--demo" in sys.argv else main())
