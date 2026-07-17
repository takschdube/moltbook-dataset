#!/usr/bin/env bash
# Retention for per-run (v*) releases: keep the last 14 days plus the first
# release of each calendar month; delete the rest. History is preserved
# elsewhere by design: archive/* releases (never touched here), daily Zenodo
# DOI versions, the Hugging Face mirror's git history, and the
# post_metrics_history table in the shipped database. Git tags are never
# deleted; they remain the per-cycle lineage markers indexed by
# snapshots.json.
#
# Usage: prune_releases.sh [owner/repo]   (DRY=1 to preview without deleting)
set -euo pipefail

REPO="${1:-takschdube/moltbook-dataset}"
CUTOFF=$(date -u -d '14 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
      || date -u -v-14d +%Y-%m-%dT%H:%M:%SZ)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

gh api "repos/$REPO/releases" --paginate \
  --jq '.[] | "\(.created_at)\t\(.tag_name)"' | sort > "$TMP/releases.txt"

# The first v* release of each month is a keeper
awk -F'\t' '$2 ~ /^v/ {m=substr($1,1,7); if (!seen[m]++) print $2}' \
  "$TMP/releases.txt" > "$TMP/keep.txt"

kept=0 pruned=0
while IFS=$'\t' read -r created tag; do
  case "$tag" in
    v*) ;;
    *) kept=$((kept + 1)); continue ;;  # archive/* and anything else: never touched
  esac
  if [[ "$created" > "$CUTOFF" ]] || grep -qxF "$tag" "$TMP/keep.txt"; then
    kept=$((kept + 1))
    continue
  fi
  if [[ -n "${DRY:-}" ]]; then
    echo "would prune $tag ($created)"
  else
    # No --cleanup-tag: tags are permanent lineage markers. Non-fatal per
    # release; anything skipped is retried on the next run.
    gh release delete "$tag" --repo "$REPO" --yes \
      || echo "WARN: prune failed for $tag"
    echo "pruned $tag"
  fi
  pruned=$((pruned + 1))
done < "$TMP/releases.txt"

echo "kept $kept, pruned $pruned"
