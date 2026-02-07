#!/usr/bin/env python3
"""
Package dataset into timestamped zip files with manifests.

Creates:
  releases/moltbook-dataset-YYYY-MM-DD.zip     (full dataset)
  releases/RELEASE_NOTES.md                     (auto-generated notes)
"""

import json
import zipfile
import os
import sys
from pathlib import Path
from datetime import datetime

RAW_DIR = Path("data/raw")
DERIVED_DIR = Path("data/derived")
RELEASES_DIR = Path("releases")
RELEASES_DIR.mkdir(exist_ok=True)

TODAY = datetime.utcnow()
DATE_TAG = TODAY.strftime("%Y-%m-%d")
MONTH_TAG = TODAY.strftime("%Y-%m")


def count_comments(posts_full):
    """Recursively count all comments across posts."""
    total = 0

    def _count(comments):
        nonlocal total
        for c in comments:
            total += 1
            if c.get("replies"):
                _count(c["replies"])

    for post in posts_full:
        _count(post.get("comments", []))
    return total


def load_data():
    """Load all data files from raw/ and derived/ and compute stats."""
    files = {}
    stats = {}

    # Scan both directories
    for directory, prefix in [(RAW_DIR, "raw"), (DERIVED_DIR, "derived")]:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            name = f"{prefix}/{path.name}"
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            files[name] = {
                "path": path,
                "size_bytes": path.stat().st_size,
                "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
            }

            if isinstance(data, list):
                files[name]["count"] = len(data)
            elif isinstance(data, dict):
                files[name]["count"] = None

            # Specific stats
            if path.name == "submolts.json":
                stats["submolts"] = len(data) if isinstance(data, list) else 0
            elif path.name == "posts.json" and prefix == "raw":
                stats["posts"] = len(data)
            elif path.name == "posts_full.json":
                stats["posts_with_comments"] = len(data)
                stats["comments"] = count_comments(data)
            elif path.name == "agents.json":
                stats["agents"] = len(data)
            elif path.name == "social_graph.json":
                stats["social_edges"] = len(data)
            elif path.name == "reply_graph.json":
                stats["reply_edges"] = len(data)
            elif path.name == "activity_timeline.json":
                stats["timeline_days"] = len(data)
            elif path.name == "submolt_stats.json":
                stats["submolt_stats"] = len(data)
            elif path.name == "platform_stats.json":
                stats["platform_total_posts"] = data.get("total_posts")
                stats["platform_total_comments"] = data.get("total_comments")

    return files, stats


def create_manifest(files, stats):
    """Create manifest.json with dataset metadata."""
    manifest = {
        "dataset": "Moltbook Social Interactions Dataset",
        "created_by": "Taksch Dube",
        "date": DATE_TAG,
        "timestamp": TODAY.isoformat() + "Z",
        "license": "CC BY 4.0",
        "stats": stats,
        "files": {
            name: {
                "size_bytes": info["size_bytes"],
                "size_mb": info["size_mb"],
                "records": info["count"],
            }
            for name, info in files.items()
        },
    }
    return manifest


def create_zip(files, manifest, zip_name):
    """Create zip file with all data and manifest."""
    zip_path = RELEASES_DIR / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add manifest
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        # Add data files preserving raw/derived prefixes
        for name, info in files.items():
            zf.write(info["path"], name)

    zip_size = zip_path.stat().st_size
    original_size = sum(f["size_bytes"] for f in files.values())

    return {
        "path": str(zip_path),
        "zip_size_bytes": zip_size,
        "zip_size_mb": round(zip_size / 1024 / 1024, 2),
        "original_size_bytes": original_size,
        "original_size_mb": round(original_size / 1024 / 1024, 2),
        "compression_ratio": round(original_size / max(zip_size, 1), 1),
    }


