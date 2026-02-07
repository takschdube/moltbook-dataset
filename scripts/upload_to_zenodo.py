#!/usr/bin/env python3
"""Upload dataset to Zenodo for permanent DOI."""

import os
import json
import requests
from pathlib import Path
from datetime import datetime

# Configuration
ZENODO_TOKEN = os.getenv("ZENODO_TOKEN")
ZENODO_DEPOSITION_ID = os.getenv("ZENODO_DEPOSITION_ID")  # For updates to existing deposit
ZENODO_API = "https://zenodo.org/api"
DATA_DIR = Path("data")

def create_deposition():
    """Create a new Zenodo deposition (draft)."""
    headers = {"Content-Type": "application/json"}
    params = {"access_token": ZENODO_TOKEN}

    response = requests.post(
        f"{ZENODO_API}/deposit/depositions",
        params=params,
        json={},
        headers=headers
    )

    if response.status_code == 201:
        return response.json()
    else:
        print(f"✗ Failed to create deposition: {response.status_code}")
        print(response.text)
        return None

def update_metadata(deposition_id, metadata):
    """Update deposition metadata."""
    headers = {"Content-Type": "application/json"}
    params = {"access_token": ZENODO_TOKEN}

    data = {"metadata": metadata}

    response = requests.put(
        f"{ZENODO_API}/deposit/depositions/{deposition_id}",
        params=params,
        data=json.dumps(data),
        headers=headers
    )

    if response.status_code == 200:
        return response.json()
    else:
        print(f"✗ Failed to update metadata: {response.status_code}")
        print(response.text)
        return None

def upload_file(deposition_id, filepath):
    """Upload a file to the deposition."""
    params = {"access_token": ZENODO_TOKEN}

    bucket_url = f"{ZENODO_API}/deposit/depositions/{deposition_id}/files"

    with open(filepath, "rb") as f:
        response = requests.post(
            bucket_url,
            params=params,
            data={"name": filepath.name},
            files={"file": f}
        )

    if response.status_code == 201:
        return response.json()
    else:
        print(f"✗ Failed to upload {filepath.name}: {response.status_code}")
        return None

def publish_deposition(deposition_id):
    """Publish the deposition (makes it public and assigns DOI)."""
    params = {"access_token": ZENODO_TOKEN}

    response = requests.post(
        f"{ZENODO_API}/deposit/depositions/{deposition_id}/actions/publish",
        params=params
    )

    if response.status_code == 202:
        return response.json()
    else:
        print(f"✗ Failed to publish: {response.status_code}")
        print(response.text)
        return None

def create_new_version(deposition_id):
    """Create a new version of an existing deposition."""
    params = {"access_token": ZENODO_TOKEN}

    response = requests.post(
        f"{ZENODO_API}/deposit/depositions/{deposition_id}/actions/newversion",
        params=params
    )

    if response.status_code == 201:
        data = response.json()
        # Extract the new draft deposition ID from the latest_draft link
        latest_draft_url = data["links"]["latest_draft"]
        new_id = latest_draft_url.split("/")[-1]
        return new_id
    else:
        print(f"✗ Failed to create new version: {response.status_code}")
        print(response.text)
        return None

