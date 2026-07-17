#!/usr/bin/env python3
"""Upload dataset to Hugging Face Hub."""

import os
from pathlib import Path
from huggingface_hub import HfApi, create_repo

# Configuration
HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO = os.getenv("HF_REPO", "takschdube/moltbook-dataset")
RAW_DIR = Path("data/raw")
DERIVED_DIR = Path("data/derived")

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
        print(f"Repository {HF_REPO} ready")
    except Exception as e:
        print(f"Error creating repo: {e}")
        return

    # Upload all JSON and CSV files from both directories
    for directory, prefix in [(RAW_DIR, "raw"), (DERIVED_DIR, "derived")]:
        if not directory.exists():
            continue
        for filepath in sorted(list(directory.glob("*.json")) + list(directory.glob("*.csv"))):
            path_in_repo = f"{prefix}/{filepath.name}"
            try:
                api.upload_file(
                    path_or_fileobj=str(filepath),
                    path_in_repo=path_in_repo,
                    repo_id=HF_REPO,
                    repo_type="dataset",
                    token=HF_TOKEN
                )
                print(f"  Uploaded {path_in_repo}")
            except Exception as e:
                print(f"  Failed to upload {path_in_repo}: {e}")

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
            print("  Uploaded README.md")
        except Exception as e:
            print(f"  Failed to upload README: {e}")

    print(f"\nDataset uploaded to: https://huggingface.co/datasets/{HF_REPO}")

if __name__ == "__main__":
    main()
