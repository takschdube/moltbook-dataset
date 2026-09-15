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

import ijson
from tqdm import tqdm

RAW_DIR = Path("data/raw")
DERIVED_DIR = Path("data/derived")


def load_json(path):
    """Load a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def stream_posts_full():
    """Stream posts from posts_full.json using ijson to avoid loading all into memory."""
    path = RAW_DIR / "posts_full.json"
    with open(path, "rb") as f:
        yield from ijson.items(f, "item")


def save_json(data, path):
    """Save data as JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    size = path.stat().st_size / 1024 / 1024
    count = len(data) if isinstance(data, list) else None
    label = f"{count:,} records" if count is not None else "object"
    print(f"  {path.name}: {size:.2f} MB ({label})")


# === BUILDERS ===


def build_agents(post_count):
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

    total_comments = 0

    def process_comments(comments):
        nonlocal total_comments
        for comment in comments:
            total_comments += 1
            agent_id = add_agent(comment.get("author"))
            if agent_id:
                agents[agent_id]["comment_count"] += 1
            if comment.get("replies"):
                process_comments(comment["replies"])

    for post in tqdm(stream_posts_full(), desc="  Scanning", total=post_count):
        agent_id = add_agent(post.get("author"))
        if agent_id:
            agents[agent_id]["post_count"] += 1
        process_comments(post.get("comments", []))

    return list(agents.values()), total_comments


def build_social_graph(post_count):
    """Build post-level social graph: commenter -> post author.

    Counts how many times each agent commented on another agent's posts.
    """
    interactions = defaultdict(lambda: defaultdict(int))

    def process_comments(comments, post_author_name):
        for comment in comments:
            commenter = (comment.get("author") or {}).get("name")
            if commenter and post_author_name and commenter != post_author_name:
                interactions[commenter][post_author_name] += 1
            if comment.get("replies"):
                process_comments(comment["replies"], post_author_name)

    for post in tqdm(stream_posts_full(), desc="  Scanning", total=post_count):
        post_author = (post.get("author") or {}).get("name")
        process_comments(post.get("comments", []), post_author)

    graph = []
    for commenter, targets in interactions.items():
        for target, count in targets.items():
            graph.append({"from": commenter, "to": target, "interactions": count})

    return graph


def build_reply_graph(post_count):
    """Build thread-level reply graph: replier -> parent comment author.

    Uses parent_id on comments to find the actual parent commenter,
    capturing who replied to whom within threads.
    Streams posts_full.json twice (index pass + count pass).
    """
    # First pass: build a lookup of comment_id -> (author_name, author_id)
    comment_authors = {}

    def index_comments(comments):
        for comment in comments:
            author = comment.get("author") or {}
            name = author.get("name")
            if comment.get("id") and name:
                comment_authors[comment["id"]] = (name, comment.get("author_id") or author.get("id"))
            if comment.get("replies"):
                index_comments(comment["replies"])

    for post in tqdm(stream_posts_full(), desc="  Indexing", total=post_count):
        index_comments(post.get("comments", []))

    # Second pass: count reply edges
    interactions = defaultdict(lambda: defaultdict(int))

    def count_replies(comments, post_author):
        for comment in comments:
            author = comment.get("author") or {}
            replier = author.get("name")
            replier_id = comment.get("author_id") or author.get("id")
            parent_id = comment.get("parent_id")
            # Depth-0 comments carry no parent_id; their parent is the post
            # author. Requiring parent_id dropped every reply to a poster.
            parent = comment_authors.get(parent_id) if parent_id else post_author
            if replier and parent and parent[0] and replier != parent[0]:
                interactions[(replier, replier_id)][parent] += 1
            if comment.get("replies"):
                count_replies(comment["replies"], post_author)

    for post in tqdm(stream_posts_full(), desc="  Counting", total=post_count):
        author = post.get("author") or {}
        count_replies(post.get("comments", []),
                      (author.get("name"), post.get("author_id") or author.get("id")))

    graph = []
    for (replier, replier_id), targets in interactions.items():
        for (target, target_id), count in targets.items():
            graph.append({"from": replier, "from_id": replier_id,
                          "to": target, "to_id": target_id, "replies": count})

    return graph


def build_fetch_completeness(post_count):
    """Classify every post by whether its comment layer can be trusted.

    comment_count is the platform's figure and comments is what was retrieved,
    so a nonzero count against an empty array is proof of non-retrieval rather
    than a thread with no replies. Only never_had_comments and complete support
    an absence claim.
    """
    def tree_size(comments):
        return sum(1 + tree_size(c.get("replies")) for c in comments or [])

    counts = defaultdict(int)
    for post in tqdm(stream_posts_full(), desc="  Classifying", total=post_count):
        got = tree_size(post.get("comments"))
        claimed = post.get("comment_count") or 0
        stamped = "comments_fetched_at" in post
        if got == 0:
            cls = "never_had_comments" if claimed == 0 else "not_fetched"
        elif got >= claimed:
            cls = "complete"
        else:
            cls = "partial"
        counts[cls] += 1
        if stamped and post.get("comments_fetched_at") is None:
            counts["marked_not_fetched"] += 1

    total = sum(counts[k] for k in ("never_had_comments", "not_fetched", "complete", "partial"))
    usable = counts["never_had_comments"] + counts["complete"]
    return {
        "total_posts": total,
        "complete": counts["complete"],
        "partial": counts["partial"],
        "never_had_comments": counts["never_had_comments"],
        "not_fetched": counts["not_fetched"],
        "usable_for_absence_claims": usable,
        "usable_fraction": round(usable / total, 4) if total else 0.0,
        "note": (
            "not_fetched means the platform reported comments but none were "
            "retrieved. Those posts cannot support a claim that replies were "
            "absent. partial means fewer comments were retrieved than the "
            "platform reported."
        ),
    }


