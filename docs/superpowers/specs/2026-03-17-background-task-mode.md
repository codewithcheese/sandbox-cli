# sandbox-cli — Background Task Mode

Add background task execution to sandbox-cli so a parent Claude Code session can programmatically dispatch work to isolated Docker sandboxes and collect results.

## Problem

sandbox-cli currently only supports interactive sessions. A parent Claude session running a multi-prototype plan has no way to dispatch parallel sandboxed tasks, wait for completion, and collect results. The CLI needs a non-interactive execution path that follows the same blocking-with-notification pattern as `auto-chat chatgpt submit`.

## Naming Model

A sandbox name is the primary key for all operations. From the user-supplied `name`:

- **`sname`** = `safe_name(name)` — replaces `/` with `-` for filesystem/docker safety.
- **Container** = `sandbox-{repo}-{sname}` — includes repo name for scoping `ls` and `rm --all`.
- **Worktree** = `~/.config/sandbox-cli/worktrees/{repo}__{sname}` — centralized, not in the project parent directory.
- **Branch** = `name` (original, unsanitized) — the actual git branch name.
- **Log files** = `~/.config/sandbox-cli/logs/sandbox-{repo}-{sname}.json` and `.log`.

A name is **permanently reserved** once used. Any of these artifacts existing (container, branch, worktree, or log file) constitutes a conflict. Only `sandbox rm` frees a name for reuse.

## CLI Interface

### Background mode (new)

```
sandbox start <name> --task "prompt"
sandbox start <name> --task-file prompt.txt
sandbox start <name> --task "prompt" --model sonnet
```

When `--task` or `--task-file` is present, `start` takes the background path: launches a detached container, blocks until Claude finishes, auto-commits and pushes on success, saves logs, cleans up, and returns JSON to stdout.

`--task` and `--task-file` are mutually exclusive. `--task-file` reads the prompt from a file (avoids shell argument length limits for large prompts).

`--model` is passed through to Claude as `--model <value>`.

### Output JSON

Success:

```json
{
  "container": "sandbox-myproject-proto-1",
  "name": "proto-1",
  "branch": "proto-1",
  "exitCode": 0,
  "response": "Built prototype 1 with data model, serialization, and 24 passing tests.",
  "diffStats": {
    "filesChanged": 8,
    "insertions": 482,
    "deletions": 0
  },
  "commitSha": "abc1234"
}
```

Failure (Claude exited non-zero but commit succeeded):

```json
{
  "container": "sandbox-myproject-proto-1",
  "name": "proto-1",
  "branch": "proto-1",
  "exitCode": 1,
  "error": "Claude exited with code 1",
  "response": "...",
  "diffStats": { "filesChanged": 3, "insertions": 120, "deletions": 5 },
  "commitSha": "def5678"
}
```

Failure (commit failed, worktree preserved):

```json
{
  "container": "sandbox-myproject-proto-1",
  "name": "proto-1",
  "branch": "proto-1",
  "exitCode": 1,
  "error": "Commit failed, worktree preserved at /path/to/repo__proto-1",
  "worktreePath": "/path/to/repo__proto-1"
}
```

The `container`, `name`, and `branch` fields are always present. On success, `commitSha` and `diffStats` are included. The `error` field is absent on success, present as a string on failure. When the worktree could not be committed, `worktreePath` is included so the caller can recover manually, and the worktree is NOT cleaned up.

Note: `branch` contains the original unsanitized name (e.g. `feature/auth`), while `container` and `name` use the safe name (e.g. `feature-auth`).

### Re-reading output

```
sandbox read <name>
```

Takes the sandbox name (same as used with `start`). Requires repo context. Derives the container name internally as `sandbox-{repo}-{sname}`.

Resolution order:
1. Check `~/.config/sandbox-cli/logs/sandbox-{repo}-{sname}.json`:
   - If it contains a `status: "running"` state file — the task is in flight. Check the container (running or exited) and recover (same as steps 7–14 of the background lifecycle).
   - If it contains a completed result (no `status` field) — return it immediately.
2. If no log file but container exists (running or exited):
   - If running — `docker wait`, then collect output (steps 8–14).
   - If exited — collect output directly (skip wait, proceed with steps 8–14).
3. If no log file and no container — return `{ "error": "Sandbox not found" }`.

Returns the same JSON shape as `start --task`.

### Modified commands

