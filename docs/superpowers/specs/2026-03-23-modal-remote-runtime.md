# sandbox-cli — Modal Remote Runtime

Add a `--remote` flag to `sandbox start` so that background tasks can run on Modal instead of local Docker.

> **Research reference:** See [modal-remote-runtime-research.md](./2026-03-23-modal-remote-runtime-research.md) for detailed Modal API findings, code examples, and constraint analysis that informed this design.

## Problem

sandbox-cli currently requires a local Docker daemon to run background tasks. This means:
- Local CPU/memory is consumed by the sandbox container
- Running many parallel tasks is limited by local resources
- Environments without Docker (CI, cloud dev, lightweight laptops) can't use sandbox-cli

Modal provides serverless sandboxes with managed compute, eliminating the local Docker requirement for background tasks.

## Scope

- Background task mode only (`--task` / `--task-file`). Interactive mode stays Docker-only.
- All existing providers supported: `claude` (default), `codex`, `gemini`.
- No changes to `sandbox ls`, `sandbox rm`, `sandbox ports`, `sandbox post-exit`, or interactive mode.
- No changes to `sandbox auth`.
- No runtime abstraction layer — parallel code path alongside existing Docker logic, structured for future extraction.
- `modal` is an optional dependency, only imported when `--remote` is used.

## CLI Interface

```
sandbox start --task "prompt" --remote                          # Claude on Modal
sandbox start --task "prompt" --remote --provider gemini        # Gemini on Modal
sandbox start --task "prompt" --remote --model sonnet           # with model override
sandbox start --task "prompt"                                   # Docker (unchanged)
```

`--remote` is only valid with `--task` or `--task-file`. Using `--remote` without a task exits with an error: `"--remote only supports background task mode"`.

`--remote` is incompatible with `--continue` (no resume for remote sandboxes).

`--remote` makes push implicit — results are always pushed from inside the sandbox. The `--push` flag is ignored. This is a behavior difference from Docker, where push is opt-in. The `--cleanup` flag is also ignored (no local worktree to clean up).

## No Local Worktree

Unlike the Docker path, `--remote` does not create a local git worktree. The sandbox clones from the remote git origin at cloud network speeds, which is faster than uploading a local worktree.

This means:
- No local worktree directory
- No local branch created
- `sandbox ls` and `sandbox rm` are unaffected (Docker-only)
- Results are retrieved via `sandbox read` from the local state file

**Known limitation:** `sandbox rm` does not clean up remote branches created by `--remote` sandboxes. Remote branches must be deleted manually or via `git push origin --delete <branch>`. This may be addressed in a future version.

## Auth & Prerequisites

### Modal setup

The user must have the `modal` Python package installed and be authenticated (`modal token set` or `modal setup`). If `--remote` is used but Modal is not available, exit with an error and install instructions.

### Secrets

All secrets are injected as inline Modal secrets (`Secret.from_dict()`), read from the user's local environment at launch time:

| Secret | Source | Required by |
|--------|--------|-------------|
| `CLAUDE_CODE_OAUTH_TOKEN` | `~/.config/sandbox-cli/auth_token` | Claude provider |
| `GH_TOKEN` | `$GH_TOKEN` env var, or `gh auth token` fallback | All providers (git clone/push) |
| `GEMINI_API_KEY` | `$GEMINI_API_KEY` env var | Gemini provider |

No persistent Modal Secrets are created. No changes to `sandbox auth`.

### GH_TOKEN resolution

Update the existing `get_gh_token()` function to check `$GH_TOKEN` env var first, then fall back to `gh auth token`. This benefits both Docker and Modal paths. The auth check must validate the resolved token is non-empty — `GH_TOKEN` is required for `--remote` (the sandbox needs it for git clone/push).

### Auth check flow

1. Check `modal` package is importable
2. Check Modal is authenticated (attempt `App.lookup`)
3. Check provider auth (same as Docker path — auth_token for Claude, env vars for others)
4. Check `GH_TOKEN` is resolvable and non-empty (env var first, then `gh auth token`)
5. Fail fast with clear messages if anything is missing

## Execution Flow — `run_sandbox_remote()`

A new function, parallel to `run_sandbox_background()`. This function has its own finalization logic — it does NOT reuse `_collect_and_finalize()`, which is Docker-specific (relies on `docker logs`, local worktree for git operations, etc.).