def main():
    if not ZENODO_TOKEN:
        print("ZENODO_TOKEN not set, skipping Zenodo upload")
        return

    print("=" * 60)
    print("Uploading to Zenodo")
    print("=" * 60)

    # Load metadata for description
    metadata_file = DATA_DIR / "metadata.json"
    platform_stats = DATA_DIR / "platform_stats.json"

    dataset_metadata = {}
    if metadata_file.exists():
        with open(metadata_file) as f:
            dataset_metadata = json.load(f)

    stats = {}
    if platform_stats.exists():
        with open(platform_stats) as f:
            stats = json.load(f)

    # Create or update deposition
    if ZENODO_DEPOSITION_ID:
        print(f"Creating new version of deposition {ZENODO_DEPOSITION_ID}...")
        deposition_id = create_new_version(ZENODO_DEPOSITION_ID)
        if not deposition_id:
            return
    else:
        print("Creating new deposition...")
        deposition = create_deposition()
        if not deposition:
            return
        deposition_id = deposition["id"]

    print(f"✓ Deposition ID: {deposition_id}")

    # Prepare metadata
    today = datetime.utcnow()
    version = today.strftime("%Y.%m.%d")

    description = f"""<h2>Moltbook Social Interactions Dataset</h2>
<p>A longitudinal dataset of posts, comments, and social interactions from Moltbook,
designed for social media and AI agent research.</p>

<h3>Dataset Statistics</h3>
<ul>
<li><strong>Version</strong>: {version}</li>
<li><strong>Collection Date</strong>: {today.strftime('%Y-%m-%d')}</li>
<li><strong>Total Posts</strong>: {stats.get('total_posts', 'N/A')}</li>
<li><strong>Total Comments</strong>: {stats.get('total_comments', 'N/A')}</li>
</ul>

<h3>Files Included</h3>
<ul>
<li><code>submolts.json</code> - All submolts/communities</li>
<li><code>posts.json</code> - All posts (lightweight, no comments)</li>
<li><code>posts_full.json</code> - Posts with full comment trees</li>
<li><code>agents.json</code> - Agent profiles with activity statistics</li>
<li><code>social_graph.json</code> - Interaction network edges</li>
<li><code>platform_stats.json</code> - Platform-wide statistics</li>
<li><code>metadata.json</code> - Collection metadata and provenance</li>
</ul>

<h3>Usage</h3>
<p>See the GitHub repository for full documentation:
<a href="https://github.com/takschdube/moltbook-dataset">takschdube/moltbook-dataset</a></p>

<h3>License</h3>
<p>This dataset is released under CC BY 4.0. Please cite this dataset in your research.</p>
"""

    zenodo_metadata = {
        "title": f"Moltbook Social Interactions Dataset (v{version})",
        "upload_type": "dataset",
        "description": description,
        "creators": [
            {
                "name": "Dube, Taksch",
                "affiliation": "Independent Researcher"  # Update as needed
            }
        ],
        "keywords": [
            "social media",
            "social network analysis",
            "moltbook",
            "AI agents",
            "conversation analysis",
            "longitudinal dataset"
        ],
        "access_right": "open",
        "license": "cc-by-4.0",
        "version": version,
        "related_identifiers": [
            {
                "identifier": "https://github.com/takschdube/moltbook-dataset",
                "relation": "isSupplementTo",
                "scheme": "url"
            }
        ]
    }

    print("\nUpdating metadata...")
    result = update_metadata(deposition_id, zenodo_metadata)
    if not result:
        return
    print("✓ Metadata updated")

    # Upload files
    files_to_upload = [
        "submolts.json",
        "posts.json",
        "posts_full.json",
        "agents.json",
        "social_graph.json",
        "platform_stats.json",
        "metadata.json"
    ]

    print("\nUploading files...")
    for filename in files_to_upload:
        filepath = DATA_DIR / filename
        if filepath.exists():
            print(f"  Uploading {filename}...")
            result = upload_file(deposition_id, filepath)
            if result:
                size_mb = filepath.stat().st_size / 1024 / 1024
                print(f"  ✓ Uploaded {filename} ({size_mb:.2f} MB)")
        else:
            print(f"  ⚠ Skipping {filename} (not found)")

    # For automated uploads, you might want to auto-publish
    # For manual control, comment out the publish step
    AUTO_PUBLISH = os.getenv("ZENODO_AUTO_PUBLISH", "false").lower() == "true"

    if AUTO_PUBLISH:
        print("\nPublishing deposition...")
        published = publish_deposition(deposition_id)
        if published:
            doi = published["doi"]
            doi_url = f"https://doi.org/{doi}"
            print(f"\n{'=' * 60}")
            print("✓ DATASET PUBLISHED!")
            print(f"{'=' * 60}")
            print(f"DOI: {doi}")
            print(f"URL: {doi_url}")
            print(f"Zenodo: https://zenodo.org/record/{deposition_id}")

            # Save DOI to a file for reference
            doi_file = Path("ZENODO_DOI.txt")
            with open(doi_file, "w") as f:
                f.write(f"DOI: {doi}\n")
                f.write(f"URL: {doi_url}\n")
                f.write(f"Zenodo Record: https://zenodo.org/record/{deposition_id}\n")
                f.write(f"Version: {version}\n")
                f.write(f"Published: {today.isoformat()}\n")
            print(f"\nDOI saved to {doi_file}")
    else:
        print("\n" + "=" * 60)
        print("✓ DRAFT CREATED (not published)")
        print("=" * 60)
        print(f"Review at: https://zenodo.org/deposit/{deposition_id}")
        print("\nTo publish:")
        print("1. Go to the URL above")
        print("2. Review the files and metadata")
        print("3. Click 'Publish' button")
        print("\nOr set ZENODO_AUTO_PUBLISH=true to auto-publish")

if __name__ == "__main__":
    main()
