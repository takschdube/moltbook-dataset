#!/usr/bin/env python3
"""Publish the dataset to Hugging Face as one revision per crawl.

Two things this is careful about.

Each crawl lands as a single commit rather than one commit per file. The
revision history is the durable archive of this dataset, since per-run GitHub
releases are pruned, and a history of fourteen commits per crawl is far harder
to walk than one. The commit message carries the crawl timestamp and corpus
size so a revision describes itself without a lookup.

Large files move on a daily cadence rather than every six hours. The corpus is
several gigabytes and every upload stores another immutable copy, so pushing it
four times a day would grow the repository by tens of gigabytes a week to
record a few thousand new posts. Small files still move every crawl, so the
index, the crawl history and the manifests stay current.

Environment:
    HF_TOKEN            required
    HF_REPO             defaults to takschdube/moltbook-dataset
    HF_UPLOAD_LARGE     "1" forces large files, "0" suppresses them
    HF_LARGE_THRESHOLD  bytes above which a file is treated as large
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, create_repo

HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO = os.getenv("HF_REPO", "takschdube/moltbook-dataset")
RAW_DIR = Path("data/raw")
DERIVED_DIR = Path("data/derived")
LARGE_THRESHOLD = int(os.getenv("HF_LARGE_THRESHOLD", str(32 * 1024 * 1024)))
# Crawls run at 00, 06, 12 and 18 UTC. Sending large files only on the first of
# the day keeps one full snapshot per day in the history.
LARGE_UPLOAD_HOUR_BEFORE = 6


def corpus_state():
    for path in (Path("corpus_state.json"), RAW_DIR / "corpus_state.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def crawl_stamp(state):
    stamp = state.get("updated_at")
    if stamp:
        return stamp
    try:
        return json.loads((RAW_DIR / "metadata.json").read_text(encoding="utf-8"))["last_crawl"]
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def send_large_files():
    forced = os.getenv("HF_UPLOAD_LARGE")
    if forced is not None:
        return forced == "1"
    return datetime.now(timezone.utc).hour < LARGE_UPLOAD_HOUR_BEFORE


def collect(include_large):
    operations, skipped = [], []
    for directory, prefix in [(RAW_DIR, "raw"), (DERIVED_DIR, "derived")]:
        if not directory.exists():
            continue
        for path in sorted(list(directory.glob("*.json")) + list(directory.glob("*.csv"))):
            if path.stat().st_size > LARGE_THRESHOLD and not include_large:
                skipped.append(path.name)
                continue
            operations.append(CommitOperationAdd(f"{prefix}/{path.name}", str(path)))
    readme = Path("README.md")
    if readme.exists():
        operations.append(CommitOperationAdd("README.md", str(readme)))
    return operations, skipped


def main():
    if not HF_TOKEN:
        print("HF_TOKEN not set, skipping Hugging Face upload")
        return 0

    try:
        create_repo(repo_id=HF_REPO, token=HF_TOKEN, repo_type="dataset",
                    exist_ok=True, private=False)
    except Exception as e:
        print(f"Error preparing repo: {e}")
        return 1

    include_large = send_large_files()
    operations, skipped = collect(include_large)
    if not operations:
        print("nothing to upload")
        return 0

    state = corpus_state()
    stamp = crawl_stamp(state)
    posts = state.get("posts")
    summary = f"crawl {stamp[:19]}Z" if not stamp.endswith("Z") else f"crawl {stamp[:19]}"
    if posts:
        summary += f" ({posts:,} posts)"
    if not include_large:
        summary += ", incremental"

    print(f"Uploading {len(operations)} files to {HF_REPO}")
    if skipped:
        print(f"  holding {len(skipped)} large files for the daily snapshot: {', '.join(skipped)}")

    try:
        info = HfApi().create_commit(
            repo_id=HF_REPO,
            repo_type="dataset",
            operations=operations,
            commit_message=summary,
            token=HF_TOKEN,
        )
    except Exception as e:
        print(f"Upload failed: {e}")
        return 1

    revision = getattr(info, "oid", None) or ""
    print(f"  committed {summary}" + (f" as {revision[:12]}" if revision else ""))
    if revision:
        Path("hf_revision.txt").write_text(revision, encoding="utf-8")
    print(f"https://huggingface.co/datasets/{HF_REPO}")
    return 0


def _demo():
    os.environ["HF_UPLOAD_LARGE"] = "1"
    assert send_large_files() is True
    os.environ["HF_UPLOAD_LARGE"] = "0"
    assert send_large_files() is False
    del os.environ["HF_UPLOAD_LARGE"]
    assert isinstance(send_large_files(), bool)
    assert crawl_stamp({"updated_at": "2026-09-15T02:00:00+00:00"}).startswith("2026-09-15")
    print("ok")


if __name__ == "__main__":
    sys.exit(_demo() if "--demo" in sys.argv else main())