```
sandbox rm <name>              # remove all artifacts for a sandbox
sandbox rm --all               # remove all sandboxes for current repo (replaces purge)
sandbox rm <name> --force      # remove even if task is running
```

`rm <name>` removes all local artifacts for a sandbox identity:
- Docker container
- Git worktree
- Log/state files (`~/.config/sandbox-cli/logs/sandbox-{repo}-{sname}.json` and `.log`)
- Optionally the git branch (prompts interactively)

If the state file indicates the task is still running (`status: "running"` and container is active), `rm` requires `--force`. Without it, `rm` exits with an error to prevent accidentally destroying an in-flight task.

`rm --all` replaces the `purge` command. Removes all containers, worktrees, and log files for the current repo.

### Existing commands (unchanged)

```
sandbox start <name>          # interactive mode (existing behavior)
sandbox ls                    # list worktrees + containers
sandbox post-exit <name>      # interactive cleanup prompt (existing)
sandbox ports <name>          # show sandbox ports (existing)
```

## Background Mode Lifecycle

1. **Resolve repo** — `git rev-parse` to find repo root and repo name.
2. **Build image** — acquire file lock at `~/.config/sandbox-cli/build.lock`. Check for `Dockerfile.sandbox` in repo root; build custom image if present, otherwise build/reuse default image. Release lock. File lock prevents races when multiple `start --task` calls run in parallel. All build progress output goes to stderr (not stdout) to avoid contaminating JSON output.
3. **Check for conflicts** — exit with error if ANY of these exist: container `sandbox-{repo}-{sname}`, branch `{name}`, worktree path, or log file `sandbox-{repo}-{sname}.json`. The caller should choose a unique name or `sandbox rm` first. No implicit removal of existing resources.
4. **Write state file** — write `{"status": "running", "container": "sandbox-{repo}-{sname}", "name": "{sname}", "branch": "{name}", "worktreePath": "...", "baseCommit": "..."}` to `~/.config/sandbox-cli/logs/sandbox-{repo}-{sname}.json`. This reserves the name and provides recovery metadata for `read`.
5. **Create worktree** — `git worktree add ~/.config/sandbox-cli/worktrees/{repo}__{sname} -b {name}`. Record the HEAD commit as `baseCommit` for stable diff stats.
6. **Copy .env files** — from repo root to worktree.
7. **Launch container detached** — `docker run -d --name sandbox-{repo}-{sname}` with volume mounts:
   - Worktree (rw)
   - `.git` dir (rw)
   - `~/.claude` → `/home/agent/.claude` (rw) — Claude CLI auth/config
   - `~/.ssh` (ro)
   - `~/.config/gh` (ro)
   - `pnpm-store` volume

   Environment variables: `GH_TOKEN`, `CLAUDE_CONFIG_DIR=/home/agent/.claude`.

   CMD override: `claude -p "prompt" --print --output-format json --dangerously-skip-permissions` plus `--model` if specified. No `-it`, no port mappings, no system prompt append.
8. **Wait** — `docker wait <container>` blocks until the container exits. Parse exit code with error handling (default to non-zero on parse failure).
9. **Save raw logs** — `docker logs <container>` captures Claude's JSON output. Write to `~/.config/sandbox-cli/logs/sandbox-{repo}-{sname}.log`. Create the logs directory if it doesn't exist.
10. **Commit** — `git -C <worktree> add -A` then check `git status --porcelain`. If there are changes, `git commit -m "sandbox: {sname}"`. Exclude `.env*` files from the commit to avoid accidentally pushing secrets. Skip if nothing to commit.
11. **Push (success only)** — if Claude exited 0 and commit succeeded, `git push -u origin {name}`. If push fails, log the error but continue. Do NOT push on non-zero Claude exit code.
12. **Cleanup** — `docker rm <container>`. If commit succeeded (or there was nothing to commit): `git worktree remove --force <worktree>`. If commit failed: preserve the worktree and include `worktreePath` in the output JSON so the caller can recover.
13. **Extract response + build result** — parse the saved log file to find the last `type: "result"` object and extract its text content. Compute diff stats from `git diff --numstat <baseCommit> {name}`. Build the result JSON object.
14. **Save result** — overwrite the state file at `~/.config/sandbox-cli/logs/sandbox-{repo}-{sname}.json` with the final result JSON (no `status` field). This transitions the file from "running state" to "completed result."
15. **Return JSON** — print result object to stdout. All progress/status output goes to stderr; only the final JSON goes to stdout.

### Response extraction

