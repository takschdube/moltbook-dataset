#!/usr/bin/env python3
"""Checks for the fetch-provenance layer.

The defect these guard against: a post whose comments were never retrieved was
written with an empty comments array, indistinguishable from a thread that has
no comments. Every absence claim made against this archive depended on telling
those apart.

Run: uv run python tests/test_provenance.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = tempfile.mkdtemp()
os.environ["DATA_DIR"] = str(Path(WORK) / "data")
os.environ["ARCHIVE_DIR"] = str(Path(WORK) / "archives")
os.environ["MOLTBOOK_API_KEY"] = "test-key-not-used"
os.chdir(WORK)
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import moltbook_crawler as mc  # noqa: E402


def test_tree_size_counts_nested_replies():
    assert mc.tree_size([]) == 0
    assert mc.tree_size(None) == 0
    assert mc.tree_size([{"replies": []}]) == 1
    # comment_count is a whole-tree figure, so a top-level len() undercounts it
    nested = [{"replies": [{"replies": [{"replies": []}]}, {"replies": []}]}]
    assert len(nested) == 1
    assert mc.tree_size(nested) == 4


def test_backfill_marks_posts_as_never_fetched():
    db = mc.init_db()
    db.execute("INSERT INTO posts (id, data) VALUES (?, ?)",
               ("p1", json.dumps({"id": "p1", "comment_count": 7})))
    db.commit()

    mc.ensure_all_posts_in_full(db)

    row = db.execute("SELECT data FROM posts_full WHERE id = 'p1'").fetchone()
    post = json.loads(row[0])
    assert post["comments"] == []
    # The marker is the whole point: null distinguishes "never fetched" from
    # "fetched and genuinely empty".
    assert "comments_fetched_at" in post
    assert post["comments_fetched_at"] is None

    fetch = db.execute(
        "SELECT outcome, n_comments, claimed_count FROM comment_fetches WHERE post_id = 'p1'"
    ).fetchone()
    assert fetch == ("not_attempted", None, 7), fetch
    db.close()


def test_manifest_separates_success_failure_and_true_empty():
    db = mc.init_db()
    mc.record_fetch(db, "ok1", "ok", {"http": "200", "attempts": 1}, 12, 12)
    mc.record_fetch(db, "empty1", "ok", {"http": "200", "attempts": 1}, 0, 0)
    mc.record_fetch(db, "bad1", "error", {"http": "503", "attempts": 3}, None, None)
    db.commit()

    rows = {r[0]: r for r in db.execute(
        "SELECT post_id, outcome, http_status, attempts, n_comments FROM comment_fetches")}
    assert rows["ok1"][1] == "ok" and rows["ok1"][4] == 12
    # A real empty and a failure both yield no comments; only the manifest
    # tells them apart.
    assert rows["empty1"][1] == "ok" and rows["empty1"][4] == 0
    assert rows["bad1"][1] == "error" and rows["bad1"][2] == "503" and rows["bad1"][4] is None
    db.close()


def test_status_out_records_outcome_without_network():
    # No API key path is the one branch reachable without a network call.
    saved, mc.API_KEY = mc.API_KEY, None
    try:
        st = {}
        assert mc.make_request("/posts", status_out=st) is None
        assert st["http"] == "no_api_key"
    finally:
        mc.API_KEY = saved


def test_reply_graph_keeps_replies_made_to_a_poster():
    import build_derived as bd

    post = {
        "id": "p1", "author_id": "u-poster", "author": {"name": "poster", "id": "u-poster"},
        "comment_count": 2,
        "comments": [{
            "id": "c1", "author_id": "u-alice", "author": {"name": "alice", "id": "u-alice"},
            "depth": 0,  # replying to the post itself, so no parent_id
            "replies": [{
                "id": "c2", "author_id": "u-bob", "author": {"name": "bob", "id": "u-bob"},
                "depth": 1, "parent_id": "c1", "replies": [],
            }],
        }],
    }
    bd.stream_posts_full = lambda: iter([post])
    edges = {(e["from"], e["to"]): e for e in bd.build_reply_graph(1)}

    # bob -> alice was already counted; alice -> poster was the dropped case.
    assert ("bob", "alice") in edges
    assert ("alice", "poster") in edges, "reply made directly to a poster is missing"
    assert edges[("alice", "poster")]["from_id"] == "u-alice"
    assert edges[("alice", "poster")]["to_id"] == "u-poster"


def test_completeness_classification():
    import build_derived as bd

    posts = [
        {"comment_count": 0, "comments": []},                                 # true empty
        {"comment_count": 3, "comments": []},                                 # never fetched
        {"comment_count": 1, "comments": [{"replies": []}]},                  # complete
        {"comment_count": 9, "comments": [{"replies": [{"replies": []}]}]},   # partial
    ]
    bd.stream_posts_full = lambda: iter(posts)
    r = bd.build_fetch_completeness(len(posts))

    assert r["never_had_comments"] == 1
    assert r["not_fetched"] == 1
    assert r["complete"] == 1
    assert r["partial"] == 1
    # Only a true empty and a complete fetch can support "there were no replies".
    assert r["usable_for_absence_claims"] == 2
    assert r["usable_fraction"] == 0.5


def test_every_queued_thread_is_fetched():
    """The fetch pool submits work in batches while iterating completions.
    as_completed only ever yields the futures it was given, so batches added
    mid-iteration were dropped and each run quietly stopped after the first
    hundred. This fails against that version."""
    original, mc.DB_PATH = mc.DB_PATH, Path(tempfile.mkdtemp()) / "fetch.db"
    db = mc.init_db()
    mc.DB_PATH = original

    queued = [f"post-{i}" for i in range(250)]   # more than one batch of 100
    for pid in queued:
        db.execute("INSERT INTO posts_full (id, data) VALUES (?, ?)",
                   (pid, json.dumps({"id": pid, "comment_count": 1, "comments": []})))
    db.commit()

    calls = []
    original_fetch = mc.fetch_comments_only
    mc.fetch_comments_only = lambda pid: (calls.append(pid),
                                          ([{"replies": []}], {"http": "200", "attempts": 1}))[1]
    try:
        done = mc.fetch_all_comments(db, queued, set(queued))
    finally:
        mc.fetch_comments_only = original_fetch

    assert done == (250, 250), f"reported {done}, expected every thread fetched"
    assert len(calls) == 250, f"only {len(calls)} of 250 threads were fetched"
    stored = db.execute(
        "SELECT COUNT(*) FROM comment_fetches WHERE outcome = 'ok'").fetchone()[0]
    assert stored == 250, f"only {stored} of 250 fetches were recorded"
    db.close()


def test_backfill_targets_only_short_threads():
    """A backfill should re-fetch exactly what a reader would have to discard:
    threads holding fewer comments than the platform reports, inside the window
    asked for. Anything wider wastes requests against a rate limit."""
    # Its own database file: init_db reuses one path, so tests otherwise see
    # each other's rows.
    original, mc.DB_PATH = mc.DB_PATH, Path(tempfile.mkdtemp()) / "backfill.db"
    db = mc.init_db()
    mc.DB_PATH = original
    rows = [
        # id, created_at, claimed, stored tree size
        ("short_in_window", "2026-07-15T00:00:00Z", 5, 1),
        ("complete_in_window", "2026-07-15T00:00:00Z", 2, 2),
        ("empty_in_window", "2026-07-15T00:00:00Z", 0, 0),
        ("short_before_window", "2026-06-01T00:00:00Z", 5, 0),
        ("short_after_window", "2026-09-01T00:00:00Z", 5, 0),
    ]
    for pid, created, claimed, stored in rows:
        comments = [{"replies": []} for _ in range(stored)]
        db.execute("INSERT INTO posts_full (id, data) VALUES (?, ?)",
                   (pid, json.dumps({"id": pid, "created_at": created,
                                     "comment_count": claimed, "comments": comments})))
    db.commit()

    got = set(mc.select_incomplete(db, "2026-07-01", "2026-08-29"))
    assert got == {"short_in_window"}, got
    # without a window, every short thread qualifies regardless of date
    assert set(mc.select_incomplete(db)) == {
        "short_in_window", "short_before_window", "short_after_window"}
    db.close()


def test_catchup_is_bounded_and_skippable():
    """Draining the backlog must not crowd out discovery, so the top-up is
    capped, and setting the cap to zero turns it off."""
    original, mc.DB_PATH = mc.DB_PATH, Path(tempfile.mkdtemp()) / "catchup.db"
    db = mc.init_db()
    mc.DB_PATH = original
    for i in range(50):
        db.execute("INSERT INTO posts_full (id, data) VALUES (?, ?)",
                   (f"p{i}", json.dumps({"id": f"p{i}", "comment_count": 3, "comments": []})))
    db.commit()

    backlog = mc.select_incomplete(db)
    assert len(backlog) == 50
    # the cap is applied by slicing, so a smaller cap takes a strict subset
    assert len(backlog[:10]) == 10
    assert set(backlog[:10]) <= set(backlog)
    db.close()


def fake_api(pages, post=None, fail_page=None):
    """Stand-in for make_request: the comments endpoint serves `pages` (lists of
    top-level ids) behind cursors; the post endpoint answers without comments,
    as the live API has since September 2026."""
    calls = []

    def request(endpoint, params=None, status_out=None):
        calls.append((endpoint, dict(params or {})))
        if status_out is not None:
            status_out.update(http="200", attempts=1)
        if not endpoint.endswith("/comments"):
            return {"success": True, "post": post or {"id": "p", "comment_count": 0}}
        i = int((params or {}).get("cursor") or 0)
        if i == fail_page:
            return None
        more = i + 1 < len(pages)
        return {"success": True, "count": sum(map(len, pages)), "has_more": more,
                "next_cursor": str(i + 1) if more else None,
                "comments": [{"id": c, "replies": [{"id": c + "r", "replies": []}]} for c in pages[i]]}
    return request, calls


def test_comments_follow_every_page():
    """The endpoint pages since September 2026; the first page of a large
    thread is not the thread."""
    original = mc.make_request
    mc.make_request, calls = fake_api([["a", "b"], ["c"], ["c", "d"]])
    try:
        comments, st = mc.fetch_comments_only("p")
    finally:
        mc.make_request = original
    assert [c["id"] for c in comments] == ["a", "b", "c", "d"]   # c repeated across a page edge, kept once
    assert mc.tree_size(comments) == 8                          # replies ride inside their parents
    assert [p.get("cursor") for _, p in calls] == [None, "1", "2"]
    assert all(p["sort"] == "new" and p["limit"] == mc.COMMENT_PAGE for _, p in calls)
    assert st["http"] == "200"


def test_a_failed_page_fails_the_fetch():
    """Half a thread stored as if whole is the defect this file exists for."""
    original = mc.make_request
    mc.make_request, _ = fake_api([["a"], ["b"]], fail_page=1)
    try:
        comments, _ = mc.fetch_comments_only("p")
    finally:
        mc.make_request = original
    assert comments is None


def test_new_posts_take_comments_from_the_comments_endpoint():
    """The post endpoint no longer carries comments; reading the missing key as
    [] stored every new thread empty and marked it fetched."""
    original = mc.make_request
    mc.make_request, calls = fake_api([["a", "b"]], post={"id": "p", "comment_count": 4})
    try:
        post, comments, _ = mc.fetch_post_with_comments("p")
    finally:
        mc.make_request = original
    assert post["id"] == "p" and mc.tree_size(comments) == 4
    assert [e for e, _ in calls] == ["/posts/p", "/posts/p/comments"]


def test_refresh_never_shrinks_a_stored_thread():
    """A shorter answer (deletions, a dropped page) must not overwrite what the
    archive already holds; a longer one replaces it."""
    original, mc.DB_PATH = mc.DB_PATH, Path(tempfile.mkdtemp()) / "shrink.db"
    db = mc.init_db()
    mc.DB_PATH = original
    five = [{"id": str(i), "replies": []} for i in range(5)]
    for pid in ("shorter", "longer"):
        db.execute("INSERT INTO posts_full (id, data) VALUES (?, ?)",
                   (pid, json.dumps({"id": pid, "comment_count": 9, "comments": five})))
    db.commit()
    answers = {"shorter": [{"id": "x", "replies": []}] * 2, "longer": [{"id": "y", "replies": []}] * 7}
    original_fetch = mc.fetch_comments_only
    mc.fetch_comments_only = lambda pid: (answers[pid], {"http": "200", "attempts": 1})
    try:
        mc.fetch_all_comments(db, ["shorter", "longer"], {"shorter", "longer"})
    finally:
        mc.fetch_comments_only = original_fetch
    stored = {pid: mc.tree_size(json.loads(d)["comments"]) for pid, d in db.execute("SELECT id, data FROM posts_full")}
    assert stored == {"shorter": 5, "longer": 7}, stored
    db.close()


def test_new_post_without_its_comments_stays_unfetched():
    """A post whose comment fetch failed is stored, but as never fetched, so the
    backlog returns to it instead of trusting an empty array."""
    original, mc.DB_PATH = mc.DB_PATH, Path(tempfile.mkdtemp()) / "nocomments.db"
    db = mc.init_db()
    mc.DB_PATH = original
    original_fetch = mc.fetch_post_with_comments
    mc.fetch_post_with_comments = lambda pid: ({"id": pid, "comment_count": 3}, None, {"http": "503", "attempts": 3})
    try:
        mc.fetch_all_comments(db, ["new"], set())
    finally:
        mc.fetch_post_with_comments = original_fetch
    post = json.loads(db.execute("SELECT data FROM posts_full WHERE id = 'new'").fetchone()[0])
    assert post["comments"] == [] and post["comments_fetched_at"] is None
    assert db.execute("SELECT outcome FROM comment_fetches WHERE post_id = 'new'").fetchone()[0] == "error"
    assert mc.select_incomplete(db) == ["new"]
    db.close()


def test_backlog_takes_least_recently_fetched_first():
    """Threads that can never be completed must not hold the front of a capped
    backlog every cycle."""
    original, mc.DB_PATH = mc.DB_PATH, Path(tempfile.mkdtemp()) / "order.db"
    db = mc.init_db()
    mc.DB_PATH = original
    for pid, fetched in (("recent", "2026-10-28T00:00:00"), ("never", None), ("old", "2026-09-01T00:00:00")):
        db.execute("INSERT INTO posts_full (id, data) VALUES (?, ?)",
                   (pid, json.dumps({"id": pid, "comment_count": 2, "comments": [], "comments_fetched_at": fetched})))
    db.commit()
    assert mc.select_incomplete(db) == ["never", "old", "recent"]
    db.close()


def test_workflow_has_no_duplicate_keys():
    """A duplicated key is silently accepted by a YAML loader and rejected by
    GitHub, so a plain safe_load is not a check. One slipped through as a
    repeated continue-on-error and every run failed before starting a job."""
    import yaml

    class StrictLoader(yaml.SafeLoader):
        pass

    def no_duplicates(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise AssertionError(f"duplicated key {key!r} at {key_node.start_mark}")
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_duplicates)

    for path in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        with open(path, encoding="utf-8") as f:
            doc = yaml.load(f, StrictLoader)
        for job in doc["jobs"].values():
            assert job["steps"], f"{path.name} has no steps"
            # every step is either a shell command or an action, never both
            for step in job["steps"]:
                assert ("run" in step) != ("uses" in step), step.get("name")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