def build_activity_timeline(posts):
    """Build daily activity timeline from posts.

    Groups posts by date and counts posts + comments per day.
    """
    daily = defaultdict(lambda: {"posts": 0, "comments": 0})

    for post in tqdm(posts, desc="  Scanning"):
        date = post.get("created_at", "")[:10]
        if date:
            daily[date]["posts"] += 1
            daily[date]["comments"] += post.get("comment_count", 0)

    # "comments" is the platform's own count for posts created that day, which
    # is not the same as the number of comments in this archive. See
    # fetch_completeness.json for how much of the comment layer was retrieved.
    timeline = [
        {"date": date, "posts": counts["posts"], "comments_reported_by_platform": counts["comments"],
         "comments": counts["comments"]}
        for date, counts in sorted(daily.items())
    ]

    return timeline


def build_submolt_stats(posts):
    """Build per-submolt statistics from posts.

    Groups posts by submolt and counts posts, comments, and unique authors.
    """
    stats = defaultdict(lambda: {"posts": 0, "comments": 0, "authors": set()})

    for post in tqdm(posts, desc="  Scanning"):
        submolt = post.get("submolt")
        if submolt:
            # submolt can be a dict with name/display_name or a string
            submolt_name = submolt.get("name") if isinstance(submolt, dict) else submolt
            if submolt_name:
                stats[submolt_name]["posts"] += 1
                stats[submolt_name]["comments"] += post.get("comment_count", 0)
                author = (post.get("author") or {}).get("name")
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

    # Count posts_full without loading into memory
    post_count = 0
    if posts_full_path.exists():
        with open(posts_full_path, "rb") as f:
            for _ in tqdm(ijson.items(f, "item"), desc="  Counting posts_full", unit=" posts"):
                post_count += 1

    print(f"Loaded {len(posts):,} posts, {post_count:,} posts with comments")
    print()

    # Build derived datasets one at a time, streaming posts_full.json per builder
    counts = {}

    print("Agents:")
    agents, total_comments = build_agents(post_count)
    save_json(agents, DERIVED_DIR / "agents.json")
    counts["agents"] = len(agents)
    counts["comments_collected"] = total_comments
    del agents

    print("Social graph (post-level):")
    social_graph = build_social_graph(post_count)
    save_json(social_graph, DERIVED_DIR / "social_graph.json")
    counts["social_edges"] = len(social_graph)
    del social_graph

    print("Reply graph (thread-level):")
    reply_graph = build_reply_graph(post_count)
    save_json(reply_graph, DERIVED_DIR / "reply_graph.json")
    counts["reply_edges"] = len(reply_graph)
    del reply_graph

    print("Activity timeline:")
    timeline = build_activity_timeline(posts)
    save_json(timeline, DERIVED_DIR / "activity_timeline.json")
    counts["timeline_days"] = len(timeline)
    del timeline

    print("Fetch completeness:")
    completeness = build_fetch_completeness(post_count)
    save_json(completeness, DERIVED_DIR / "fetch_completeness.json")
    counts["usable_for_absence_claims"] = completeness["usable_for_absence_claims"]
    print(f"  {completeness['usable_fraction']:.1%} of posts have a trustworthy comment layer")

    print("Submolt stats:")
    submolt_stats = build_submolt_stats(posts)
    save_json(submolt_stats, DERIVED_DIR / "submolt_stats.json")
    counts["submolts"] = len(submolt_stats)
    del submolt_stats

    # Save summary for update_readme.py to read
    summary = {
        "posts": len(posts),
        "posts_full": post_count,
        "comments_collected": counts["comments_collected"],
        "agents": counts["agents"],
        "social_edges": counts["social_edges"],
        "reply_edges": counts["reply_edges"],
        "timeline_days": counts["timeline_days"],
        "submolts_active": counts["submolts"],
    }
    save_json(summary, DERIVED_DIR / "build_summary.json")

    print()
    print("=" * 50)
    print("DERIVED BUILD COMPLETE")
    print("=" * 50)
    print(f"  Agents:          {counts['agents']:,}")
    print(f"  Comments:        {counts['comments_collected']:,}")
    print(f"  Social edges:    {counts['social_edges']:,}")
    print(f"  Reply edges:     {counts['reply_edges']:,}")
    print(f"  Timeline days:   {counts['timeline_days']:,}")
    print(f"  Submolts:        {counts['submolts']:,}")


if __name__ == "__main__":
    main()
