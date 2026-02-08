#!/usr/bin/env python3
"""
Moltbook Data Crawler - Incremental Version
Collects posts, comments, submolts from the Moltbook API.
Writes raw API responses to data/raw/. Derived datasets are built separately.

Usage:
    uv sync
    cp .env.example .env  # Add your API key to .env
    uv run python moltbook_crawler.py [--full|--incremental]
"""

import requests
import json
import time
import os
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# === CONFIGURATION ===
API_KEY = os.getenv("MOLTBOOK_API_KEY")
BASE_URL = os.getenv("MOLTBOOK_BASE_URL", "https://www.moltbook.com/api/v1")
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# Rate limiting
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.5"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
COMMENT_WORKERS = int(os.getenv("COMMENT_WORKERS", "10"))

# Directories
RAW_DIR = Path(os.getenv("DATA_DIR", "data")) / "raw"
ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR", "archives"))
LOGS_DIR = Path("logs")

# Create directories
RAW_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# === LOGGING ===
class CrawlLogger:
    def __init__(self):
        self.log_file = LOGS_DIR / f"crawl_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        self.stats = {
            "start_time": datetime.utcnow().isoformat(),
            "requests_made": 0,
            "errors": 0,
            "new_posts": 0,
            "updated_posts": 0
        }

    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {level}: {message}"
        print(log_line)
        with open(self.log_file, 'a') as f:
            f.write(log_line + "\n")

    def save_stats(self):
        self.stats["end_time"] = datetime.utcnow().isoformat()
        stats_file = LOGS_DIR / f"stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(stats_file, 'w') as f:
            json.dump(self.stats, f, indent=2)

logger = CrawlLogger()

# === HELPERS ===

def make_request(endpoint, params=None):
    """Make API request with retry logic."""
    if not API_KEY:
        logger.log("API_KEY not set! Check your .env file", "ERROR")
        return None

    url = f"{BASE_URL}{endpoint}"
    for attempt in range(MAX_RETRIES):
        try:
            logger.stats["requests_made"] += 1
            resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                logger.log("Rate limited. Waiting 60s...", "WARN")
                time.sleep(60)
            else:
                logger.log(f"Error {resp.status_code}: {resp.text[:100]}", "ERROR")
                logger.stats["errors"] += 1
        except Exception as e:
            logger.log(f"Request failed: {e}", "ERROR")
            logger.stats["errors"] += 1
        time.sleep(REQUEST_DELAY * (attempt + 1))
    return None

def load_json(filename):
    """Load JSON file if it exists."""
    filepath = RAW_DIR / filename
    if filepath.exists():
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def save_json(data, filename, archive=False):
    """Save data to JSON file with optional archiving."""
    filepath = RAW_DIR / filename

    # Archive old version if requested and file exists
    if archive and filepath.exists():
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        archive_path = ARCHIVE_DIR / f"{filepath.stem}_{timestamp}.json"
        os.rename(filepath, archive_path)
        logger.log(f"Archived old version to {archive_path}")

    # Save new version
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    size = filepath.stat().st_size / 1024 / 1024  # MB
    logger.log(f"Saved {filepath} ({size:.2f} MB)")

def get_last_crawl_time():
    """Get timestamp of last successful crawl."""
    metadata = load_json("metadata.json")
    if metadata and "last_crawl" in metadata:
        return datetime.fromisoformat(metadata["last_crawl"])
    return None

def save_metadata(crawl_info):
    """Save crawl metadata."""
    metadata = load_json("metadata.json") or {"crawl_history": []}
    metadata["last_crawl"] = datetime.utcnow().isoformat()
    metadata["crawl_history"].append(crawl_info)
    save_json(metadata, "metadata.json")

# === CRAWLERS ===

def fetch_submolts():
    """Fetch all submolts."""
    logger.log("Fetching Submolts")
    resp = make_request("/submolts")
    if resp and resp.get("success"):
        submolts = resp.get("submolts", [])
        logger.log(f"Found {len(submolts)} submolts")
        stats = {
            "total_posts": resp.get("total_posts"),
            "total_comments": resp.get("total_comments"),
            "submolt_count": resp.get("count"),
            "crawled_at": datetime.utcnow().isoformat()
        }
        return submolts, stats
    return [], {}