def generate_release_notes(files, stats, zip_info):
    """Generate markdown release notes."""
    lines = []
    lines.append(f"# Moltbook Dataset - {DATE_TAG}\n")
    lines.append(f"**Crawled**: {TODAY.strftime('%Y-%m-%d %H:%M UTC')}\n")

    lines.append("## Dataset Statistics\n")
    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Submolts | {stats.get('submolts', 0):,} |")
    lines.append(f"| Posts | {stats.get('posts', 0):,} |")
    lines.append(f"| Comments | {stats.get('comments', 0):,} |")
    lines.append(f"| Agents | {stats.get('agents', 0):,} |")
    lines.append(f"| Social Edges | {stats.get('social_edges', 0):,} |")
    lines.append(f"| Reply Edges | {stats.get('reply_edges', 0):,} |")
    lines.append("")

    lines.append("## Files\n")
    lines.append("| File | Records | Size |")
    lines.append("|------|---------|------|")
    for name, info in sorted(files.items()):
        count = f"{info['count']:,}" if info["count"] is not None else "-"
        lines.append(f"| `{name}` | {count} | {info['size_mb']} MB |")
    lines.append("")

    lines.append("## Download\n")
    lines.append("| Archive | Compressed | Original | Ratio |")
    lines.append("|---------|------------|----------|-------|")
    lines.append(
        f"| `{os.path.basename(zip_info['path'])}` "
        f"| {zip_info['zip_size_mb']} MB "
        f"| {zip_info['original_size_mb']} MB "
        f"| {zip_info['compression_ratio']}x |"
    )
    lines.append("")

    lines.append("## Citation\n")
    lines.append("```bibtex")
    lines.append("@dataset{moltbook_dataset,")
    lines.append("  author = {Taksch Dube},")
    lines.append("  title  = {Moltbook Social Interactions Dataset},")
    lines.append(f"  year   = {TODAY.year},")
    lines.append(f"  date   = {{{DATE_TAG}}},")
    lines.append("  url    = {https://github.com/takschdube/moltbook-dataset}")
    lines.append("}")
    lines.append("```\n")

    lines.append("## License\n")
    lines.append("CC BY 4.0 - Attribution required.\n")

    return "\n".join(lines)


def main():
    print(f"Packaging dataset release: {DATE_TAG}")
    print("=" * 50)

    # Load and analyze data
    files, stats = load_data()

    if not files:
        print("ERROR: No data files found in data/raw/ or data/derived/")
        sys.exit(1)

    print(f"Found {len(files)} data files")
    for name, info in files.items():
        count = f"{info['count']:,} records" if info["count"] is not None else "object"
        print(f"  {name}: {info['size_mb']} MB ({count})")

    # Create manifest
    manifest = create_manifest(files, stats)

    # Create full dataset zip
    zip_name = f"moltbook-dataset-{DATE_TAG}.zip"
    print(f"\nCreating {zip_name}...")
    zip_info = create_zip(files, manifest, zip_name)
    print(f"  Compressed: {zip_info['original_size_mb']} MB -> {zip_info['zip_size_mb']} MB ({zip_info['compression_ratio']}x)")

    # Generate release notes
    notes = generate_release_notes(files, stats, zip_info)
    notes_path = RELEASES_DIR / "RELEASE_NOTES.md"
    with open(notes_path, "w") as f:
        f.write(notes)
    print(f"\nRelease notes: {notes_path}")

    # Write summary for GitHub Actions
    summary = {
        "date": DATE_TAG,
        "month": MONTH_TAG,
        "zip_path": str(RELEASES_DIR / zip_name),
        "notes_path": str(notes_path),
        "stats": stats,
        "zip_info": zip_info,
    }
    summary_path = RELEASES_DIR / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'=' * 50}")
    print("PACKAGING COMPLETE")
    print(f"{'=' * 50}")
    print(f"  Posts:        {stats.get('posts', 0):,}")
    print(f"  Comments:     {stats.get('comments', 0):,}")
    print(f"  Agents:       {stats.get('agents', 0):,}")
    print(f"  Archive:      {zip_info['zip_size_mb']} MB")
    print(f"  Release:      {zip_name}")


if __name__ == "__main__":
    main()
