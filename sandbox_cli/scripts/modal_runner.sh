#!/usr/bin/env bash
# Modal sandbox runner script.
# Executed inside a Modal sandbox via sb.exec().
# Args: REPO_URL BRANCH PROVIDER_CMD...
# Secrets expected in env: GH_TOKEN, and provider-specific tokens.
set -euo pipefail

# --- Input validation ---
REPO_URL="${1:-}"
BRANCH="${2:-}"
shift 2 || true
PROVIDER_CMD=("$@")

if [[ -z "$REPO_URL" ]]; then
    echo "ERROR: REPO_URL is required" >&2
    exit 1
fi
if [[ -z "$BRANCH" ]]; then
    echo "ERROR: BRANCH is required" >&2
    exit 1
fi
if [[ ${#PROVIDER_CMD[@]} -eq 0 ]]; then
    echo "ERROR: PROVIDER_CMD is required" >&2
    exit 1
fi
if [[ -z "${GH_TOKEN:-}" ]]; then
    echo "ERROR: GH_TOKEN is required" >&2
    exit 1
fi

# --- Result trap: always emit __SANDBOX_RESULT__ even on failure ---
EXIT_CODE=0
COMMIT_SHA=""
PUSHED=false
RESULT_ERROR=""

emit_result() {
    local exit_code="${EXIT_CODE}"
    local commit_sha="${COMMIT_SHA}"
    local pushed="${PUSHED}"

    # Build modified files list
    local modified_files="[]"
    if [[ -n "$commit_sha" ]] && git -C /workspace rev-parse HEAD >/dev/null 2>&1; then
        local files
        files=$(git -C /workspace diff --name-only "${BASE_COMMIT:-HEAD~1}" HEAD 2>/dev/null | jq -R . | jq -s . 2>/dev/null || echo "[]")
        modified_files="$files"
    fi

    # Build diff stats
    local diff_stats="{}"
    if [[ -n "$commit_sha" ]] && [[ -n "${BASE_COMMIT:-}" ]]; then
        local numstat
        numstat=$(git -C /workspace diff --numstat "${BASE_COMMIT}" HEAD 2>/dev/null || true)
        if [[ -n "$numstat" ]]; then
            local files_changed insertions deletions
            files_changed=$(echo "$numstat" | wc -l | tr -d ' ')
            insertions=$(echo "$numstat" | awk '{sum += $1} END {print sum+0}')
            deletions=$(echo "$numstat" | awk '{sum += $2} END {print sum+0}')
            diff_stats="{\"filesChanged\": ${files_changed}, \"insertions\": ${insertions}, \"deletions\": ${deletions}}"
        fi
    fi

    local error_field=""
    if [[ -n "${RESULT_ERROR}" ]]; then
        error_field=", \"error\": $(echo "$RESULT_ERROR" | jq -R .)"
    fi

    echo "__SANDBOX_RESULT__ {\"exitCode\": ${exit_code}, \"commitSha\": $(echo "$commit_sha" | jq -R .), \"modifiedFiles\": ${modified_files}, \"diffStats\": ${diff_stats}, \"pushed\": ${pushed}${error_field}}"
}

trap 'EXIT_CODE=$?; emit_result' EXIT

# --- Configure git auth via GIT_ASKPASS ---
cat >/tmp/git-askpass.sh <<'ASKPASS'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' "x-access-token" ;;
  *Password*) printf '%s\n' "${GH_TOKEN}" ;;
  *) printf '\n' ;;
esac
ASKPASS
chmod 700 /tmp/git-askpass.sh
export GIT_ASKPASS=/tmp/git-askpass.sh
export GIT_TERMINAL_PROMPT=0

# Configure git identity for commits
git config --global user.email "sandbox@modal.local"
git config --global user.name "Sandbox"

# --- Clone ---
echo "Cloning $REPO_URL branch $BRANCH..." >&2
git clone --branch "$BRANCH" --single-branch "$REPO_URL" /workspace
cd /workspace

BASE_COMMIT=$(git rev-parse HEAD)
export BASE_COMMIT

# --- Run agent ---
echo "Running: ${PROVIDER_CMD[*]}" >&2
set +e
"${PROVIDER_CMD[@]}"
AGENT_EXIT=$?
set -e
EXIT_CODE=$AGENT_EXIT

# --- Commit changes ---
git add -A
# Never commit .env files or codex result file
git reset HEAD -- '.env*' '.sandbox-result.txt' 2>/dev/null || true

if git status --porcelain | grep -q .; then
    if git commit -m "sandbox: $BRANCH"; then
        COMMIT_SHA=$(git rev-parse HEAD)
        # --- Push ---
        if git push origin "HEAD:$BRANCH"; then
            PUSHED=true
        else
            PUSHED=false
            RESULT_ERROR="git push failed"
        fi
    else
        RESULT_ERROR="git commit failed"
    fi
fi

exit "$AGENT_EXIT"