def fetch_posts_incremental(since=None):
    """Fetch posts since last crawl. Returns (posts, new_ids, updated_ids)."""
    logger.log(f"Fetching Posts (incremental mode, since={since})")
    all_posts = []
    new_ids = set()
    updated_ids = set()
    existing_posts = {p["id"]: p for p in (load_json("posts.json") or [])}
    offset = 0
    limit = 50
    fetched = 0

    while True:
        logger.log(f"Fetching posts {offset} to {offset + limit}...")
        resp = make_request("/posts", {"sort": "new", "limit": limit, "offset": offset})

        if not resp or not resp.get("success"):
            logger.log("Failed to fetch posts", "ERROR")
            break

        posts = resp.get("posts", [])
        if not posts:
            break

        # Check if we've reached posts we already have
        new_posts = []
        for post in posts:
            if post["id"] not in existing_posts:
                new_posts.append(post)
                new_ids.add(post["id"])
                logger.stats["new_posts"] += 1
            elif since:
                # Update existing post if it might have new comments
                created = post["created_at"].replace("Z", "").replace("+00:00", "")
                post_time = datetime.fromisoformat(created.split(".")[0])
                if post_time > since - timedelta(days=7):  # Update recent posts
                    new_posts.append(post)
                    updated_ids.add(post["id"])
                    logger.stats["updated_posts"] += 1

        all_posts.extend(new_posts)
        fetched += len(posts)
        logger.log(f"Got {len(new_posts)} new/updated posts (total: {len(all_posts)})")

        # If no new posts in this batch and we have a since time, we can stop
        if since and len(new_posts) == 0:
            logger.log("Reached posts from last crawl, stopping")
            break

        if not resp.get("has_more"):
            break

        offset = resp.get("next_offset", offset + limit)
        time.sleep(REQUEST_DELAY)

    # Merge with existing posts
    for post in all_posts:
        existing_posts[post["id"]] = post

    # Checkpoint after incremental listing
    if fetched % 500 < limit:
        save_json(list(existing_posts.values()), "posts.json")

    logger.log(f"Total posts in dataset: {len(existing_posts)}")
    return list(existing_posts.values()), new_ids, updated_ids

def fetch_all_posts():
    """Fetch ALL posts (full crawl). Saves progress every 500 posts."""
    logger.log("Fetching ALL Posts (full mode)")
    existing_posts = {p["id"]: p for p in (load_json("posts.json") or [])}
    logger.log(f"Loaded {len(existing_posts)} existing posts")
    offset = 0
    limit = 50
    fetched = 0

    while True:
        logger.log(f"Fetching posts {offset} to {offset + limit}...")
        resp = make_request("/posts", {"sort": "new", "limit": limit, "offset": offset})

        if not resp or not resp.get("success"):
            logger.log("Failed to fetch posts", "ERROR")
            break

        posts = resp.get("posts", [])
        if not posts:
            break

        for post in posts:
            if post["id"] not in existing_posts:
                existing_posts[post["id"]] = post
                logger.stats["new_posts"] += 1
        fetched += len(posts)
        logger.log(f"Got {len(posts)} posts (total fetched: {fetched}, dataset: {len(existing_posts)})")

        # Checkpoint every 500 posts
        if fetched % 500 < limit:
            save_json(list(existing_posts.values()), "posts.json")

        if not resp.get("has_more"):
            break

        offset = resp.get("next_offset", offset + limit)
        time.sleep(REQUEST_DELAY)

    logger.log(f"Total posts in dataset: {len(existing_posts)}")
    return list(existing_posts.values())

def fetch_post_with_comments(post_id):
    """Fetch single post with full comment tree."""
    resp = make_request(f"/posts/{post_id}")
    if resp and resp.get("success"):
        return resp.get("post"), resp.get("comments", [])
    return None, []

