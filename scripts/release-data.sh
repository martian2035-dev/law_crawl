#!/usr/bin/env bash
# Upload and download the data files (data/laws.db, data/laws_content.db) to and from the repository's Release.
#   scripts/release-data.sh download   fetch the published data (creates an empty Release if there is none)
#   scripts/release-data.sh upload     publish the current data to the Release (overwrites files of the same name)
# Needs the gh command and GH_TOKEN. The Release tag comes from DATA_TAG and defaults to "data".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${LAW_CRAWL_DATA_DIR:-$ROOT/data}"
TAG="${DATA_TAG:-data}"
FILES=(laws.db laws_content.db)

ensure_release() {
  gh release view "$TAG" >/dev/null 2>&1 && return
  gh release create "$TAG" --title "Law data" \
    --notes "Data files maintained by the pipeline: laws.db (catalogue) and laws_content.db (texts). Overwritten after every crawl. Field values are in Chinese, as published by the sources."
}

case "${1:-}" in
  download)
    mkdir -p "$DATA_DIR"
    ensure_release
    for file in "${FILES[@]}"; do
      if gh release download "$TAG" --pattern "$file" --dir "$DATA_DIR" --clobber 2>/dev/null; then
        echo "[release-data] downloaded $file ($(du -h "$DATA_DIR/$file" | cut -f1))"
      else
        echo "[release-data] the Release has no $file yet; starting from scratch"
      fi
    done
    ;;
  upload)
    ensure_release
    for file in "${FILES[@]}"; do
      [ -f "$DATA_DIR/$file" ] || continue
      gh release upload "$TAG" "$DATA_DIR/$file" --clobber
      echo "[release-data] published $file ($(du -h "$DATA_DIR/$file" | cut -f1))"
    done
    ;;
  *)
    echo "usage: scripts/release-data.sh download|upload" >&2
    exit 1
    ;;
esac
