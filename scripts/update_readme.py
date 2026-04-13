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
ZENODO_JSON = Path("ZENODO.json")

START_MARKER = "<!-- DATASET_STATS_START -->"
END_MARKER = "<!-- DATASET_STATS_END -->"
COVERAGE_NOTE_START = "<!-- COVERAGE_NOTE_START -->"
COVERAGE_NOTE_END = "<!-- COVERAGE_NOTE_END -->"
CITATION_START = "<!-- CITATION_START -->"
CITATION_END = "<!-- CITATION_END -->"
DOWNLOADS_START = "<!-- DOWNLOADS_START -->"
DOWNLOADS_END = "<!-- DOWNLOADS_END -->"


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
        f"| Submolts (listed) | {format_number(stats.get('submolt_count'))} |",
        f"| Submolts (active) | {format_number(stats.get('submolts_active'))} |",
        "",
        f"*Last updated: {now}*",
        "",
        END_MARKER,
    ]

    return "\n".join(lines)


def _abbreviate_number(n):
    """Format a large number as e.g. '2.58M', '12.1M', '374K'."""
    if n is None:
        return "--"
    if n >= 1_000_000:
        val = n / 1_000_000
        return f"{val:.2f}M" if val < 10 else f"{val:.1f}M"
    if n >= 1_000:
        val = n / 1_000
        return f"{val:.0f}K"
    return str(n)


def build_coverage_note(stats):
    """Build the coverage note with live numbers."""
    total_posts = stats.get("total_posts")
    collected_posts = stats.get("posts_collected")
    total_comments = stats.get("total_comments")
    submolt_count = stats.get("submolt_count")

    if not total_posts or not collected_posts:
        return None

    tp = _abbreviate_number(total_posts)
    tc = _abbreviate_number(total_comments)
    cp = _abbreviate_number(collected_posts)
    sc = format_number(submolt_count) if submolt_count else "--"

    lines = [
        COVERAGE_NOTE_START,
        "",
        f"> **Note on platform totals.** The Moltbook API reports platform-wide "
        f"aggregates ({tp} posts, {tc} comments) that include content not "
        f"accessible through the public API; the API documentation notes this "
        f"explicitly. Our crawler performs exhaustive pagination across all "
        f"{sc} listed submolts using multiple sort orders (new, top, hot, "
        f"rising) with overlap detection, and converges on ~{cp} posts with "
        f"diminishing returns per crawl cycle. The gap between the reported "
        f"platform total and the accessible collection is a property of the "
        f"API, not a sampling limitation. Researchers should treat the collected "
        f"subset as representative of publicly accessible content, not of the "
        f"full platform.",
        "",
        COVERAGE_NOTE_END,
    ]

    return "\n".join(lines)


def build_citation_section():
    """Build citation section with Zenodo DOI."""
    if not ZENODO_JSON.exists():
        return None

    with open(ZENODO_JSON) as f:
        zenodo = json.load(f)

    concept_doi = zenodo.get("concept_doi")
    url = zenodo.get("url")
    year = datetime.now(timezone.utc).year

    if not concept_doi:
        return None

    lines = [
        CITATION_START,
        "",
        "If you use this dataset in your research, please cite:",
        "",
        f"> Dube, T. ({year}). Moltbook Social Interactions Dataset. Zenodo. {url}",
        "",
        "```bibtex",
        f"@dataset{{moltbook_{year},",
        "  author    = {Dube, Taksch},",
        "  title     = {Moltbook Social Interactions Dataset},",
        f"  year      = {{{year}}},",
        "  publisher = {Zenodo},",
        f"  doi       = {{{concept_doi}}},",
        f"  url       = {{{url}}}",
        "}",
        "```",
        "",
        CITATION_END,
    ]

    return "\n".join(lines)


def build_downloads_section():
    """Build downloads table from download_stats.json."""
    stats_path = DERIVED_DIR / "download_stats.json"
    if not stats_path.exists():
        return None

    with open(stats_path) as f:
        stats = json.load(f)

    platforms = stats.get("platforms", {})
    total = stats.get("total_downloads", 0)

    if not platforms:
        return None

    platform_labels = {
        "zenodo": "Zenodo",
        "huggingface": "Hugging Face",
        "github": "GitHub Releases",
        "kaggle": "Kaggle",
    }

    lines = [
        DOWNLOADS_START,
        "",
        "| Platform | Downloads |",
        "|----------|-----------|",
    ]

    for key in ["zenodo", "huggingface", "github", "kaggle"]:
        if key in platforms:
            count = format_number(platforms[key].get("downloads"))
            url = platforms[key].get("url", "")
            label = platform_labels.get(key, key)
            lines.append(f"| [{label}]({url}) | {count} |")

    lines.append(f"| **Total** | **{format_number(total)}** |")
    lines.append("")
    lines.append(DOWNLOADS_END)

    return "\n".join(lines)


def replace_section(readme, start_marker, end_marker, content):
    """Replace content between markers. Returns readme unchanged if markers missing."""
    if start_marker not in readme or end_marker not in readme:
        return readme
    start_idx = readme.index(start_marker)
    end_idx = readme.index(end_marker) + len(end_marker)
    return readme[:start_idx] + content + readme[end_idx:]


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
    readme = replace_section(readme, START_MARKER, END_MARKER, table)

    coverage_note = build_coverage_note(stats)
    if coverage_note:
        readme = replace_section(readme, COVERAGE_NOTE_START, COVERAGE_NOTE_END, coverage_note)

    citation = build_citation_section()
    if citation:
        readme = replace_section(readme, CITATION_START, CITATION_END, citation)

    downloads = build_downloads_section()
    if downloads:
        readme = replace_section(readme, DOWNLOADS_START, DOWNLOADS_END, downloads)

    README_PATH.write_text(readme, encoding="utf-8")

    print("README.md updated with latest stats")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    if citation:
        print("  Citation section updated")
    if downloads:
        print("  Downloads section updated")


if __name__ == "__main__":
    main()
