#!/usr/bin/env python3
"""Checks for the Zenodo uploader, without network.

The defect these guard against: on 2026-10-15 one of two uploads failed and
the uploader published anyway, minting a version without the database.

Run: uv run python tests/test_zenodo_upload.py
"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import upload_to_zenodo as z  # noqa: E402

z.time.sleep = lambda s: None
z.PART_SIZE = 1000


class Resp:
    def __init__(self, status, body=b""):
        self.status_code = status
        self.text = ""
        self._md5 = "md5:" + hashlib.md5(body).hexdigest()

    def json(self):
        return {"checksum": self._md5}


def fake_put(fail, stored, calls):
    """A bucket that answers 502 to the keys in fail, the first time or always."""
    def put(url, data, headers, timeout):
        key, body = url.rsplit("/", 1)[1], data.read()
        calls.append(key)
        if fail.get(key) == "always" or (fail.get(key) == "once" and calls.count(key) == 1):
            return Resp(502)
        stored[key] = body
        return Resp(201, body)
    return put


def write(nbytes):
    path = Path(tempfile.mkdtemp()) / "moltbook.db.zst"
    path.write_bytes(os.urandom(nbytes))
    return path


def test_parts_reassemble_and_a_failed_part_is_retried():
    path, stored, calls = write(2500), {}, []
    z.requests.put = fake_put({"moltbook.db.zst.part001": "once"}, stored, calls)
    assert z.upload_file("https://bucket", path, "moltbook.db.zst")
    keys = sorted(stored)
    assert keys == ["moltbook.db.zst.part000", "moltbook.db.zst.part001",
                    "moltbook.db.zst.part002"], keys
    assert b"".join(stored[k] for k in keys) == path.read_bytes()
    assert calls.count("moltbook.db.zst.part001") == 2


def test_small_file_keeps_its_name():
    path, stored, calls = write(10), {}, []
    z.requests.put = fake_put({}, stored, calls)
    assert z.upload_file("https://bucket", path, "moltbook.db.zst")
    assert list(stored) == ["moltbook.db.zst"]


def test_a_part_that_never_goes_up_stops_the_upload():
    path, stored, calls = write(2500), {}, []
    z.requests.put = fake_put({"moltbook.db.zst.part001": "always"}, stored, calls)
    assert not z.upload_file("https://bucket", path, "moltbook.db.zst")
    assert calls.count("moltbook.db.zst.part001") == z.UPLOAD_ATTEMPTS
    assert "moltbook.db.zst.part002" not in calls


def test_corrupted_part_is_not_accepted():
    path, calls = write(10), []

    def put(url, data, headers, timeout):
        calls.append(data.read())
        return Resp(201, b"not what was sent")

    z.requests.put = put
    assert not z.upload_file("https://bucket", path, "moltbook.db.zst")
    assert len(calls) == z.UPLOAD_ATTEMPTS


def test_incomplete_upload_is_never_published():
    published = []
    z.ZENODO_TOKEN = "test-token-not-used"
    z.due = lambda: True
    z.get_or_create_draft = lambda: (1, "https://bucket", False)
    z.set_metadata = lambda deposition_id: None
    z.upload_files = lambda bucket_url: False
    z.publish = published.append
    try:
        z.main()
    except SystemExit as e:
        assert e.code == 1
    else:
        raise AssertionError("main() went on after a failed upload")
    assert published == []


def test_version_without_the_database_is_incomplete():
    assert not z.complete(["moltbook-dataset-2026-10-15.zip"])
    assert not z.complete(["moltbook.db.zst"])
    assert z.complete(["moltbook-dataset-2026-09-14.zip", "moltbook.db.zst"])
    assert z.complete(["moltbook-dataset-2026-10-16.zip.part000",
                       "moltbook.db.zst.part000"])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
