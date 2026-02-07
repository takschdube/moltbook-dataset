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
DATA_DIR = Path("data")

def create_kaggle_metadata():
    """Create dataset-metadata.json for Kaggle."""
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

    # Add each JSON file as a resource
    for json_file in DATA_DIR.glob("*.json"):
        metadata["resources"].append({
            "path": json_file.name,
            "description": f"Moltbook {json_file.stem} data"
        })

    metadata_path = DATA_DIR / "dataset-metadata.json"
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

    # Create metadata
    create_kaggle_metadata()

    # Create version
    try:
        result = subprocess.run(
            ["kaggle", "datasets", "version", "-p", ".", "-m", "Automated update from GitHub Actions"],
            capture_output=True,
            text=True,
            cwd=DATA_DIR
        )

        if result.returncode == 0:
            print("✓ Dataset uploaded to Kaggle")
            print(f"  View at: https://www.kaggle.com/datasets/{KAGGLE_USERNAME}/{DATASET_SLUG}")
        else:
            # If version fails, try creating new dataset
            print("Creating new dataset...")
            result = subprocess.run(
                ["kaggle", "datasets", "create", "-p", "."],
                capture_output=True,
                text=True,
                cwd=DATA_DIR
            )
            if result.returncode == 0:
                print("✓ New dataset created on Kaggle")
            else:
                print(f"✗ Error: {result.stderr}")
    except Exception as e:
        print(f"✗ Failed to upload to Kaggle: {e}")

if __name__ == "__main__":
    main()
