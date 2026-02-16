#!/usr/bin/env python3
"""Upload dataset to Kaggle."""

import os
import json
import subprocess
from pathlib import Path

# Configuration
KAGGLE_USERNAME = os.getenv("KAGGLE_USERNAME")
KAGGLE_KEY = os.getenv("KAGGLE_KEY")
DATASET_SLUG = "moltbook-dataset"
RAW_DIR = Path("data/raw")
DERIVED_DIR = Path("data/derived")
STAGING_DIR = Path("data/kaggle_staging")


def create_kaggle_metadata():
    """Create dataset-metadata.json for Kaggle.

    Kaggle expects a flat directory with metadata. We copy files from
    raw/ and derived/ into a staging directory and generate metadata there.
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    metadata = {
        "title": "Moltbook Social Interactions Dataset",
        "id": f"{KAGGLE_USERNAME}/{DATASET_SLUG}",
        "licenses": [{"name": "CC-BY-4.0"}],
        "keywords": [
            "social-media",
            "social-network-analysis",
            "nlp",
            "ai-agents",
            "conversation-analysis"
        ],
        "description": "A longitudinal dataset of posts, comments, and social interactions from Moltbook, designed for social media and AI agent research.",
        "resources": []
    }

    # Copy files from both directories into staging
    import shutil
    for directory, prefix in [(RAW_DIR, "raw"), (DERIVED_DIR, "derived")]:
        if not directory.exists():
            continue
        for json_file in sorted(directory.glob("*.json")):
            # Prefix filename so raw/derived are distinguishable
            staged_name = f"{prefix}_{json_file.name}"
            shutil.copy2(json_file, STAGING_DIR / staged_name)
            metadata["resources"].append({
                "path": staged_name,
                "description": f"Moltbook {prefix}/{json_file.stem} data"
            })

    metadata_path = STAGING_DIR / "dataset-metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    return metadata


def main():
    if not KAGGLE_USERNAME or not KAGGLE_KEY:
        print("Kaggle credentials not set, skipping upload")
        return

    print(f"Uploading to Kaggle: {KAGGLE_USERNAME}/{DATASET_SLUG}")

    # Setup Kaggle credentials
    kaggle_dir = Path.home() / ".kaggle"
    kaggle_dir.mkdir(exist_ok=True)

    kaggle_json = kaggle_dir / "kaggle.json"
    with open(kaggle_json, "w") as f:
        json.dump({
            "username": KAGGLE_USERNAME,
            "key": KAGGLE_KEY
        }, f)
    kaggle_json.chmod(0o600)

    # Create metadata and stage files
    create_kaggle_metadata()

    # Create version
    try:
        result = subprocess.run(
            ["kaggle", "datasets", "version", "-p", ".", "-m", "Automated update from GitHub Actions", "--dir-mode", "zip"],
            capture_output=True,
            text=True,
            cwd=STAGING_DIR
        )

        if result.returncode == 0:
            print("Dataset uploaded to Kaggle")
            print(f"  View at: https://www.kaggle.com/datasets/{KAGGLE_USERNAME}/{DATASET_SLUG}")
        else:
            # If version fails, try creating new dataset
            print(f"Version update failed: {result.stdout.strip()} {result.stderr.strip()}")
            print("Creating new dataset...")
            result = subprocess.run(
                ["kaggle", "datasets", "create", "-p", ".", "--dir-mode", "zip"],
                capture_output=True,
                text=True,
                cwd=STAGING_DIR
            )
            if result.returncode == 0:
                print("New dataset created on Kaggle")
                print(f"  View at: https://www.kaggle.com/datasets/{KAGGLE_USERNAME}/{DATASET_SLUG}")
            else:
                print(f"Error (stdout): {result.stdout.strip()}")
                print(f"Error (stderr): {result.stderr.strip()}")
        if result.stdout.strip():
            print(f"  Output: {result.stdout.strip()}")
    except Exception as e:
        print(f"Failed to upload to Kaggle: {e}")

if __name__ == "__main__":
    main()
