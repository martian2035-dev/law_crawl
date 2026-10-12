#!/usr/bin/env bash
# 数据文件（data/laws.db、data/laws_content.db）与仓库 Release 之间的上传、下载。
#   scripts/release-data.sh download   取回已发布的数据（Release 不存在时先建一个空的）
#   scripts/release-data.sh upload     把当前数据发布到 Release（覆盖同名文件）
# 需要 gh 命令和环境变量 GH_TOKEN；Release 的标签取环境变量 DATA_TAG，默认 data。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${LAW_CRAWL_DATA_DIR:-$ROOT/data}"
TAG="${DATA_TAG:-data}"
FILES=(laws.db laws_content.db)

ensure_release() {
  gh release view "$TAG" >/dev/null 2>&1 && return
  gh release create "$TAG" --title "法规数据" \
    --notes "由流水线维护的数据文件：laws.db（法规目录）、laws_content.db（法规正文）。每次采集后覆盖更新。"
}

case "${1:-}" in
  download)
    mkdir -p "$DATA_DIR"
    ensure_release
    for file in "${FILES[@]}"; do
      if gh release download "$TAG" --pattern "$file" --dir "$DATA_DIR" --clobber 2>/dev/null; then
        echo "[release-data] 已取回 $file（$(du -h "$DATA_DIR/$file" | cut -f1)）"
      else
        echo "[release-data] Release 里还没有 $file，从头开始"
      fi
    done
    ;;
  upload)
    ensure_release
    for file in "${FILES[@]}"; do
      [ -f "$DATA_DIR/$file" ] || continue
      gh release upload "$TAG" "$DATA_DIR/$file" --clobber
      echo "[release-data] 已发布 $file（$(du -h "$DATA_DIR/$file" | cut -f1)）"
    done
    ;;
  *)
    echo "用法：scripts/release-data.sh download|upload" >&2
    exit 1
    ;;
esac