def fetch_all_comments(posts, post_ids_to_update=None):
    """Fetch comments for posts using parallel requests. Saves every 100 posts."""
    logger.log("Fetching Comments")

    # Load existing full posts
    existing_full = {p["id"]: p for p in (load_json("posts_full.json") or [])}
    logger.log(f"Loaded {len(existing_full)} existing posts with comments")

    # Determine which posts need comment updates
    if post_ids_to_update is None:
        # Full mode: skip posts we already have comments for
        posts_to_fetch = [p for p in posts if p["id"] not in existing_full]
    else:
        posts_to_fetch = [p for p in posts if p["id"] in post_ids_to_update]

    total = len(posts_to_fetch)
    logger.log(f"Fetching comments for {total} posts ({COMMENT_WORKERS} parallel workers)")

    if total == 0:
        logger.log("No new posts to fetch comments for")
    else:
        completed = 0
        with ThreadPoolExecutor(max_workers=COMMENT_WORKERS) as executor:
            futures = {
                executor.submit(fetch_post_with_comments, post["id"]): post
                for post in posts_to_fetch
            }

            for future in as_completed(futures):
                post = futures[future]
                full_post, comments = future.result()
                if full_post:
                    full_post["comments"] = comments
                    existing_full[post["id"]] = full_post

                completed += 1

                # Checkpoint every 100 posts
                if completed % 100 == 0:
                    save_json(list(existing_full.values()), "posts_full.json")
                    logger.log(f"[{completed}/{total}] Checkpoint saved ({len(existing_full)} total posts)")

    # Include posts without comments as empty
    for post in posts:
        if post["id"] not in existing_full:
            post["comments"] = []
            existing_full[post["id"]] = post

    return list(existing_full.values())

# === MAIN ===

def crawl(mode="incremental"):
    """Run crawler in specified mode."""
    logger.log("=" * 50)
    logger.log(f"MOLTBOOK DATA CRAWLER - {mode.upper()} MODE")
    logger.log(f"Started at: {datetime.utcnow().isoformat()}")
    logger.log("=" * 50)

    # Fetch submolts (always full, single request)
    submolts, platform_stats = fetch_submolts()
    save_json(submolts, "submolts.json")
    save_json(platform_stats, "platform_stats.json")

    if mode == "full":
        # Start comment fetching for already-listed posts in the background
        # while we continue pulling new post listings in the main thread.
        # Each phase writes to a different file (posts.json vs posts_full.json)
        # so there are no file conflicts.
        existing_posts = load_json("posts.json") or []
        comment_thread = None
        if existing_posts:
            logger.log(f"Starting background comment fetch for {len(existing_posts)} already-listed posts")
            comment_thread = Thread(
                target=fetch_all_comments,
                args=(existing_posts, None),
                daemon=True,
            )
            comment_thread.start()

        # Fetch post listings (main thread, checkpoints every 500)
        posts = fetch_all_posts()
        save_json(posts, "posts.json", archive=True)

        # Wait for background comment fetch to finish
        if comment_thread:
            logger.log("Waiting for background comment fetch to complete...")
            comment_thread.join()
            logger.log("Background comment fetch done")

        # Second pass: fetch comments for any newly discovered posts.
        # fetch_all_comments loads posts_full.json from disk and skips
        # posts that were already fetched by the background thread.
        posts_full = fetch_all_comments(posts, None)
        save_json(posts_full, "posts_full.json", archive=True)

    else:  # incremental
        last_crawl = get_last_crawl_time()
        posts, new_ids, updated_ids = fetch_posts_incremental(since=last_crawl)
        save_json(posts, "posts.json", archive=True)
        post_ids_to_update = new_ids | updated_ids if (new_ids or updated_ids) else None

        posts_full = fetch_all_comments(posts, post_ids_to_update)
        save_json(posts_full, "posts_full.json", archive=True)

    # Save metadata
    crawl_info = {
        "timestamp": datetime.utcnow().isoformat(),
        "mode": mode,
        "stats": {
            "submolts": len(submolts),
            "posts": len(posts),
            "posts_full": len(posts_full),
            "requests": logger.stats["requests_made"],
            "errors": logger.stats["errors"]
        }
    }
    save_metadata(crawl_info)

    logger.log("=" * 50)
    logger.log("CRAWL COMPLETE")
    logger.log("=" * 50)
    logger.log(f"Submolts:     {len(submolts)}")
    logger.log(f"Posts:        {len(posts)}")
    logger.log(f"Posts (full): {len(posts_full)}")
    logger.log(f"Data saved to: {RAW_DIR}/")
    logger.log(f"Finished at:  {datetime.utcnow().isoformat()}")

    logger.save_stats()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Moltbook Data Crawler")
    parser.add_argument("--mode", choices=["full", "incremental"], default="incremental",
                       help="Crawl mode: full (all data) or incremental (only new)")
    parser.add_argument("--full", action="store_true", help="Shorthand for --mode=full")

    args = parser.parse_args()
    mode = "full" if args.full else args.mode

    crawl(mode)
