#!/bin/bash
# Sandbox stop hook - prompts to commit/push if there are uncommitted changes

# Check if we're in a git repo
if ! git rev-parse --git-dir > /dev/null 2>&1; then
  exit 0
fi

# Check if there are uncommitted changes (staged or unstaged)
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  exit 0  # No changes
fi

# Output JSON to prompt Claude to commit
cat << 'EOF'
{
  "continue": true,
  "systemMessage": "There are uncommitted changes in the repository. Please run `git status` to review, and if the code is ready, commit and push the changes."
}
EOF
