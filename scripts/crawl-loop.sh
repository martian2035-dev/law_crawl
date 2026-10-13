#!/usr/bin/env bash
# The crawl loop used by the pipeline: one run is cut into chunks, and the data is published to the Release after
# every chunk, so a job that is cancelled or times out loses the last chunk at most.
#
# The first chunk runs `update` (catalogue diff + texts + ministry rules); later chunks only fetch texts. With
# RETRY_UNREADABLE=true the first chunk also clears the "not a docx" unreadable records and fetches those laws
# again, ahead of the rest of the queue (run it once after a new converter was added). The loop
# stops when one of these happens:
#   - the total time is used up (CRAWL_TOTAL_SECONDS, 4.5 hours by default; a GitHub job may run for 6 hours);
#   - a source demands a browser challenge (exit code 2): requests were too frequent, so no more today;
#   - a chunk fetched no new text: everything is fetched, or the source is unavailable.
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
retry_flag=""
[ "${RETRY_UNREADABLE:-false}" = true ] && retry_flag="--retry-unreadable"

content_count() {
  python -m law_crawl manifest 2>/dev/null | python -c 'import json,sys; print(json.load(sys.stdin).get("with_content", 0))'
}

while :; do
  remaining=$((TOTAL_SECONDS - ($(date +%s) - started)))
  [ "$remaining" -le 60 ] && break
  budget=$((remaining < CHUNK_SECONDS ? remaining : CHUNK_SECONDS))
  before=$(content_count)
  if [ "$first" = 1 ]; then
    python -m law_crawl update --content-limit "$CONTENT_LIMIT" --budget-seconds "$budget" $retry_flag
  else
    python -m law_crawl content --limit "$CONTENT_LIMIT" --budget-seconds "$budget"
  fi
  status=$?
  first=0
  [ "$status" -gt "$worst" ] && worst=$status
  scripts/release-data.sh upload || echo "[crawl-loop] publishing this chunk failed; retrying after the next chunk"
  after=$(content_count)
  echo "[crawl-loop] chunk finished: exit code $status, texts $before -> $after"
  [ "$status" = "$EXIT_BLOCKED" ] && { echo "[crawl-loop] a source demands a browser challenge; stopping for today"; break; }
  [ "$after" = "$before" ] && { echo "[crawl-loop] no new text in this chunk; stopping"; break; }
done

# A browser challenge is an expected outcome and must not fail the pipeline; other non-zero exit codes still do
[ "$worst" = "$EXIT_BLOCKED" ] && exit 0
exit "$worst"
