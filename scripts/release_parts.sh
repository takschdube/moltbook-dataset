#!/usr/bin/env bash
# GitHub release assets must be under 2 GiB, so a file past PART_BYTES goes up
# as numbered parts, named as on Zenodo: FILE.part000, FILE.part001, ...
#
#   release_parts.sh split FILE   print the files to upload: FILE, or its parts
#   release_parts.sh join FILE    rebuild FILE from its parts, if it was split
set -euo pipefail
PART_BYTES=${PART_BYTES:-1900000000}
f=$2
case $1 in
  split)
    if [ "$(wc -c < "$f")" -le "$PART_BYTES" ]; then
      echo "$f"
    else
      split -b "$PART_BYTES" -d -a 3 "$f" "$f.part"
      ls "$f".part*
    fi ;;
  join)
    if compgen -G "$f.part*" > /dev/null; then
      cat "$f".part* > "$f"
      rm "$f".part*
    fi ;;
  *) echo "usage: $0 split|join FILE" >&2; exit 2 ;;
esac
