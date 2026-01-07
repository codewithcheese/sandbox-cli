#!/bin/bash
# Sandbox stop hook - prompts to commit/push if there are uncommitted changes

# Check if we're in a git repo
if ! git rev-parse --git-dir > /dev/null 2>&1; then
  exit 0
fi

# Check if there are uncommitted changes (staged or unstaged)
# Exclude .claude/ from untracked files (sandbox-managed)
untracked=$(git ls-files --others --exclude-standard | grep -v '^\.claude/')
if git diff --quiet && git diff --cached --quiet && [ -z "$untracked" ]; then
  exit 0  # No changes
fi

# Output a reminder (non-blocking) about uncommitted changes
cat << 'EOF'
{
  "decision": "allow",
  "systemMessage": "Reminder: There are uncommitted changes in the repository. If your work is complete, please commit and push before stopping."
}
EOF