1. **Validate** — `--remote` requires `--task`, check Modal, check provider auth, check GH_TOKEN
2. **Resolve names** — generate sandbox name, branch name, state file path (same naming, no worktree path)
3. **Create remote branch** — `git push origin HEAD:refs/heads/{branch}`. **Must succeed before proceeding.** If push fails (no access, remote down, branch already exists), exit with error before creating any sandbox. Check for existing remote branch first and fail if it exists.
4. **Write state file** — `{"status": "running", "runtime": "modal", "provider": "claude", "branch": "...", "sandboxId": null, "baseCommit": "..."}`
5. **Create Modal sandbox** — `Sandbox.create()` with image, inline secrets, resource config
6. **Update state file** with `sandboxId`
7. **Exec runner script** — single bash process inside sandbox, with `pty=False` (print mode doesn't need PTY; see PTY section below)
8. **Stream stdout** — tee to terminal, capture in memory, and **write incrementally to `log_raw` file** (so partial output survives if the CLI process crashes)
9. **Collect results** — parse `__SANDBOX_RESULT__` marker from captured output. Call provider's `extract_response(worktree_path, log_raw_path)` on the saved `log_raw` file (same interface as Docker path — the log file contains the full agent output)
10. **Terminate sandbox**
11. **Write final state file** — same result dict shape as Docker path, including `"runtime": "modal"`

### Error handling

`run_sandbox_remote()` must catch Modal-specific exceptions and convert them to the standard error result dict format (matching Docker path behavior). All error paths must call `sb.terminate()` and `sb.detach()` in a finally block to avoid orphaned sandboxes.

| Exception | Meaning | Handling |
|-----------|---------|----------|
| `SandboxTimeoutError` | Sandbox exceeded `timeout` | Write error result, terminate |
| `SandboxTerminatedError` | Internal termination (OOM, infra) | Write error result, terminate |
| `ExecTimeoutError` | Runner process exceeded timeout | Write error result, terminate |
| `NotFoundError` | Sandbox ID no longer valid (in `sandbox read`) | Report sandbox expired |

### PTY vs stream-json interaction

Research (Q7) found that `pty=True` is NOT required for `claude -p --output-format stream-json` — this is a headless print mode. Modal's example uses `pty=True` for interactive Claude, not print mode. PTY mode can cause Claude Code (a TUI app) to emit terminal control output that corrupts NDJSON parsing.

**Approach:**
1. **Default to `pty=False`** for the runner script exec. This gives clean NDJSON output.
2. **Guard during parsing**: skip blank lines, log lines that don't start with `{` as protocol noise.
3. **Fallback**: if Claude hangs without PTY (version-dependent), retry with `pty=True` and strip ANSI escape codes before JSON parsing (`re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', line)`).

### Provider `build_cmd` on Modal

Provider `build_cmd(task, model, worktree_path)` is called with `worktree_path` set to `Path("/workspace")` — the in-sandbox working directory. This matters for the Codex provider which uses `worktree_path` for its `-o` output flag. The runner script clones the repo to `/workspace`, so this path is correct inside the sandbox.

### Sandbox configuration

```python
modal.Sandbox.create(
    app=app,
    image=image,
    timeout=7200,        # 2 hours hard max
    idle_timeout=600,    # 10 min after process exits
    cpu=2.0,
    memory=4096,         # MiB
    secrets=[Secret.from_dict({...})],
)
```

`timeout` is generous because Claude can run autonomously for 30+ minutes. `idle_timeout` is the safety net — it only triggers after the agent process exits, since a running process counts as "activity."

## Modal Image

Defined as a `modal.Image` chain in a dedicated function (e.g. `get_modal_image()`), similar to but lighter than the Docker image:

```python
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ca-certificates", "curl", "git", "jq", "build-essential")
    .run_commands(
        "curl -fsSL https://claude.ai/install.sh | bash",
        "npm install -g @openai/codex @google/gemini-cli",
    )
)
```

Differences from Docker image:
- No GitHub CLI (git auth via GIT_ASKPASS)
- No Playwright/browsers (background tasks don't need them)
- Modal caches unchanged image definitions automatically

No support for `Dockerfile.sandbox` templates on Modal initially.

## Runner Script — `sandbox_cli/scripts/modal_runner.sh`

A bash script exec'd inside the sandbox. Single responsibility: clone, run agent, push results.

### Inputs (exec args)

- `REPO_URL` — git remote URL
- `BRANCH` — branch to clone and push to
- `PROVIDER_CMD` — full agent command (from provider's `build_cmd`)

The task prompt is written into the sandbox via a preliminary `exec` call before the runner starts.

### Input validation

The script validates that `REPO_URL`, `BRANCH`, `PROVIDER_CMD`, and `GH_TOKEN` are all non-empty at the top, failing with a clear error message rather than producing cryptic downstream failures.

### Flow

```
1. Validate inputs (REPO_URL, BRANCH, PROVIDER_CMD, GH_TOKEN non-empty)
2. Configure GIT_ASKPASS from $GH_TOKEN
3. git clone --branch $BRANCH --single-branch $REPO_URL /workspace
4. cd /workspace
5. Run $PROVIDER_CMD
6. Capture agent exit code
7. git add -A && git reset HEAD -- .env*
8. If changes: git commit -m "sandbox: $BRANCH"
9. If commit succeeded: git push origin HEAD:$BRANCH
10. Print __SANDBOX_RESULT__ JSON to stdout
11. Exit with agent's exit code
```

### Failure handling

- `trap` ensures result JSON is always printed, even on failure
- If agent fails but made partial commits, those still get pushed
- If git push fails, result JSON includes `"pushed": false` and an error field

### Result JSON

Final stdout line, prefixed with `__SANDBOX_RESULT__`:

```json
{"exitCode": 0, "commitSha": "abc123", "modifiedFiles": ["..."], "diffStats": {"filesChanged": 2, "insertions": 45, "deletions": 3}, "pushed": true}
```

`"pushed": false` occurs when git push fails (e.g. remote rejected, network error). The agent's work is lost in this case (known v1 limitation).

## Result Collection & `sandbox read`

### During execution

Stdout is streamed to terminal and captured. The captured output is written incrementally to the `log_raw` file at `~/.config/sandbox-cli/logs/`. On completion:
1. Parse the `__SANDBOX_RESULT__` marker line from captured output
2. Call provider's `extract_response(worktree_path, log_raw_path)` on the saved log file
3. Write final state file

### State file format

Same shape as Docker, with `runtime` field added:

```json
{
  "name": "feature-foo",
  "branch": "feature-foo",
  "provider": "claude",
  "runtime": "modal",
  "exitCode": 0,
  "commitSha": "abc123",
  "modifiedFiles": ["src/app.py"],
  "diffStats": {"filesChanged": 1, "insertions": 20, "deletions": 5},
  "pushed": true,
  "response": "I implemented..."
}
```

### `sandbox read` with Modal

`sandbox read` checks for `data.get("runtime") == "modal"` in the state file:
- If `runtime` is `"modal"` and status is `"running"`: reconnect via `Sandbox.from_id(data["sandboxId"])`, wait for completion, read remaining stdout via `sb.stdout.read()`, append to `log_raw`, then finalize.
- If `runtime` is `"modal"` and already completed: return stored result.
- If `runtime` is missing or `"docker"`: existing Docker behavior (unchanged).

**Reconnection limitation:** `sb.stdout.read()` only returns output produced since the last read, not a full replay. If the original CLI process streamed most of the output before crashing, the reconnection only gets the tail. However, since `log_raw` is written incrementally during streaming, the file contains all output up to the crash point. The reconnection appends any remaining output and proceeds with finalization. This means the combined `log_raw` should contain the complete output in most cases.

## What Changes, What Doesn't

### New code
- `run_sandbox_remote()` — Modal execution path (does NOT reuse `_collect_and_finalize()`)
- `sandbox_cli/scripts/modal_runner.sh` — in-sandbox runner
- `get_modal_image()` — Modal image definition function
- `--remote` flag on `sandbox start`
- Auth checks for Modal + GH_TOKEN
- Update HELP_TEXT with `--remote` documentation

### Modified code
- `sandbox start` command — route to `run_sandbox_remote()` when `--remote`
- `sandbox read` — handle `runtime: "modal"` state files (reconnect via `Sandbox.from_id`)
- `get_gh_token()` — check `$GH_TOKEN` env var first, then fall back to `gh auth token`

### Untouched
- All Docker code paths (except `get_gh_token()` improvement which benefits both)
- `sandbox ls`, `sandbox rm`, `sandbox ports`, `sandbox post-exit`
- `sandbox auth`
- Git worktree logic
- Dockerfile / image building
- Interactive mode
- Provider dicts (no changes — `build_cmd` is called with `/workspace` as worktree_path for Modal)

### New dependency
- `modal` — optional. Only imported when `--remote` is used. Lazy import with clear error if missing.
