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
        steps = doc["jobs"]["crawl-and-publish"]["steps"]
        assert steps, f"{path.name} has no steps"
        # every step is either a shell command or an action, never both
        for step in steps:
            assert ("run" in step) != ("uses" in step), step.get("name")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
