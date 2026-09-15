"""Reconstruct historical agent identity text from the dataset's revision history.

Author records are denormalized into each post and overwritten on every crawl, so
the current files hold only the latest self-description. Earlier versions survive
only in the Hugging Face revision history. This walks that history and emits one
row per account per change.

Requires a local clone of the HF dataset repo (blobs are fetched over HTTP, so a
GIT_LFS_SKIP_SMUDGE clone is enough).

Usage: python identity_history.py <hf_clone_dir> <out.csv> [YYYY-MM-DD ...]
"""
import csv
import json
import subprocess
import sys
import urllib.request

REPO = "https://huggingface.co/datasets/takschdube/moltbook-dataset/resolve"
# posts.json carried only id and name before the platform's schema change.
DEFAULT_SOURCE = "raw/posts_full.json"
# The platform renamed these partway through collection. Read both spellings
# so a series spanning the change stays comparable.
ALIASES = {
    "id": ("id",),
    "name": ("name",),
    "description": ("description",),
    "karma": ("karma",),
    "follower_count": ("followerCount", "follower_count"),
    "following_count": ("followingCount", "following_count"),
    "created_at": ("createdAt", "created_at"),
    "last_active": ("lastActive", "last_active"),
    "is_active": ("isActive", "is_active"),
    "is_claimed": ("isClaimed", "is_claimed"),
    "deleted_at": ("deletedAt", "deleted_at"),
    "owner": ("owner",),
}
FIELDS = list(ALIASES)


def field(author, name):
    for key in ALIASES[name]:
        if key in author:
            return author[key]
    return ""


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout


def crawl_index(repo, since, until):
    """Map each commit in range to the crawl timestamp recorded inside it."""
    out = {}
    for sha in git(repo, "log", "--format=%H", "--since", since, "--until", until).split():
        blob = git(repo, "show", f"{sha}:raw/metadata.json")
        try:
            t = json.loads(blob).get("last_crawl")
        except Exception:
            continue
        if t:
            out.setdefault(t, sha)
    return sorted(out.items())


def revision_before(index, cutoff):
    prior = [x for x in index if x[0] < cutoff]
    return prior[-1] if prior else None


def authors_from(stream):
    """Author records keyed by account id, read from a posts export.

    The export format changed when storage moved to SQLite on 2026-03-31: older
    revisions are pretty-printed JSON, newer ones are one post per line. ijson
    streams both without loading a multi-gigabyte file into memory.
    """
    import ijson

    seen = {}
    for post in ijson.items(stream, "item"):
        a = post.get("author")
        if isinstance(a, dict) and a.get("id"):
            seen[a["id"]] = a
    return seen


def authors_at(sha, source=DEFAULT_SOURCE):
    """Author records at one revision.

    Read posts_full.json rather than posts.json: before the platform's schema
    change the listing endpoint returned only an account's id and name, so the
    self-description exists only in the per-post responses. posts_full is large
    but it is the only place the identity text lives for that period.
    """
    with urllib.request.urlopen(f"{REPO}/{sha}/{source}") as r:
        return authors_from(r)


def authors_from_file(path):
    with open(path, "rb") as f:
        return authors_from(f)


def main(repo, out_path, dates, source=DEFAULT_SOURCE, local=None):
    index = crawl_index(repo, min(dates) + "T00:00:00", max(dates) + "T23:59:59")
    print(f"{len(index)} distinct crawls in range")

    last = {}          # author_id -> tuple of tracked values
    rows = []
    for d in dates:
        hit = revision_before(index, f"{d}T23:59:59")
        if not hit:
            print(f"{d}: no crawl found, skipped")
            continue
        observed, sha = hit
        people = authors_from_file(local[d]) if local and d in local else authors_at(sha, source)
        changed = 0
        for aid, a in people.items():
            vals = tuple(str(field(a, f)) for f in FIELDS)
            if last.get(aid) != vals:
                rows.append([observed, sha[:12]] + list(vals))
                last[aid] = vals
                changed += 1
        print(f"{d}  crawl={observed[:19]}  accounts={len(people):5d}  changed={changed:5d}")

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["observed_at", "revision"] + FIELDS)
        w.writerows(rows)
    print(f"\nwrote {len(rows)} rows to {out_path}")


if __name__ == "__main__":
    args = sys.argv[1:]
    source = DEFAULT_SOURCE
    if "--source" in args:
        i = args.index("--source")
        source = args[i + 1]
        del args[i:i + 2]
    # --local YYYY-MM-DD=path reads an already-downloaded export for that date
    local = {}
    while "--local" in args:
        i = args.index("--local")
        date, _, path = args[i + 1].partition("=")
        local[date] = path
        del args[i:i + 2]
    main(args[0], args[1], args[2:], source, local)
