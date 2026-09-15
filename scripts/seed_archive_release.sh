#!/usr/bin/env bash
# Publish a restored database as the release the next crawl will resume from.
#
# Order matters. The collector must already be running the current code before
# this lands, because the pipeline that picks this database up needs the runner
# disk reclamation, the 2 GB asset check and the daily large-file cadence to
# handle a corpus this size. Seeding first and merging afterwards means one
# crawl runs the old pipeline against the full corpus.
#
# The release is tagged archive/* so retention never prunes it: this is the
# point the corpus was restored from, and it should stay reachable.
#
# Usage: seed_archive_release.sh <moltbook.db> [owner/repo]
#        DRY=1 to check without publishing
set -euo pipefail

DB="${1:?usage: seed_archive_release.sh <moltbook.db> [owner/repo]}"
REPO="${2:-takschdube/moltbook-dataset}"
TAG="archive/restored-$(date -u +%Y-%m-%d)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

[ -f "$DB" ] || { echo "ERROR: $DB not found"; exit 1; }

echo "Checking $DB"
CHECK=$(sqlite3 "$DB" "PRAGMA integrity_check;" | head -1)
[ "$CHECK" = "ok" ] || { echo "ERROR: integrity check said: $CHECK"; exit 1; }

POSTS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM posts;")
FULL=$(sqlite3 "$DB" "SELECT COUNT(*) FROM posts_full;")
echo "  integrity ok, $POSTS posts, $FULL with comment records"
[ "$POSTS" -gt 0 ] || { echo "ERROR: no posts"; exit 1; }
[ "$POSTS" = "$FULL" ] || echo "  WARNING: posts and posts_full differ"

# Refuse to seed something smaller than what is already published: that is the
# failure this whole guard exists to prevent.
if [ -f corpus_state.json ]; then
  KNOWN=$(python3 -c "import json;print(json.load(open('corpus_state.json'))['posts'])" 2>/dev/null || echo 0)
  if [ "$KNOWN" -gt 0 ] && [ "$POSTS" -lt "$KNOWN" ]; then
    echo "ERROR: $POSTS posts is below the recorded $KNOWN. Refusing to seed."
    exit 1
  fi
fi

echo "Compressing"
zstd -3 -f "$DB" -o "$WORK/moltbook.db.zst" -q
SIZE=$(stat -c%s "$WORK/moltbook.db.zst" 2>/dev/null || stat -f%z "$WORK/moltbook.db.zst")
echo "  $((SIZE / 1024 / 1024)) MB"
if [ "$SIZE" -gt 2000000000 ]; then
  echo "ERROR: past the 2 GB release asset limit. Publish via the Hugging Face mirror instead."
  exit 1
fi

if [ -n "${DRY:-}" ]; then
  echo "DRY: would publish $TAG to $REPO as Latest"
  echo "DRY: would raise the corpus_state.json baseline to $POSTS"
  exit 0
fi

# Raise the high-water mark only once the release exists. Until then the
# baseline must describe what is actually published, or the next crawl's
# restore guard fails against a corpus that is still the old size.
python3 - "$POSTS" "$FULL" <<'STATE'
import json, sys
from datetime import datetime, timezone
json.dump({"posts": int(sys.argv[1]), "posts_full": int(sys.argv[2]),
           "schema_version": "2026.09.15",
           "updated_at": datetime.now(timezone.utc).isoformat()},
          open("corpus_state.json", "w"), indent=2)
print("  corpus_state.json raised to", sys.argv[1])
STATE

echo "Publishing $TAG"
gh release create "$TAG" "$WORK/moltbook.db.zst" \
  --repo "$REPO" \
  --title "Restored corpus $(date -u +%Y-%m-%d)" \
  --notes "Working database holding $POSTS posts, merged from an archived snapshot. The next crawl resumes from here. Tagged archive/ so retention leaves it in place." \
  --latest

echo
echo "Done. Commit the updated corpus_state.json so the restore guard has its baseline."
