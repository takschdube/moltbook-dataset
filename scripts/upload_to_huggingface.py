#!/usr/bin/env python3
"""Upload dataset to Hugging Face Hub."""

import os
import json
from pathlib import Path
from huggingface_hub import HfApi, create_repo

# Configuration
HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO = os.getenv("HF_REPO", "takschdube/moltbook-dataset")
DATA_DIR = Path("data")

def main():
    if not HF_TOKEN:
        print("HF_TOKEN not set, skipping Hugging Face upload")
        return

    print(f"Uploading to Hugging Face: {HF_REPO}")

    api = HfApi()

    # Create repo if it doesn't exist
    try:
        create_repo(
            repo_id=HF_REPO,
            token=HF_TOKEN,
            repo_type="dataset",
            exist_ok=True,
            private=False
        )
        print(f"✓ Repository {HF_REPO} ready")
    except Exception as e:
        print(f"Error creating repo: {e}")
        return

    # Upload all data files
    files_to_upload = [
        "submolts.json",
        "posts.json",
        "posts_full.json",
        "agents.json",
        "social_graph.json",
        "platform_stats.json",
        "metadata.json"
    ]

    for filename in files_to_upload:
        filepath = DATA_DIR / filename
        if filepath.exists():
            try:
                api.upload_file(
                    path_or_fileobj=str(filepath),
                    path_in_repo=filename,
                    repo_id=HF_REPO,
                    repo_type="dataset",
                    token=HF_TOKEN
                )
                print(f"✓ Uploaded {filename}")
            except Exception as e:
                print(f"✗ Failed to upload {filename}: {e}")

    # Upload README
    readme_path = Path("README.md")
    if readme_path.exists():
        try:
            api.upload_file(
                path_or_fileobj=str(readme_path),
                path_in_repo="README.md",
                repo_id=HF_REPO,
                repo_type="dataset",
                token=HF_TOKEN
            )
            print(f"✓ Uploaded README.md")
        except Exception as e:
            print(f"✗ Failed to upload README: {e}")

    print(f"\n✓ Dataset uploaded to: https://huggingface.co/datasets/{HF_REPO}")

if __name__ == "__main__":
    main()
