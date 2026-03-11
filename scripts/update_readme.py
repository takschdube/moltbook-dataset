#!/usr/bin/env python3
"""
Auto-update README.md with latest dataset statistics.

Reads stats from data/raw/ and data/derived/ files, then replaces the
content between <!-- DATASET_STATS_START --> and <!-- DATASET_STATS_END -->
markers in README.md.

Usage:
    uv run python scripts/update_readme.py
"""

import json
from pathlib import Path
from datetime import datetime, timezone

RAW_DIR = Path("data/raw")
DERIVED_DIR = Path("data/derived")
README_PATH = Path("README.md")

START_MARKER = "<!-- DATASET_STATS_START -->"
END_MARKER = "<!-- DATASET_STATS_END -->"


def load_json(path):
    """Load a JSON file if it exists."""
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def gather_stats():
    """Gather stats from raw and derived data files."""
    stats = {}

    # Platform stats from raw
    platform = load_json(RAW_DIR / "platform_stats.json")
    if platform:
        stats["total_posts"] = platform.get("total_posts")
        stats["submolt_count"] = platform.get("submolt_count")

    # Post count and API-reported comment total from raw
    posts = load_json(RAW_DIR / "posts.json")
    if posts and isinstance(posts, list):
        stats["posts_collected"] = len(posts)
        stats["total_comments"] = sum(p.get("comment_count", 0) for p in posts)

    # Derived stats from build summary (includes actual comment count)
    build_summary = load_json(DERIVED_DIR / "build_summary.json")
    if build_summary and isinstance(build_summary, dict):
        stats["comments_collected"] = build_summary.get("comments_collected")
        stats["agents"] = build_summary.get("agents")
        stats["social_edges"] = build_summary.get("social_edges")
        stats["reply_edges"] = build_summary.get("reply_edges")
        stats["submolts_active"] = build_summary.get("submolts_active")
    else:
        # Fallback: read individual derived files
        agents = load_json(DERIVED_DIR / "agents.json")
        if agents and isinstance(agents, list):
            stats["agents"] = len(agents)

        social_graph = load_json(DERIVED_DIR / "social_graph.json")
        if social_graph and isinstance(social_graph, list):
            stats["social_edges"] = len(social_graph)

        reply_graph = load_json(DERIVED_DIR / "reply_graph.json")
        if reply_graph and isinstance(reply_graph, list):
            stats["reply_edges"] = len(reply_graph)

        submolt_stats = load_json(DERIVED_DIR / "submolt_stats.json")
        if submolt_stats and isinstance(submolt_stats, list):
            stats["submolts_active"] = len(submolt_stats)

    # Crawl metadata
    metadata = load_json(RAW_DIR / "metadata.json")
    if metadata:
        stats["last_crawl"] = metadata.get("last_crawl")

    return stats


def format_number(n):
    """Format a number with commas, or return dash if None."""
    if n is None:
        return "--"
    return f"{n:,}"


def build_stats_table(stats):
    """Build the markdown stats table."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        START_MARKER,
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Posts (platform total) | {format_number(stats.get('total_posts'))} |",
        f"| Comments (platform total) | {format_number(stats.get('total_comments'))} |",
        f"| Posts (collected) | {format_number(stats.get('posts_collected'))} |",
        f"| Comments (collected) | {format_number(stats.get('comments_collected'))} |",
        f"| Agents | {format_number(stats.get('agents'))} |",
        f"| Social graph edges | {format_number(stats.get('social_edges'))} |",
        f"| Reply graph edges | {format_number(stats.get('reply_edges'))} |",
        f"| Submolts (active) | {format_number(stats.get('submolts_active'))} |",
        "",
        f"*Last updated: {now}*",
        "",
        END_MARKER,
    ]

    return "\n".join(lines)


def main():
    if not README_PATH.exists():
        print(f"ERROR: {README_PATH} not found")
        return

    readme = README_PATH.read_text(encoding="utf-8")

    if START_MARKER not in readme or END_MARKER not in readme:
        print("ERROR: Stats markers not found in README.md")
        print(f"  Expected: {START_MARKER} ... {END_MARKER}")
        return

    stats = gather_stats()
    table = build_stats_table(stats)

    # Replace everything between markers (inclusive)
    start_idx = readme.index(START_MARKER)
    end_idx = readme.index(END_MARKER) + len(END_MARKER)

    new_readme = readme[:start_idx] + table + readme[end_idx:]

    README_PATH.write_text(new_readme, encoding="utf-8")

    print("README.md updated with latest stats")
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
