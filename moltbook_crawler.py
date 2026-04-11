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
import sqlite3
import time
import os
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from tqdm import tqdm
import ijson

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

# Time budget (0 = unlimited)
TIME_BUDGET_MINUTES = 0
_start_time = None

def time_remaining():
    """Return remaining seconds, or float('inf') if no budget set."""
    if TIME_BUDGET_MINUTES <= 0 or _start_time is None:
        return float('inf')
    elapsed = (datetime.now(timezone.utc) - _start_time).total_seconds()
    return (TIME_BUDGET_MINUTES * 60) - elapsed

def has_time(reserve_minutes=10):
    """Check if there's enough time left, keeping a reserve for saving/cleanup."""
    return time_remaining() > reserve_minutes * 60

# Directories
RAW_DIR = Path(os.getenv("DATA_DIR", "data")) / "raw"
DB_PATH = RAW_DIR / "moltbook.db"
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
            "start_time": datetime.now(timezone.utc).isoformat(),
            "requests_made": 0,
            "errors": 0,
            "new_posts": 0,
            "updated_posts": 0
        }

    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {level}: {message}"
        tqdm.write(log_line)
        with open(self.log_file, 'a') as f:
            f.write(log_line + "\n")

    def save_stats(self):
        self.stats["end_time"] = datetime.now(timezone.utc).isoformat()
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
    """Load JSON file if it exists. Returns None for missing or corrupt files."""
    filepath = RAW_DIR / filename
    if filepath.exists():
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.log(f"WARNING: {filepath} is corrupt, ignoring", "WARN")
            return None
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

    # Save new version (atomic: write to .tmp then replace)
    tmp_path = str(filepath) + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, str(filepath))

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
    metadata["last_crawl"] = datetime.now(timezone.utc).isoformat()
    metadata["crawl_history"].append(crawl_info)
    save_json(metadata, "metadata.json")

# === DATABASE ===

