#!/usr/bin/env python3
"""Append this run to snapshots.json, the tag-to-revision index.

The Hugging Face revision history is the only continuous record of this
archive: GitHub per-run releases are pruned after 14 days and only monthly
archives survive. Without this index there is no way to map a point in time to
the revision holding the corpus as it stood then.

The index was backfilled once on 2026-07-17 and nothing wrote it afterwards,
so it has a gap from then until this script was added.

Usage: uv run python scripts/write_snapshot_index.py
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

INDEX = Path("snapshots.json")
HF_REPO = os.getenv("HF_DATASET_REPO", "takschdube/moltbook-dataset")


def latest_hf_revision():
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("huggingface_hub not installed; skipping")
        return None
    token = os.getenv("HF_TOKEN")
    try:
        commits = HfApi().list_repo_commits(repo_id=HF_REPO, repo_type="dataset", token=token)
    except Exception as e:
        print(f"could not read HF commits: {e}")
        return None
    return commits[0].commit_id if commits else None


def crawl_time():
    """Prefer the crawler's own timestamp over wall clock at publish time."""
    for path, key in [("data/raw/corpus_state.json", "updated_at"),
                      ("corpus_state.json", "updated_at"),
                      ("data/raw/metadata.json", "last_crawl")]:
        try:
            with open(path, encoding="utf-8") as f:
                value = json.load(f).get(key)
            if value:
                return value
        except Exception:
            continue
    return datetime.now(timezone.utc).isoformat()


def current_tag():
    tag = os.getenv("RELEASE_TAG")
    if tag:
        return tag
    out = subprocess.run(["git", "describe", "--tags", "--abbrev=0"],
                         capture_output=True, text=True).stdout.strip()
    return out or "v" + datetime.now(timezone.utc).strftime("%Y.%m.%d-%H%M")


def main():
    revision = latest_hf_revision()
    if not revision:
        print("no revision resolved; index unchanged")
        return 0

    rows = []
    if INDEX.exists():
        try:
            rows = json.loads(INDEX.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("snapshots.json is corrupt; refusing to overwrite")
            return 1

    if rows and rows[-1].get("hf_revision") == revision:
        print("revision already indexed; nothing to do")
        return 0

    rows.append({
        "tag": current_tag(),
        "snapshot_time": crawl_time(),
        "hf_revision": revision,
        "hf_revision_is_prior_run": False,
        "github_release": True,
    })
    INDEX.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    print(f"indexed {rows[-1]['tag']} -> {revision[:12]} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
