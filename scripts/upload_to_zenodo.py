#!/usr/bin/env python3
"""
Upload dataset to Zenodo — safe, no-draft, single-record versioning.

Uses ZENODO.json in the repo root to track the deposit. On first run,
creates a new deposit and publishes immediately. On subsequent runs,
creates a new VERSION of the same deposit (same concept DOI).

Never leaves drafts behind. Never creates duplicate deposits.

Usage:
    ZENODO_TOKEN=... uv run python scripts/upload_to_zenodo.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ZENODO_TOKEN = os.getenv("ZENODO_TOKEN")
ZENODO_API = "https://zenodo.org/api"
ZENODO_JSON = Path("ZENODO.json")
RELEASES_DIR = Path("releases")
RAW_DIR = Path("data/raw")
DERIVED_DIR = Path("data/derived")

HF_REPO = "takschdube/moltbook-dataset"
KAGGLE_URL = "https://www.kaggle.com/datasets/takschdube/moltbook-dataset"
GITHUB_REPO = "https://github.com/takschdube/moltbook-dataset"


def api(method, endpoint, **kwargs):
    """Make authenticated Zenodo API request."""
    url = f"{ZENODO_API}{endpoint}" if endpoint.startswith("/") else endpoint
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {ZENODO_TOKEN}"
    resp = getattr(requests, method)(url, headers=headers, **kwargs)
    return resp


def load_zenodo_json():
    """Load ZENODO.json if it exists."""
    if ZENODO_JSON.exists():
        with open(ZENODO_JSON) as f:
            return json.load(f)
    return None


def save_zenodo_json(data):
    """Save ZENODO.json atomically."""
    tmp = str(ZENODO_JSON) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, str(ZENODO_JSON))


def get_or_create_draft():
    """Get an existing draft or create one. Returns (deposition_id, bucket_url, is_new).

    Logic:
    1. If ZENODO.json exists -> create new version of that record
    2. If not -> create brand new deposit
    """
    record = load_zenodo_json()

    if record:
        record_id = record["latest_record_id"]
        print(f"Creating new version of record {record_id}...")

        resp = api("post", f"/deposit/depositions/{record_id}/actions/newversion")
        if resp.status_code != 201:
            print(f"ERROR: Failed to create new version: {resp.status_code}")
            print(resp.text[:500])
            sys.exit(1)

        # The new draft URL is in the response
        draft_url = resp.json()["links"]["latest_draft"]
        draft_resp = api("get", draft_url)
        draft = draft_resp.json()

        # Clear existing files from the draft
        for f in draft.get("files", []):
            api("delete", f"/deposit/depositions/{draft['id']}/files/{f['id']}")

        return draft["id"], draft["links"]["bucket"], False
    else:
        print("First upload — creating new deposit...")
        resp = api("post", "/deposit/depositions", json={})
        if resp.status_code != 201:
            print(f"ERROR: Failed to create deposit: {resp.status_code}")
            print(resp.text[:500])
            sys.exit(1)

        draft = resp.json()
        return draft["id"], draft["links"]["bucket"], True


def set_metadata(deposition_id):
    """Set deposit metadata."""
    today = datetime.now(timezone.utc)
    version = today.strftime("%Y.%m.%d")

    metadata = {
        "title": "Moltbook Social Interactions Dataset",
        "upload_type": "dataset",
        "description": (
            '<p>A longitudinal dataset of social interactions from '
            '<a href="https://www.moltbook.com">Moltbook</a> — an AI-agent social '
            'platform where autonomous "Molties" post, comment, and interact.</p>'
            '<p>Contains posts, comments, agent profiles, social graphs, and activity '
            'timelines collected automatically every 6 hours. Designed for social '
            'network analysis, AI agent behavior research, and temporal community analysis.</p>'
            f'<p>Full documentation: <a href="{GITHUB_REPO}">GitHub</a> | '
            f'<a href="https://huggingface.co/datasets/{HF_REPO}">Hugging Face</a> | '
            f'<a href="{KAGGLE_URL}">Kaggle</a></p>'
        ),
        "creators": [{"name": "Dube, Taksch", "affiliation": "Dube International"}],
        "keywords": [
            "social media", "social network analysis", "moltbook",
            "AI agents", "conversation analysis", "longitudinal dataset"
        ],
        "access_right": "open",
        "license": "cc-by-4.0",
        "version": version,
        "related_identifiers": [
            {"identifier": GITHUB_REPO, "relation": "isSupplementTo", "scheme": "url"},
            {"identifier": f"https://huggingface.co/datasets/{HF_REPO}", "relation": "isAlternateIdentifier", "scheme": "url"},
            {"identifier": KAGGLE_URL, "relation": "isAlternateIdentifier", "scheme": "url"},
        ],
    }

    resp = api("put", f"/deposit/depositions/{deposition_id}", json={"metadata": metadata})
    if resp.status_code != 200:
        print(f"WARNING: Failed to update metadata: {resp.status_code}")
        print(resp.text[:500])
    else:
        print("Metadata updated")


def upload_file(bucket_url, filepath, name):
    """Upload a single file to the deposit bucket."""
    with open(filepath, "rb") as f:
        resp = requests.put(
            f"{bucket_url}/{name}",
            data=f,
            headers={
                "Authorization": f"Bearer {ZENODO_TOKEN}",
                "Content-Type": "application/octet-stream",
            },
        )
    if resp.status_code in (200, 201):
        size_mb = filepath.stat().st_size / 1024 / 1024
        print(f"  Uploaded {name} ({size_mb:.1f} MB)")
        return True
    else:
        print(f"  FAILED {name}: {resp.status_code}")
        return False


def upload_files(bucket_url):
    """Upload the release zip (or individual files if no zip exists)."""
    # Prefer the release zip — single file, includes everything
    zips = sorted(RELEASES_DIR.glob("moltbook-dataset-*.zip")) if RELEASES_DIR.exists() else []
    if zips:
        latest_zip = zips[-1]
        print(f"Uploading {latest_zip.name}...")
        return upload_file(bucket_url, latest_zip, latest_zip.name)

    # Fallback: upload individual files
    print("No release zip found, uploading individual files...")
    ok = True
    for directory, prefix in [(RAW_DIR, "raw"), (DERIVED_DIR, "derived")]:
        if not directory.exists():
            continue
        for filepath in sorted(directory.glob("*.json")):
            name = f"{prefix}_{filepath.name}"
            if not upload_file(bucket_url, filepath, name):
                ok = False
    return ok


def publish(deposition_id):
    """Publish the deposit. Returns the published record."""
    resp = api("post", f"/deposit/depositions/{deposition_id}/actions/publish")
    if resp.status_code != 202:
        print(f"ERROR: Failed to publish: {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)

    return resp.json()


def main():
    if not ZENODO_TOKEN:
        print("ZENODO_TOKEN not set, skipping Zenodo upload")
        return

    print("=" * 60)
    print("Zenodo Upload")
    print("=" * 60)

    # Get or create draft
    deposition_id, bucket_url, is_new = get_or_create_draft()
    print(f"Deposition: {deposition_id} ({'new' if is_new else 'new version'})")

    # Set metadata
    set_metadata(deposition_id)

    # Upload files
    if not upload_files(bucket_url):
        print("WARNING: Some files failed to upload")

    # Publish — never leave a draft behind
    print("Publishing...")
    record = publish(deposition_id)

    doi = record["doi"]
    concept_doi = record["conceptdoi"]
    concept_recid = record["conceptrecid"]
    record_id = record["id"]

    # Save ZENODO.json
    zenodo_data = {
        "doi": doi,
        "concept_doi": concept_doi,
        "concept_record_id": concept_recid,
        "latest_record_id": record_id,
        "url": f"https://doi.org/{concept_doi}",
        "latest_url": f"https://doi.org/{doi}",
        "zenodo_url": f"https://zenodo.org/records/{record_id}",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_zenodo_json(zenodo_data)

    print()
    print("=" * 60)
    print("PUBLISHED")
    print("=" * 60)
    print(f"  DOI (this version): {doi}")
    print(f"  DOI (all versions): {concept_doi}")
    print(f"  URL: https://doi.org/{concept_doi}")
    print(f"  Record: https://zenodo.org/records/{record_id}")
    print(f"  ZENODO.json updated")


if __name__ == "__main__":
    main()