Claude's `--output-format json` emits newline-delimited JSON objects, one per event. The response extractor:

1. Reads the raw log file line by line, skipping non-JSON lines (stderr may be mixed in from `docker logs`).
2. Finds the last object with `type: "result"` and extracts its text content.
3. Falls back to the last object with an assistant message text if no `type: "result"` is found.
4. Returns `null` if no summary could be extracted.

### Diff stats

Diff stats are computed by running `git diff --numstat <base-commit> <branch>` where `<base-commit>` is the commit SHA recorded at worktree creation time (step 5). This ensures stable stats regardless of whether the main branch has advanced. The result is structured:

```json
{
  "filesChanged": 8,
  "insertions": 482,
  "deletions": 0
}
```

## Changes to Existing Code

### Remove

- `.claude/hooks/on-stop.sh` — experimental commit reminder hook. These hooks added friction in practice (the commit reminder was too aggressive, the CI watcher triggered unnecessarily).
- `.claude/hooks/post-push.sh` — experimental CI watcher hook.
- `.claude/settings.json` — hooks configuration. No longer needed with hooks removed.
- `purge` command — replaced by `rm --all`.
- Remove `--settings /opt/sandbox-claude/settings.json` from the interactive Claude command in `run_sandbox()`. Without hooks, no custom settings are required.
- Remove the `/opt/sandbox-claude:ro` volume mount from interactive container launches. This mount only existed to make the hooks/settings available inside the container. The `.claude/agents/dockerfile-sandbox.md` agent is a host-side Claude Code prompt, not a container-side asset, so it is unaffected.

### Keep as-is

- `ls`, `ports`, `post-exit` commands.
- All utility functions: worktree management, container management, image building, env file copying.
- `Dockerfile` and `.claude/agents/dockerfile-sandbox.md`.
- `scripts/watch-ci.sh` (useful standalone, not tied to hooks).

### Modify

- **Container naming** — keep `sandbox-{repo}-{sname}` scheme. All commands require repo context for consistent scoping.
- **`start` command** — add `--task`, `--task-file`, `--model` flags. When `--task` or `--task-file` is present, take the background path instead of outputting `__SANDBOX_EXEC__` shell wrapper directives.
- **`run_sandbox()`** — update container naming to drop repo prefix. Split into two paths: interactive (existing `__SANDBOX_EXEC__` output) and background (detached docker, wait, commit+push, save logs, cleanup, return JSON).
- **`rm` command** — also removes log/state files. Add `--all` flag to replace `purge`. Add `--force` flag to allow removing in-flight tasks. Update container naming.
- **`build_template_if_exists()`** — add file locking before Docker build.
- **`ensure_default_image()`** — add file locking before Docker build. Fix `click.echo()` calls to use `err=True` to avoid contaminating stdout in background mode.

### Add

- **`read` command** — takes sandbox name, resolves to log file or running/exited container for recovery. Performs full lifecycle recovery (commit, push, cleanup) for exited containers.
- **State file** — written at task launch with `status: "running"` and recovery metadata (`container`, `name`, `branch`, `worktreePath`, `baseCommit`). Overwritten with final result on completion. Acts as name reservation.
- **Log storage** — `~/.config/sandbox-cli/logs/` directory. Two files per run: `sandbox-{repo}-{sname}.log` (raw Claude NDJSON output) and `sandbox-{repo}-{sname}.json` (state file → final result JSON).
- **Response extraction** — parse Claude's `--output-format json` output with fallback chain.
- **File locking utility** — acquire/release a file lock for image builds.
- **Base commit tracking** — record the HEAD commit at worktree creation for stable diff stats.

## Intended Usage Pattern

A parent Claude Code session dispatches multiple sandboxed tasks in parallel using `run_in_background`:

```python
# Parent Claude runs these via Bash tool with run_in_background:
sandbox start proto-1 --task-file prompts/proto-1.txt --model sonnet
sandbox start proto-2 --task-file prompts/proto-2.txt --model sonnet
sandbox start proto-3 --task-file prompts/proto-3.txt --model sonnet
```

Each command blocks until its sandbox finishes. The parent receives notifications as each completes, with the JSON result containing branch name, summary, and diff stats. If a command times out, the parent can `sandbox read <name>` to reconnect.

## Platform

macOS and Linux only (uses `fcntl` for file locking).

## Dependencies

- Python: `click` (existing), `fcntl` (stdlib, for file locking)
- Docker
- Git
- Claude Code CLI (installed in the container image)
