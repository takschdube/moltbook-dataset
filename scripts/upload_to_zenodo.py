#!/usr/bin/env python3
"""
Upload dataset to Zenodo: single-record versioning.

Uses ZENODO.json in the repo root to track the deposit. On first run,
creates a new deposit and publishes immediately. On subsequent runs,
creates a new VERSION of the same deposit (same concept DOI).

Publishes only a complete version: the release zip and the database, every
part verified. A failed upload leaves its draft for the next run to reuse.
Never creates duplicate deposits.

Usage:
    ZENODO_TOKEN=... uv run python scripts/upload_to_zenodo.py
    uv run python scripts/upload_to_zenodo.py --check  # exit 0 if a version is due
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ZENODO_TOKEN = os.getenv("ZENODO_TOKEN")
ZENODO_API = "https://zenodo.org/api"
ZENODO_JSON = Path("ZENODO.json")
RELEASES_DIR = Path("releases")
RAW_DIR = Path("data/raw")

HF_REPO = "takschdube/moltbook-dataset"
KAGGLE_URL = "https://www.kaggle.com/datasets/takschdube/moltbook-dataset"
GITHUB_REPO = "https://github.com/takschdube/moltbook-dataset"

# Zenodo took 240 MB in 23 seconds in September 2026; in October it took 44
# minutes over a 1.2 GB archive, and most attempts ended in a 502 partway
# through. Parts keep a failed attempt cheap to repeat.
PART_SIZE = 100 * 1024 * 1024
UPLOAD_ATTEMPTS = 5


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


def resolve_latest(record):
    """Resolve the concept's live latest record (id, published_at, file keys).

    ZENODO.json goes stale when the end-of-run commit is dropped (rebase
    conflict against a moved main), so the stored pointer is a fallback,
    not the source of truth. The concept id redirects to the latest
    published version on the public API.
    """
    resp = requests.get(
        f"{ZENODO_API}/records/{record['concept_record_id']}", timeout=30
    )
    if resp.status_code == 200:
        r = resp.json()
        return r["id"], r.get("created"), [f["key"] for f in r.get("files", [])]
    return record["latest_record_id"], record.get("updated_at"), None


def complete(keys):
    """Whether a version holds both the release zip and the database.

    2026-10-15 was published without the database. A version like that does
    not count toward the cadence, so the next run publishes a complete one.
    """
    return (any(k.startswith("moltbook-dataset-") for k in keys)
            and any(k.startswith("moltbook.db.zst") for k in keys))


def due():
    """Whether a new version should be published now.

    Monthly cadence. Each version stores another full copy of a corpus now
    running to several gigabytes, and a deposit has a finite quota, so a
    daily version would exhaust it inside a month to record a few thousand
    new posts. Fine-grained history lives in the Hugging Face revisions;
    Zenodo carries the citable snapshots, and nobody cites a day.
    ZENODO_MIN_HOURS overrides for a deliberate out-of-band version.
    """
    min_hours = int(os.getenv("ZENODO_MIN_HOURS", str(28 * 24)))
    record = load_zenodo_json()
    if not record:
        return True
    _, published_at, keys = resolve_latest(record)
    if keys is not None and not complete(keys):
        print("Latest version lacks the zip or the database; publishing a complete one")
        return True
    if published_at:
        last = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - last
        if age < timedelta(hours=min_hours):
            print(f"Last publish {published_at} ({age.days}d ago); "
                  f"monthly cadence, skipping")
            return False
    return True


def find_existing_draft(concept_recid):
    """Find an unpublished draft of our record left behind by a crashed run."""
    resp = api("get", "/deposit/depositions", params={"status": "draft", "size": 50})
    if resp.status_code != 200:
        return None
    for dep in resp.json():
        if str(dep.get("conceptrecid")) == str(concept_recid):
            full = api("get", f"/deposit/depositions/{dep['id']}")
            if full.status_code == 200:
                return full.json()
    return None


def get_or_create_draft():
    """Get an existing draft or create one. Returns (deposition_id, bucket_url, is_new).

    Logic:
    1. If ZENODO.json exists -> create new version of that record,
       reusing a leftover draft if a previous run crashed mid-publish
    2. If not -> create brand new deposit
    """
    record = load_zenodo_json()

    if record:
        record_id, _, _ = resolve_latest(record)
        print(f"Creating new version of record {record_id}...")

        draft = None
        resp = api("post", f"/deposit/depositions/{record_id}/actions/newversion")
        if resp.status_code == 201:
            draft_resp = api("get", resp.json()["links"]["latest_draft"])
            if draft_resp.status_code == 200:
                draft = draft_resp.json()

        if draft is None:
            # A run that crashes between newversion and publish leaves a draft
            # behind, and Zenodo rejects newversion with 400 while one exists.
            # Reuse the leftover draft instead of failing until someone
            # discards it by hand.
            print(f"newversion not usable ({resp.status_code}), looking for a leftover draft...")
            draft = find_existing_draft(record["concept_record_id"])
            if draft is None:
                print(f"ERROR: Failed to create new version: {resp.status_code}")
                print(resp.text[:500])
                sys.exit(1)
            print(f"Reusing leftover draft {draft['id']}")

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
            f'<p>Files over {PART_SIZE // 2**20} MiB are stored as numbered parts. Rejoin '
            'each before use, for example '
            '<code>cat moltbook.db.zst.part* &gt; moltbook.db.zst</code>.</p>'
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


def put_part(bucket_url, key, data):
    """PUT one object, retrying failures. True once Zenodo holds it intact."""
    md5 = f"md5:{hashlib.md5(data).hexdigest()}"
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            # A file object, not bytes: requests sends bytes in one sendall,
            # and the connect timeout would then cap the whole transfer.
            resp = requests.put(
                f"{bucket_url}/{key}",
                data=io.BytesIO(data),
                headers={
                    "Authorization": f"Bearer {ZENODO_TOKEN}",
                    "Content-Type": "application/octet-stream",
                },
                timeout=(60, 600),
            )
            if resp.status_code in (200, 201) and resp.json().get("checksum", md5) == md5:
                return True
            reason = f"{resp.status_code} {resp.text[:200]}"
        except requests.RequestException as e:
            reason = repr(e)[:200]
        print(f"  {key}: attempt {attempt}/{UPLOAD_ATTEMPTS} failed: {reason}")
        if attempt < UPLOAD_ATTEMPTS:
            time.sleep(30 * attempt)
    return False


def upload_file(bucket_url, filepath, name):
    """Upload a file, as name.part000, name.part001, ... when over PART_SIZE.

    Stops at the first part that will not go up: an incomplete set is never
    published, so uploading the rest would only waste time.
    """
    size = filepath.stat().st_size
    n = max(1, -(-size // PART_SIZE))
    with open(filepath, "rb") as f:
        for i in range(n):
            key = name if n == 1 else f"{name}.part{i:03d}"
            if not put_part(bucket_url, key, f.read(PART_SIZE)):
                print(f"  FAILED {key}")
                return False
    print(f"  Uploaded {name} ({size / 1024 / 1024:.1f} MB in {n} part{'s' * (n > 1)})")
    return True


def upload_files(bucket_url):
    """Upload the release zip plus the cumulative database. Both are required.

    The zip holds the latest-state JSON exports; the database additionally
    carries the full post_metrics_history time series, so archiving both
    keeps the longitudinal record complete on Zenodo.
    """
    zips = sorted(RELEASES_DIR.glob("moltbook-dataset-*.zip")) if RELEASES_DIR.exists() else []
    db_zst = Path("/tmp/moltbook.db.zst")  # downloaded from the Latest release
    db_raw = RAW_DIR / "moltbook.db"
    if not db_zst.exists() and db_raw.exists():
        subprocess.run(
            ["zstd", "-3", "-f", str(db_raw), "-o", str(db_zst)], check=False
        )
    if not zips or not db_zst.exists():
        print(f"ERROR: need a release zip and {db_zst}")
        return False
    print(f"Uploading {zips[-1].name} and {db_zst.name}...")
    return (upload_file(bucket_url, zips[-1], zips[-1].name)
            and upload_file(bucket_url, db_zst, "moltbook.db.zst"))


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

    if not due():
        return

    print("=" * 60)
    print("Zenodo Upload")
    print("=" * 60)

    # Get or create draft
    deposition_id, bucket_url, is_new = get_or_create_draft()
    print(f"Deposition: {deposition_id} ({'new' if is_new else 'new version'})")

    # Set metadata
    set_metadata(deposition_id)

    # A published version cannot be changed (2026-10-15 went out without the
    # database), so a failed upload stops here and the next run reuses the draft.
    if not upload_files(bucket_url):
        print("ERROR: upload incomplete, not publishing")
        sys.exit(1)

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
    if "--check" in sys.argv:
        sys.exit(0 if ZENODO_TOKEN and due() else 1)
    main()
