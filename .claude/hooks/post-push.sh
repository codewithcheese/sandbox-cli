#!/bin/bash
# Hook: PostToolUse (Bash) - triggers watch-ci.sh after git push

# Read hook input from stdin
input=$(cat)

# Check if this was a Bash tool call
tool_name=$(echo "$input" | jq -r '.tool_name // empty')
if [[ "$tool_name" != "Bash" ]]; then
  exit 0
fi

# Check if the command was a git push
command=$(echo "$input" | jq -r '.tool_input.command // empty')
if [[ ! "$command" =~ git[[:space:]]+push ]] && [[ ! "$command" =~ git\ push ]]; then
  exit 0
fi

# Check if push succeeded (exit code 0)
exit_code=$(echo "$input" | jq -r '.tool_result.exit_code // .tool_result.exitCode // empty')
if [[ "$exit_code" != "0" ]] && [[ -n "$exit_code" ]]; then
  exit 0
fi

# Check if repo has GitHub Actions workflows
workflows_dir="$(git rev-parse --show-toplevel 2>/dev/null)/.github/workflows"
if [[ ! -d "$workflows_dir" ]] || [[ -z "$(ls -A "$workflows_dir" 2>/dev/null)" ]]; then
  exit 0
fi

# Instruct Claude to start CI monitoring
cat << 'EOF'
{
  "systemMessage": "Push completed. Run `watch-ci.sh` as a background task to monitor CI. Do NOT check on it or wait for it - continue with other work. You will automatically receive the result when all workflows complete."
}
EOF
