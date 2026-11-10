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
#
# Exit code 3 means some ministry rule libraries were unreachable. Their previous data is kept, the run reports
# a warning annotation on GitHub, and the pipeline still succeeds: a single ministry being down is not a reason
# to mark the whole crawl as failed. Only a real error (1) or an unexpected exit code fails the job.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TOTAL_SECONDS="${CRAWL_TOTAL_SECONDS:-16200}"
CHUNK_SECONDS="${CRAWL_CHUNK_SECONDS:-2700}"
CONTENT_LIMIT="${CRAWL_CONTENT_LIMIT:-100000}"
EXIT_BLOCKED=2
EXIT_PARTIAL=3
started=$(date +%s)
worst=0
first=1
retry_flag=""
[ "${RETRY_UNREADABLE:-false}" = true ] && retry_flag="--retry-unreadable"

# Exit codes are not a severity scale: rank them so the most severe outcome survives across chunks. Any exit
# code we do not know about is treated as a real failure rather than silently swallowed.
severity() {
  case "$1" in
    0) echo 0 ;;
    "$EXIT_PARTIAL") echo 1 ;;
    "$EXIT_BLOCKED") echo 2 ;;
    1) echo 3 ;;
    *) echo 4 ;;
  esac
}

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
  if [ "$(severity "$status")" -gt "$(severity "$worst")" ]; then worst=$status; fi
  scripts/release-data.sh upload || echo "[crawl-loop] publishing this chunk failed; retrying after the next chunk"
  after=$(content_count)
  echo "[crawl-loop] chunk finished: exit code $status, texts $before -> $after"
  [ "$status" = "$EXIT_BLOCKED" ] && { echo "[crawl-loop] a source demands a browser challenge; stopping for today"; break; }
  [ "$after" = "$before" ] && { echo "[crawl-loop] no new text in this chunk; stopping"; break; }
done

# A browser challenge and a partially unreachable source are expected outcomes: report them, succeed anyway.
case "$worst" in
  "$EXIT_BLOCKED")
    echo "[crawl-loop] a source demanded a browser challenge; treating this run as successful"
    exit 0
    ;;
  "$EXIT_PARTIAL")
    echo "[crawl-loop] some ministry rule libraries were unreachable; see the warnings above; treating this run as successful"
    exit 0
    ;;
  *) exit "$worst" ;;
esac
