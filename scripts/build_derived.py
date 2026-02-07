#!/usr/bin/env python3
"""
Build derived datasets from raw Moltbook data.

Reads data/raw/ and writes to data/derived/.
Run after the crawler has populated data/raw/.

Usage:
    uv run python scripts/build_derived.py
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

RAW_DIR = Path("data/raw")
DERIVED_DIR = Path("data/derived")


def load_json(path):
    """Load a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data, path):
    """Save data as JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    size = path.stat().st_size / 1024 / 1024
    count = len(data) if isinstance(data, list) else None
    label = f"{count:,} records" if count is not None else "object"
    print(f"  {path.name}: {size:.2f} MB ({label})")


# === BUILDERS ===


def build_agents(posts_full):
    """Extract unique agents from posts and comments.

    Captures all available fields including description and following_count
    from the detail endpoint's richer author objects.
    """
    agents = {}

    def add_agent(author):
        if not author or not author.get("id"):
            return None
        agent_id = author["id"]
        if agent_id not in agents:
            agents[agent_id] = {
                "id": agent_id,
                "name": author.get("name"),
                "description": author.get("description"),
                "karma": author.get("karma"),
                "follower_count": author.get("follower_count"),
                "following_count": author.get("following_count"),
                "owner": author.get("owner"),
                "post_count": 0,
                "comment_count": 0,
            }
        else:
            # Update with richer data if available (detail endpoint has more fields)
            existing = agents[agent_id]
            if author.get("description") and not existing.get("description"):
                existing["description"] = author["description"]
            if author.get("following_count") and not existing.get("following_count"):
                existing["following_count"] = author["following_count"]
            # Always update to latest karma/follower counts
            if author.get("karma") is not None:
                existing["karma"] = author["karma"]
            if author.get("follower_count") is not None:
                existing["follower_count"] = author["follower_count"]
        return agent_id

    def process_comments(comments):
        for comment in comments:
            agent_id = add_agent(comment.get("author"))
            if agent_id:
                agents[agent_id]["comment_count"] += 1
            if comment.get("replies"):
                process_comments(comment["replies"])

    for post in posts_full:
        agent_id = add_agent(post.get("author"))
        if agent_id:
            agents[agent_id]["post_count"] += 1
        process_comments(post.get("comments", []))

    return list(agents.values())


def build_social_graph(posts_full):
    """Build post-level social graph: commenter -> post author.

    Counts how many times each agent commented on another agent's posts.
    """
    interactions = defaultdict(lambda: defaultdict(int))

    def process_comments(comments, post_author_name):
        for comment in comments:
            commenter = comment.get("author", {}).get("name")
            if commenter and post_author_name and commenter != post_author_name:
                interactions[commenter][post_author_name] += 1
            if comment.get("replies"):
                process_comments(comment["replies"], post_author_name)

    for post in posts_full:
        post_author = post.get("author", {}).get("name")
        process_comments(post.get("comments", []), post_author)

    graph = []
    for commenter, targets in interactions.items():
        for target, count in targets.items():
            graph.append({"from": commenter, "to": target, "interactions": count})

    return graph


def build_reply_graph(posts_full):
    """Build thread-level reply graph: replier -> parent comment author.

    Uses parent_id on comments to find the actual parent commenter,
    capturing who replied to whom within threads.
    """
    # First pass: build a lookup of comment_id -> author_name
    comment_authors = {}

    def index_comments(comments):
        for comment in comments:
            author_name = comment.get("author", {}).get("name")
            if comment.get("id") and author_name:
                comment_authors[comment["id"]] = author_name
            if comment.get("replies"):
                index_comments(comment["replies"])

    for post in posts_full:
        index_comments(post.get("comments", []))

    # Second pass: count reply edges
    interactions = defaultdict(lambda: defaultdict(int))

    def count_replies(comments):
        for comment in comments:
            replier = comment.get("author", {}).get("name")
            parent_id = comment.get("parent_id")
            if replier and parent_id and parent_id in comment_authors:
                parent_author = comment_authors[parent_id]
                if replier != parent_author:
                    interactions[replier][parent_author] += 1
            if comment.get("replies"):
                count_replies(comment["replies"])

    for post in posts_full:
        count_replies(post.get("comments", []))

    graph = []
    for replier, targets in interactions.items():
        for target, count in targets.items():
            graph.append({"from": replier, "to": target, "replies": count})

    return graph


def build_activity_timeline(posts):
    """Build daily activity timeline from posts.

    Groups posts by date and counts posts + comments per day.
    """
    daily = defaultdict(lambda: {"posts": 0, "comments": 0})

    for post in posts:
        date = post.get("created_at", "")[:10]
        if date:
            daily[date]["posts"] += 1
            daily[date]["comments"] += post.get("comment_count", 0)

    timeline = [
        {"date": date, "posts": counts["posts"], "comments": counts["comments"]}
        for date, counts in sorted(daily.items())
    ]

    return timeline


def build_submolt_stats(posts):
    """Build per-submolt statistics from posts.

    Groups posts by submolt and counts posts, comments, and unique authors.
    """
    stats = defaultdict(lambda: {"posts": 0, "comments": 0, "authors": set()})

    for post in posts:
        submolt = post.get("submolt")
        if submolt:
            # submolt can be a dict with name/display_name or a string
            submolt_name = submolt.get("name") if isinstance(submolt, dict) else submolt
            if submolt_name:
                stats[submolt_name]["posts"] += 1
                stats[submolt_name]["comments"] += post.get("comment_count", 0)
                author = post.get("author", {}).get("name")
                if author:
                    stats[submolt_name]["authors"].add(author)

    result = [
        {
            "submolt": submolt,
            "posts": s["posts"],
            "comments": s["comments"],
            "unique_authors": len(s["authors"]),
        }
        for submolt, s in stats.items()
    ]

    # Sort by post count descending
    result.sort(key=lambda x: x["posts"], reverse=True)

    return result


# === MAIN ===


def main():
    if not RAW_DIR.exists():
        print(f"ERROR: {RAW_DIR} not found. Run the crawler first.")
        sys.exit(1)

    DERIVED_DIR.mkdir(parents=True, exist_ok=True)

    print("Building derived datasets")
    print("=" * 50)

    # Load raw data
    posts_path = RAW_DIR / "posts.json"
    posts_full_path = RAW_DIR / "posts_full.json"

    if not posts_path.exists():
        print(f"ERROR: {posts_path} not found.")
        sys.exit(1)

    posts = load_json(posts_path)
    posts_full = load_json(posts_full_path) if posts_full_path.exists() else []

    print(f"Loaded {len(posts):,} posts, {len(posts_full):,} posts with comments")
    print()

    # Build all derived datasets
    print("Agents:")
    agents = build_agents(posts_full)
    save_json(agents, DERIVED_DIR / "agents.json")

    print("Social graph (post-level):")
    social_graph = build_social_graph(posts_full)
    save_json(social_graph, DERIVED_DIR / "social_graph.json")

    print("Reply graph (thread-level):")
    reply_graph = build_reply_graph(posts_full)
    save_json(reply_graph, DERIVED_DIR / "reply_graph.json")

    print("Activity timeline:")
    timeline = build_activity_timeline(posts)
    save_json(timeline, DERIVED_DIR / "activity_timeline.json")

    print("Submolt stats:")
    submolt_stats = build_submolt_stats(posts)
    save_json(submolt_stats, DERIVED_DIR / "submolt_stats.json")

    print()
    print("=" * 50)
    print("DERIVED BUILD COMPLETE")
    print("=" * 50)
    print(f"  Agents:          {len(agents):,}")
    print(f"  Social edges:    {len(social_graph):,}")
    print(f"  Reply edges:     {len(reply_graph):,}")
    print(f"  Timeline days:   {len(timeline):,}")
    print(f"  Submolts:        {len(submolt_stats):,}")


if __name__ == "__main__":
    main()
