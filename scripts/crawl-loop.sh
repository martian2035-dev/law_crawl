#!/usr/bin/env bash
# 流水线里的采集循环：把一次运行的时间切成若干段，每段结束就把数据发布到 Release，
# 这样作业中途被取消或超时，最多只丢最后一段的进度。
#
# 第一段跑 update（目录比对 + 正文 + 部门规章），之后各段只取正文。出现下列情况之一就停止：
#   - 用完总时长（CRAWL_TOTAL_SECONDS，默认 4.5 小时；GitHub 单个作业最长 6 小时）；
#   - 数据源要求浏览器验证（退出码 2）：说明访问过于频繁，今天不再继续；
#   - 一段下来没有取到新的正文：已经取完，或数据源不可用。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TOTAL_SECONDS="${CRAWL_TOTAL_SECONDS:-16200}"
CHUNK_SECONDS="${CRAWL_CHUNK_SECONDS:-2700}"
CONTENT_LIMIT="${CRAWL_CONTENT_LIMIT:-100000}"
EXIT_BLOCKED=2
started=$(date +%s)
worst=0
first=1

content_count() {
  python -m law_crawl manifest 2>/dev/null | python -c 'import json,sys; print(json.load(sys.stdin).get("with_content", 0))'
}

while :; do
  remaining=$((TOTAL_SECONDS - ($(date +%s) - started)))
  [ "$remaining" -le 60 ] && break
  budget=$((remaining < CHUNK_SECONDS ? remaining : CHUNK_SECONDS))
  before=$(content_count)
  if [ "$first" = 1 ]; then
    python -m law_crawl update --content-limit "$CONTENT_LIMIT" --budget-seconds "$budget"
  else
    python -m law_crawl content --limit "$CONTENT_LIMIT" --budget-seconds "$budget"
  fi
  status=$?
  first=0
  [ "$status" -gt "$worst" ] && worst=$status
  scripts/release-data.sh upload || echo "[crawl-loop] 本段数据发布失败，下一段结束时再试"
  after=$(content_count)
  echo "[crawl-loop] 本段结束：退出码 $status，已有正文 $before → $after"
  [ "$status" = "$EXIT_BLOCKED" ] && { echo "[crawl-loop] 数据源要求浏览器验证，今天到此为止"; break; }
  [ "$after" = "$before" ] && { echo "[crawl-loop] 本段没有新增正文，停止"; break; }
done

# 浏览器验证是预期内的情况，不让流水线显示为失败；其余非零退出码照常报错
[ "$worst" = "$EXIT_BLOCKED" ] && exit 0
exit "$worst"
