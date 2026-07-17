#!/usr/bin/env python3
"""
Aggregate download stats across all platforms.

Queries Zenodo, Hugging Face, GitHub Releases, and Kaggle for download
counts and writes a unified summary to data/derived/download_stats.json.

Each platform query is independent — if one fails, the others still work.

Usage:
    uv run python scripts/download_stats.py
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

ZENODO_JSON = Path("ZENODO.json")
DERIVED_DIR = Path("data/derived")
OUTPUT_PATH = DERIVED_DIR / "download_stats.json"

GITHUB_REPO = "takschdube/moltbook-dataset"
HF_REPO = "takschdube/moltbook-dataset"
KAGGLE_SLUG = "takschdube/moltbook-dataset"


def load_zenodo_json():
    if ZENODO_JSON.exists():
        with open(ZENODO_JSON) as f:
            return json.load(f)
    return None


def get_zenodo_downloads():
    """Get total downloads across all Zenodo versions.

    A record's plain stats fields (downloads, views, ...) are already the
    all-versions aggregate for the whole concept; the per-version figures are
    the version_* fields. Summing stats.downloads across version records
    therefore multiplies the aggregate by the page size (the 10,800 bug of
    2026-07), so read the aggregate once from the concept record instead.
    """
    record = load_zenodo_json()
    if not record:
        return None

    concept_id = record["concept_record_id"]

    # The concept id redirects to the latest published version
    resp = requests.get(
        f"https://zenodo.org/api/records/{concept_id}",
        timeout=30,
    )
    if resp.status_code != 200:
        return None

    stats = resp.json().get("stats", {})
    return {
        "downloads": stats.get("downloads", 0),
        "unique_downloads": stats.get("unique_downloads", 0),
        "views": stats.get("views", 0),
        "url": record["url"],
    }


def get_hf_downloads():
    """Get cumulative all-time download count from Hugging Face.

    The default `downloads` field is a rolling 30-day count; `downloadsAllTime`
    is the cumulative figure and is only returned when explicitly expanded.
    A token is sent when available: anonymous Hub requests from shared CI IPs
    are the most common cause of the transient 429s that drop this row.
    """
    token = (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACE_TOKEN")
        or os.getenv("HUGGINGFACE_HUB_TOKEN")
    )
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(
        f"https://huggingface.co/api/datasets/{HF_REPO}",
        params={"expand[]": "downloadsAllTime"},
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    downloads = data.get("downloadsAllTime")
    if downloads is None:
        downloads = data.get("downloads", 0)
    return {
        "downloads": downloads,
        "url": f"https://huggingface.co/datasets/{HF_REPO}",
    }


def get_github_downloads():
    """Get total asset download count across all GitHub releases."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{GITHUB_REPO}/releases", "--paginate",
             "--jq", "[.[].assets[].download_count] | add // 0"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None

        # gh --paginate may output multiple lines for paginated results
        total = sum(int(line) for line in result.stdout.strip().split("\n") if line.strip())

        return {
            "downloads": total,
            "url": f"https://github.com/{GITHUB_REPO}/releases",
        }
    except Exception:
        return None


def get_kaggle_downloads():
    """Get download count from Kaggle. Requires KAGGLE_API_TOKEN."""
    token = os.getenv("KAGGLE_API_TOKEN") or os.getenv("KAGGLE_KEY")
    username = os.getenv("KAGGLE_USERNAME")
    if not token or not username:
        return None

    try:
        resp = requests.get(
            f"https://www.kaggle.com/api/v1/datasets/view/{KAGGLE_SLUG}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        return {
            "downloads": data.get("totalDownloads", data.get("downloadCount", 0)),
            "url": f"https://www.kaggle.com/datasets/{KAGGLE_SLUG}",
        }
    except Exception:
        return None


def main():
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)

    print("Collecting download stats")
    print("=" * 40)

    platforms = {}
    total = 0

    for name, fetch_fn in [
        ("zenodo", get_zenodo_downloads),
        ("huggingface", get_hf_downloads),
        ("github", get_github_downloads),
        ("kaggle", get_kaggle_downloads),
    ]:
        try:
            result = fetch_fn()
            if result:
                platforms[name] = result
                total += result.get("downloads", 0)
                print(f"  {name}: {result['downloads']:,} downloads")
            else:
                print(f"  {name}: unavailable")
        except Exception as e:
            print(f"  {name}: error ({e})")

    stats = {
        "total_downloads": total,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "platforms": platforms,
    }

    tmp = str(OUTPUT_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(stats, f, indent=2)
    os.replace(tmp, str(OUTPUT_PATH))

    print(f"\nTotal: {total:,} downloads across {len(platforms)} platforms")
    print(f"Saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
