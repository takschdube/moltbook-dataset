#!/usr/bin/env bash
# Prune old GitHub releases: keep the last 14 days plus the first release of
# each month; delete the rest, tags included. The pipeline only restores from
# the latest release; Zenodo, Hugging Face, and Kaggle hold the archives.
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

# The first release of each month is a keeper
awk -F'\t' '{m=substr($1,1,7)} !seen[m]++ {print $2}' "$TMP/releases.txt" > "$TMP/keep.txt"

kept=0 pruned=0
while IFS=$'\t' read -r created tag; do
  if [[ "$created" > "$CUTOFF" ]] || grep -qxF "$tag" "$TMP/keep.txt"; then
    kept=$((kept + 1))
    continue
  fi
  if [[ -n "${DRY:-}" ]]; then
    echo "would prune $tag ($created)"
  else
    gh release delete "$tag" --repo "$REPO" --yes --cleanup-tag
    echo "pruned $tag"
  fi
  pruned=$((pruned + 1))
done < "$TMP/releases.txt"

echo "kept $kept, pruned $pruned"
