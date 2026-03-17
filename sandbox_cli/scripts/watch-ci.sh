#!/usr/bin/env bash
# Watch GitHub Actions runs for a commit until all complete
set -euo pipefail

SHA="${1:-$(git rev-parse HEAD)}"
LIMIT="${LIMIT:-50}"
SLEEP="${SLEEP:-15}"

echo "Watching GitHub Actions for commit: $SHA"

# Wait for runs to appear (up to 5 min)
for _ in $(seq 1 60); do
  runs_json="$(gh run list -c "$SHA" --limit "$LIMIT" --json databaseId,status,conclusion,workflowName,url 2>/dev/null || true)"
  count="$(jq 'length' <<<"$runs_json" 2>/dev/null || echo 0)"
  [[ "$count" -gt 0 ]] && break
  sleep 5
done

if [[ "${count:-0}" -eq 0 ]]; then
  echo "No workflow runs found for $SHA"
  exit 2
fi

# Poll until all complete
while :; do
  runs_json="$(gh run list -c "$SHA" --limit "$LIMIT" --json databaseId,status,conclusion,workflowName,url)"

  echo -e "\n--- $(date +%H:%M:%S) ---"
  jq -r '.[] | "\(.status)\t\(.conclusion // "-")\t\(.workflowName)"' <<<"$runs_json"

  incomplete="$(jq '[.[] | select(.status != "completed")] | length' <<<"$runs_json")"
  [[ "$incomplete" -eq 0 ]] && break

  sleep "$SLEEP"
done

# Summary
failures="$(jq '[.[] | select((.conclusion // "") != "success")] | length' <<<"$runs_json")"
if [[ "$failures" -gt 0 ]]; then
  echo -e "\n❌ CI FAILED for commit $SHA"
  jq -r '.[] | select((.conclusion // "") != "success") | "  \(.workflowName): \(.conclusion // "unknown")\n  \(.url)"' <<<"$runs_json"
  exit 1
fi

echo -e "\n✅ CI PASSED for commit $SHA"
