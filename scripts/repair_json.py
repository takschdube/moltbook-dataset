#!/usr/bin/env python3
"""
Repair a truncated JSON array file by recovering all complete objects.

Uses ijson for streaming parse — constant memory regardless of file size.
Stops gracefully at the truncation point and writes a valid JSON array.

Usage:
    uv run python scripts/repair_json.py data/raw/posts_full.json
    uv run python scripts/repair_json.py data/raw/posts_full.json --output repaired.json
"""
import sys
import json
import ijson


def repair(input_path, output_path=None):
    if output_path is None:
        output_path = input_path

    count = 0
    tmp_path = output_path + ".tmp"

    with open(input_path, "rb") as f, \
         open(tmp_path, "w", encoding="utf-8") as out:
        out.write("[\n")
        try:
            for obj in ijson.items(f, "item"):
                if count > 0:
                    out.write(",\n")
                json.dump(obj, out, ensure_ascii=False)
                count += 1
                if count % 10000 == 0:
                    print(f"  {count} objects recovered...", flush=True)
        except (ijson.common.IncompleteJSONError, ijson.common.JSONError):
            print(f"  Truncation detected after {count} complete objects")
        out.write("\n]")

    # Atomic replace
    import os
    os.replace(tmp_path, output_path)
    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"Done: {count} objects recovered ({size_mb:.1f} MB) -> {output_path}")
    return count


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/repair_json.py INPUT.json [--output OUTPUT.json]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        output_path = sys.argv[idx + 1]

    repair(input_path, output_path)