def init_db():
    """Initialize SQLite database with required tables."""
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS posts_full (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    # Tracks per-submolt backfill progress so successive runs pick up
    # where the last one left off instead of re-scanning from the top.
    db.execute("""
        CREATE TABLE IF NOT EXISTS submolt_progress (
            name TEXT NOT NULL,
            sort TEXT NOT NULL,
            last_offset INTEGER NOT NULL DEFAULT 0,
            exhausted INTEGER NOT NULL DEFAULT 0,
            last_crawled_at TEXT,
            PRIMARY KEY (name, sort)
        )
    """)
    db.commit()
    return db

def migrate_json_to_db(db):
    """One-time migration: import existing JSON files into SQLite."""
    posts_path = RAW_DIR / "posts.json"
    posts_full_path = RAW_DIR / "posts_full.json"

    # Import posts.json if DB posts table is empty
    if posts_path.exists() and db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0:
        logger.log("Migrating posts.json to database...")
        with open(posts_path, "r", encoding="utf-8") as f:
            posts = json.load(f)
        for post in tqdm(posts, desc="[Importing posts]", unit=" posts"):
            db.execute(
                "INSERT OR IGNORE INTO posts (id, data) VALUES (?, ?)",
                (post["id"], json.dumps(post, ensure_ascii=False)),
            )
        db.commit()
        logger.log(f"Imported {len(posts)} posts")
        del posts

    # Import posts_full.json streaming with ijson (constant memory)
    if posts_full_path.exists() and db.execute("SELECT COUNT(*) FROM posts_full").fetchone()[0] == 0:
        logger.log("Migrating posts_full.json to database (streaming)...")
        imported = 0
        with open(posts_full_path, "rb") as f:
            for item in tqdm(ijson.items(f, "item"), desc="[Importing posts_full]", unit=" posts"):
                db.execute(
                    "INSERT OR IGNORE INTO posts_full (id, data) VALUES (?, ?)",
                    (item["id"], json.dumps(item, ensure_ascii=False)),
                )
                imported += 1
                if imported % 5000 == 0:
                    db.commit()
        db.commit()
        logger.log(f"Imported {imported} full posts")

def export_posts_json(db):
    """Stream posts table to data/raw/posts.json (constant memory)."""
    filepath = RAW_DIR / "posts.json"
    tmp_path = str(filepath) + ".tmp"
    count = 0
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("[\n")
        first = True
        for (data_str,) in db.execute("SELECT data FROM posts"):
            if not first:
                f.write(",\n")
            f.write(data_str)
            first = False
            count += 1
        f.write("\n]")
    os.replace(tmp_path, str(filepath))
    size = filepath.stat().st_size / 1024 / 1024
    logger.log(f"Exported {filepath} ({size:.2f} MB, {count} posts)")
    return count

def export_posts_full_json(db):
    """Stream posts_full table to data/raw/posts_full.json (constant memory)."""
    filepath = RAW_DIR / "posts_full.json"
    tmp_path = str(filepath) + ".tmp"
    count = 0
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("[\n")
        first = True
        for (data_str,) in db.execute("SELECT data FROM posts_full"):
            if not first:
                f.write(",\n")
            f.write(data_str)
            first = False
            count += 1
        f.write("\n]")
    os.replace(tmp_path, str(filepath))
    size = filepath.stat().st_size / 1024 / 1024
    logger.log(f"Exported {filepath} ({size:.2f} MB, {count} posts)")
    return count

def ensure_all_posts_in_full(db):
    """Add empty-comments entries in posts_full for any post not yet there."""
    missing = db.execute("""
        SELECT p.id, p.data FROM posts p
        LEFT JOIN posts_full pf ON p.id = pf.id
        WHERE pf.id IS NULL
    """).fetchall()
    for post_id, data_str in missing:
        post = json.loads(data_str)
        post["comments"] = []
        db.execute(
            "INSERT INTO posts_full (id, data) VALUES (?, ?)",
            (post_id, json.dumps(post, ensure_ascii=False)),
        )
    if missing:
        db.commit()
        logger.log(f"Added {len(missing)} posts with empty comments to posts_full")

# === CRAWLERS ===

def fetch_submolts():
    """Fetch all submolts with pagination.

    The /submolts endpoint returns {success, submolts[], total_posts, total_comments, count}
    where count is the total number of submolts. It does NOT return has_more/next_offset,
    so we paginate using count to know when we've fetched everything.
    """
    logger.log("Fetching Submolts")
    all_submolts = []
    stats = {}
    offset = 0
    limit = 50
    total_count = None

    while True:
        resp = make_request("/submolts", {"limit": limit, "offset": offset})
        if not resp or not resp.get("success"):
            break

        submolts = resp.get("submolts", [])
        if not submolts:
            break

        all_submolts.extend(submolts)

        # Capture platform stats and total count from first response
        if offset == 0:
            total_count = resp.get("count")
            stats = {
                "total_posts": resp.get("total_posts"),
                "total_comments": resp.get("total_comments"),
                "crawled_at": datetime.now(timezone.utc).isoformat()
            }

        # Stop when we've fetched all submolts (use count if available, else empty page)
        if total_count is not None and len(all_submolts) >= total_count:
            break

        # Fallback: if fewer than limit returned, we've hit the last page
        if len(submolts) < limit:
            break

        offset += limit
        time.sleep(REQUEST_DELAY)

    stats["submolt_count"] = len(all_submolts)
    logger.log(f"Found {len(all_submolts)} submolts (API reports {total_count} total)")
    return all_submolts, stats

def fetch_posts_incremental(db, since=None):
    """Fetch posts since last crawl. Returns (new_ids, updated_ids)."""
    existing_ids = {row[0] for row in db.execute("SELECT id FROM posts")}

    since_str = since.strftime("%Y-%m-%d %H:%M UTC") if since else "beginning"
    logger.log(f"Fetching Posts (incremental, since {since_str}, {len(existing_ids)} existing)")

    new_ids = set()
    updated_ids = set()
    offset = 0
    limit = 50
    fetched = 0
    consecutive_known_batches = 0

    pbar = tqdm(desc="[Posts]", unit=" posts", dynamic_ncols=True)

    while True:
        if not has_time(reserve_minutes=15):
            logger.log("Time budget reached during post listing, stopping")
            break

        resp = make_request("/posts", {"sort": "new", "limit": limit, "offset": offset})

        if not resp or not resp.get("success"):
            logger.log("Failed to fetch posts", "ERROR")
            break

        posts = resp.get("posts", [])
        if not posts:
            break

        new_in_batch = 0
        for post in posts:
            if post["id"] not in existing_ids:
                new_ids.add(post["id"])
                existing_ids.add(post["id"])
                logger.stats["new_posts"] += 1
                new_in_batch += 1
            else:
                # Track posts that may have new comments since last crawl
                if since:
                    post_time = datetime.fromisoformat(post["created_at"])
                    if post_time > since - timedelta(days=1):
                        updated_ids.add(post["id"])
                        logger.stats["updated_posts"] += 1

            db.execute(
                "INSERT OR REPLACE INTO posts (id, data) VALUES (?, ?)",
                (post["id"], json.dumps(post, ensure_ascii=False)),
            )

        fetched += len(posts)
        pbar.update(len(posts))
        pbar.set_postfix(new=len(new_ids), updated=len(updated_ids), total=len(existing_ids))

        # Stop after consecutive batches with no genuinely NEW posts
        if new_in_batch == 0:
            consecutive_known_batches += 1
            if consecutive_known_batches >= 5:
                logger.log("5 consecutive batches with no new posts, stopping")
                break
        else:
            consecutive_known_batches = 0

        # Checkpoint every 1000 posts fetched
        if fetched % 1000 < limit:
            db.commit()

        if not resp.get("has_more"):
            break

        offset = resp.get("next_offset", offset + limit)
        time.sleep(REQUEST_DELAY)

    pbar.close()
    db.commit()

    total = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    logger.log(f"Incremental: {len(new_ids)} new + {len(updated_ids)} updated, {total} total")
    return new_ids, updated_ids

def fetch_hot_posts(existing_post_ids):
    """Fetch hot and rising posts to catch actively discussed older posts.
    Returns (hot_ids, new_posts) where hot_ids are IDs needing comment refresh
    and new_posts are posts not yet in the dataset."""
    hot_ids = set()
    new_posts = []
    max_pages = int(os.getenv("HOT_PAGES", "20"))

    for sort_order in ["hot", "rising", "top"]:
        if not has_time(reserve_minutes=15):
            logger.log("Time budget reached, skipping remaining hot/rising scan")
            break

        offset = 0
        pages = 0
        pbar = tqdm(desc=f"[{sort_order.title()}]", unit=" posts", dynamic_ncols=True)

        while pages < max_pages:
            if not has_time(reserve_minutes=15):
                logger.log("Time budget reached during hot/rising scan, stopping")
                break

            resp = make_request("/posts", {"sort": sort_order, "limit": 50, "offset": offset})

            if not resp or not resp.get("success"):
                break

            posts = resp.get("posts", [])
            if not posts:
                break

            for post in posts:
                hot_ids.add(post["id"])
                if post["id"] not in existing_post_ids:
                    new_posts.append(post)

            pbar.update(len(posts))
            pages += 1

            if not resp.get("has_more"):
                break

            offset = resp.get("next_offset", offset + 50)
            time.sleep(REQUEST_DELAY)

        pbar.close()

    already_known = len(hot_ids) - len(new_posts)
    logger.log(f"Hot/Rising/Top: {len(hot_ids)} active posts ({len(new_posts)} new, {already_known} existing to refresh)")
    return hot_ids, new_posts

def _crawl_submolt_pages(submolt_name, sort_order, start_offset, max_pages, known_ids_snapshot):
    """Worker function: crawls pages of a single (submolt, sort) pair.

    Stateless — takes a read-only snapshot of known IDs. Returns the full list
    of posts fetched (caller dedupes against the DB), the final offset reached,
    and whether the submolt was fully paginated.

    Returns: (posts_fetched, last_offset, exhausted)
    """
    all_posts = []
    offset = start_offset
    pages = 0
    consecutive_known = 0
    exhausted = False

    while pages < max_pages:
        if not has_time(reserve_minutes=20):
            break

        resp = make_request("/posts", {
            "submolt": submolt_name,
            "sort": sort_order,
            "limit": 50,
            "offset": offset,
        })

        if not resp or not resp.get("success"):
            break

        posts = resp.get("posts", [])
        if not posts:
            exhausted = True
            break

        all_posts.extend(posts)

        new_in_batch = sum(1 for p in posts if p["id"] not in known_ids_snapshot)

        pages += 1
        offset = resp.get("next_offset", offset + 50)

        if new_in_batch == 0:
            consecutive_known += 1
            if consecutive_known >= 2:
                break
        else:
            consecutive_known = 0

        if not resp.get("has_more"):
            exhausted = True
            break

        time.sleep(REQUEST_DELAY)

    return all_posts, offset, exhausted


def fetch_submolt_backfill(db, submolts):
    """Crawl posts per-submolt to fill coverage gaps in niche communities.

    Uses a progress table so successive runs pick up where the last one left off
    instead of re-scanning from the top. Parallelizes across submolts with a
    thread pool. Prioritizes submolts with the fewest posts in the dataset.

    Args:
        db: SQLite connection
        submolts: list of submolt dicts from fetch_submolts() (must have 'name' key)

    Returns:
        set of newly discovered post IDs
    """
    max_pages = int(os.getenv("SUBMOLT_BACKFILL_PAGES", "20"))
    workers = int(os.getenv("SUBMOLT_WORKERS", "4"))
    # How long before an exhausted submolt becomes eligible for re-scanning
    refresh_hours = int(os.getenv("SUBMOLT_REFRESH_HOURS", "24"))

    submolt_names = [s.get("name") for s in submolts if s.get("name")]

    if not submolt_names:
        logger.log("No submolt names available for backfill")
        return set()

    # Load progress from DB
    progress = {}
    for row in db.execute("SELECT name, sort, last_offset, exhausted, last_crawled_at FROM submolt_progress"):
        progress[(row[0], row[1])] = {
            "last_offset": row[2],
            "exhausted": bool(row[3]),
            "last_crawled_at": row[4],
        }

    # Count posts per submolt for priority ordering
    existing_ids = {row[0] for row in db.execute("SELECT id FROM posts")}
    submolt_post_counts = {}
    for (data_str,) in db.execute("SELECT data FROM posts"):
        post = json.loads(data_str)
        submolt = post.get("submolt")
        if submolt:
            name = submolt.get("name") if isinstance(submolt, dict) else submolt
            if name:
                submolt_post_counts[name] = submolt_post_counts.get(name, 0) + 1

    # Build work queue: (name, sort, start_offset)
    now = datetime.now(timezone.utc)
    refresh_cutoff = now - timedelta(hours=refresh_hours)

    work_queue = []
    for name in submolt_names:
        for sort_order in ["top", "new"]:
            state = progress.get((name, sort_order), {})
            exhausted = state.get("exhausted", False)
            last_crawled_at = state.get("last_crawled_at")
            last_offset = state.get("last_offset", 0)

            # Skip recently-exhausted pairs
            if exhausted and last_crawled_at:
                try:
                    last_dt = datetime.fromisoformat(last_crawled_at)
                    if last_dt > refresh_cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
                # Re-eligible: reset offset to rescan from top
                last_offset = 0

            work_queue.append((name, sort_order, last_offset))

    # Priority: submolts with fewest posts first (fill gaps)
    work_queue.sort(key=lambda w: submolt_post_counts.get(w[0], 0))

    logger.log(
        f"Submolt backfill: {len(work_queue)} work items, {workers} workers, "
        f"max {max_pages} pages each"
    )

    if not work_queue:
        return set()

    new_ids = set()
    submitted = 0
    completed = 0
    pbar = tqdm(desc="[Submolt backfill]", unit=" items", dynamic_ncols=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        def submit_item(item):
            name, sort_order, start_offset = item
            return executor.submit(
                _crawl_submolt_pages, name, sort_order, start_offset, max_pages, existing_ids
            )

        # Prime the pool with an initial batch
        futures = {}
        queue_iter = iter(work_queue)
        for _ in range(workers * 2):
            try:
                item = next(queue_iter)
            except StopIteration:
                break
            if not has_time(reserve_minutes=20):
                break
            futures[submit_item(item)] = item
            submitted += 1

        while futures:
            # Wait for the next future to complete
            done_future = next(as_completed(futures))
            item = futures.pop(done_future)
            name, sort_order, _ = item

            try:
                posts, last_offset, exhausted = done_future.result()
            except Exception as e:
                logger.log(f"Worker failed for {name}/{sort_order}: {e}", "WARN")
                posts, last_offset, exhausted = [], 0, False

            # Dedupe and write to DB (main thread only)
            for post in posts:
                if post["id"] not in existing_ids:
                    existing_ids.add(post["id"])
                    new_ids.add(post["id"])
                db.execute(
                    "INSERT OR REPLACE INTO posts (id, data) VALUES (?, ?)",
                    (post["id"], json.dumps(post, ensure_ascii=False)),
                )

            # Update progress
            db.execute(
                """
                INSERT OR REPLACE INTO submolt_progress
                (name, sort, last_offset, exhausted, last_crawled_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, sort_order, last_offset, 1 if exhausted else 0, now.isoformat()),
            )

            completed += 1
            pbar.update(1)
            pbar.set_postfix(new=len(new_ids))

            # Checkpoint periodically
            if completed % 50 == 0:
                db.commit()

            # Submit next item if time allows
            if has_time(reserve_minutes=20):
                try:
                    next_item = next(queue_iter)
                    futures[submit_item(next_item)] = next_item
                    submitted += 1
                except StopIteration:
                    pass

    pbar.close()
    db.commit()

    logger.log(
        f"Submolt backfill: processed {completed}/{submitted} items, "
        f"found {len(new_ids)} new posts"
    )
    return new_ids

def fetch_all_posts(db):
    """Fetch posts (newest first), stop when we reach known data. Returns total count."""
    logger.log("Fetching Posts (full mode, smart overlap detection)")
    existing_ids = {row[0] for row in db.execute("SELECT id FROM posts")}
    logger.log(f"Loaded {len(existing_ids)} existing post IDs")
    offset = 0
    limit = 50
    fetched = 0
    consecutive_known_batches = 0

    pbar = tqdm(desc="[Posts]", unit=" posts", dynamic_ncols=True)
    while True:
        if not has_time(reserve_minutes=15):
            logger.log("Time budget reached during post listing, stopping")
            break

        resp = make_request("/posts", {"sort": "new", "limit": limit, "offset": offset})

        if not resp or not resp.get("success"):
            logger.log("Failed to fetch posts", "ERROR")
            break

        posts = resp.get("posts", [])
        if not posts:
            break

        new_in_batch = 0
        for post in posts:
            if post["id"] not in existing_ids:
                existing_ids.add(post["id"])
                logger.stats["new_posts"] += 1
                new_in_batch += 1

            db.execute(
                "INSERT OR REPLACE INTO posts (id, data) VALUES (?, ?)",
                (post["id"], json.dumps(post, ensure_ascii=False)),
            )

        fetched += len(posts)
        pbar.update(len(posts))
        pbar.set_postfix(new=logger.stats["new_posts"], total=len(existing_ids))

        if new_in_batch == 0:
            consecutive_known_batches += 1
            if consecutive_known_batches >= 3:
                logger.log("3 consecutive all-known batches, stopping (rest is existing data)")
                break
        else:
            consecutive_known_batches = 0

        # Checkpoint every 1000 posts fetched
        if fetched % 1000 < limit:
            db.commit()

        if not resp.get("has_more"):
            break

        offset = resp.get("next_offset", offset + limit)
        time.sleep(REQUEST_DELAY)
    pbar.close()
    db.commit()

    total = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    logger.log(f"Total posts in dataset: {total}")
    return total

def fetch_post_with_comments(post_id):
    """Fetch single post with full comment tree."""
    resp = make_request(f"/posts/{post_id}")
    if resp and resp.get("success"):
        return resp.get("post"), resp.get("comments", [])
    return None, []

def fetch_comments_only(post_id):
    """Fetch comments for a post (returns full nested tree in one response)."""
    resp = make_request(f"/posts/{post_id}/comments")
    if resp and resp.get("success"):
        return resp.get("comments", [])
    return None

def fetch_all_comments(db, post_ids_to_fetch, existing_full_ids):
    """Fetch comments for posts using parallel requests. Writes directly to SQLite.

    Args:
        db: SQLite connection
        post_ids_to_fetch: set/list of post IDs that need comment fetching
        existing_full_ids: set of post IDs already in posts_full (for refresh vs new detection)
    """
    logger.log("Fetching Comments")

    total = len(post_ids_to_fetch)
    logger.log(f"Fetching comments for {total} posts ({COMMENT_WORKERS} parallel workers)")

    if total == 0:
        logger.log("No new posts to fetch comments for")
        return

    completed = 0
    timed_out = False
    pbar = tqdm(total=total, desc="[Comments]", unit=" posts", dynamic_ncols=True)

    with ThreadPoolExecutor(max_workers=COMMENT_WORKERS) as executor:
        batch_size = 100
        future_to_meta = {}
        id_iter = iter(post_ids_to_fetch)
        submitted = 0

        def submit_batch():
            nonlocal submitted
            count = 0
            for post_id in id_iter:
                is_refresh = post_id in existing_full_ids
                if is_refresh:
                    fut = executor.submit(fetch_comments_only, post_id)
                else:
                    fut = executor.submit(fetch_post_with_comments, post_id)
                future_to_meta[fut] = (post_id, is_refresh)
                submitted += 1
                count += 1
                if count >= batch_size:
                    break
            return count > 0

        submit_batch()

        def handle_result(future):
            post_id, is_refresh = future_to_meta[future]
            if is_refresh:
                comments = future.result()
                if comments is not None:
                    row = db.execute(
                        "SELECT data FROM posts_full WHERE id = ?", (post_id,)
                    ).fetchone()
                    if row:
                        post_data = json.loads(row[0])
                        post_data["comments"] = comments
                        db.execute(
                            "INSERT OR REPLACE INTO posts_full (id, data) VALUES (?, ?)",
                            (post_id, json.dumps(post_data, ensure_ascii=False)),
                        )
            else:
                full_post, comments = future.result()
                if full_post:
                    full_post["comments"] = comments
                    db.execute(
                        "INSERT OR REPLACE INTO posts_full (id, data) VALUES (?, ?)",
                        (post_id, json.dumps(full_post, ensure_ascii=False)),
                    )

        for future in as_completed(future_to_meta):
            handle_result(future)
            completed += 1
            pbar.update(1)

            # Checkpoint every 1000 posts
            if completed % 1000 == 0:
                db.commit()

            # Submit more work if we have time
            if completed >= submitted:
                if not has_time(reserve_minutes=10):
                    logger.log(f"Time budget reached after {completed}/{total} comment fetches")
                    timed_out = True
                    break
                submit_batch()
            elif completed % batch_size == 0 and not has_time(reserve_minutes=10):
                # Stop submitting new batches, but let in-flight work finish
                logger.log(f"Time budget reached after {completed}/{total} comment fetches, draining in-flight requests")
                timed_out = True
                for remaining_future in as_completed(
                    [f for f in future_to_meta if not f.done()]
                ):
                    handle_result(remaining_future)
                    completed += 1
                    pbar.update(1)
                break

    pbar.close()
    db.commit()

def _fetch_comments_background(post_ids_to_fetch, existing_full_ids):
    """Run fetch_all_comments in a background thread with its own DB connection."""
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    fetch_all_comments(db, post_ids_to_fetch, existing_full_ids)
    db.close()

# === MAIN ===

def crawl(mode="incremental"):
    """Run crawler in specified mode."""
    global _start_time
    _start_time = datetime.now(timezone.utc)

    logger.log("=" * 50)
    logger.log(f"MOLTBOOK DATA CRAWLER - {mode.upper()} MODE")
    if TIME_BUDGET_MINUTES > 0:
        logger.log(f"Time budget: {TIME_BUDGET_MINUTES} minutes")
    logger.log(f"Started at: {_start_time.isoformat()}")
    logger.log("=" * 50)

    # Initialize database and migrate from JSON if needed
    db = init_db()
    migrate_json_to_db(db)

    # Fetch submolts (always full, paginated)
    submolts, platform_stats = fetch_submolts()
    save_json(submolts, "submolts.json")
    save_json(platform_stats, "platform_stats.json")

    if mode == "full":
        # Background thread: fetch comments for already-listed posts
        existing_full_ids = {row[0] for row in db.execute("SELECT id FROM posts_full")}
        existing_post_ids = {row[0] for row in db.execute("SELECT id FROM posts")}
        bg_ids = existing_post_ids - existing_full_ids

        comment_thread = None
        if bg_ids:
            logger.log(f"Starting background comment fetch for {len(bg_ids)} posts")
            comment_thread = Thread(
                target=_fetch_comments_background,
                args=(bg_ids, existing_full_ids),
                daemon=True,
            )
            comment_thread.start()

        # Fetch post listings (main thread)
        fetch_all_posts(db)

        # Per-submolt backfill to cover niche communities
        backfill_ids = fetch_submolt_backfill(db, submolts)

        # Wait for background comment fetch
        if comment_thread:
            logger.log("Waiting for background comment fetch to complete...")
            comment_thread.join()
            logger.log("Background comment fetch done")

        # Second pass: fetch comments for any newly discovered posts (including backfill)
        existing_full_ids = {row[0] for row in db.execute("SELECT id FROM posts_full")}
        all_post_ids = {row[0] for row in db.execute("SELECT id FROM posts")}
        remaining = all_post_ids - existing_full_ids
        if remaining:
            fetch_all_comments(db, remaining, existing_full_ids)

    else:  # incremental
        last_crawl = get_last_crawl_time()
        new_ids, updated_ids = fetch_posts_incremental(db, since=last_crawl)

        # Scan hot/rising/top posts to catch active older posts with new comments
        existing_post_ids = {row[0] for row in db.execute("SELECT id FROM posts")}
        hot_ids, hot_new_posts = fetch_hot_posts(existing_post_ids)
        for p in hot_new_posts:
            if p["id"] not in existing_post_ids:
                db.execute(
                    "INSERT OR REPLACE INTO posts (id, data) VALUES (?, ?)",
                    (p["id"], json.dumps(p, ensure_ascii=False)),
                )
                existing_post_ids.add(p["id"])
                new_ids.add(p["id"])
        db.commit()

        # Per-submolt backfill to cover niche communities
        backfill_ids = fetch_submolt_backfill(db, submolts)
        new_ids |= backfill_ids

        post_ids_to_update = new_ids | updated_ids | hot_ids if (new_ids or updated_ids or hot_ids) else set()

        if post_ids_to_update:
            existing_full_ids = {row[0] for row in db.execute("SELECT id FROM posts_full")}
            fetch_all_comments(db, post_ids_to_update, existing_full_ids)

    # Fill in empty comments for posts without any
    ensure_all_posts_in_full(db)

    # Get counts
    post_count = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    post_full_count = db.execute("SELECT COUNT(*) FROM posts_full").fetchone()[0]

    # Export consumer-facing JSON files (streaming, constant memory)
    logger.log("Exporting JSON files...")
    export_posts_json(db)
    export_posts_full_json(db)

    # Save metadata
    crawl_info = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "stats": {
            "submolts": len(submolts),
            "posts": post_count,
            "posts_full": post_full_count,
            "requests": logger.stats["requests_made"],
            "errors": logger.stats["errors"]
        }
    }
    save_metadata(crawl_info)

    db.close()

    logger.log("=" * 50)
    logger.log("CRAWL COMPLETE")
    logger.log("=" * 50)
    logger.log(f"Submolts:     {len(submolts)}")
    logger.log(f"Posts:        {post_count}")
    logger.log(f"Posts (full): {post_full_count}")
    logger.log(f"Data saved to: {RAW_DIR}/")
    logger.log(f"Finished at:  {datetime.now(timezone.utc).isoformat()}")

    logger.save_stats()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Moltbook Data Crawler")
    parser.add_argument("--mode", choices=["full", "incremental"], default="incremental",
                       help="Crawl mode: full (all data) or incremental (only new)")
    parser.add_argument("--full", action="store_true", help="Shorthand for --mode=full")
    parser.add_argument("--time-budget", type=int, default=0,
                       help="Time budget in minutes (0 = unlimited). Crawler will stop "
                            "gracefully before this limit and save progress.")

    args = parser.parse_args()
    mode = "full" if args.full else args.mode
    TIME_BUDGET_MINUTES = args.time_budget

    crawl(mode)
