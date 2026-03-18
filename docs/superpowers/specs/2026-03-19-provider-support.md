# sandbox-cli — Provider Support

Add a `--provider` flag to `sandbox start` so that Codex CLI can be used as an alternative to Claude Code CLI for background tasks.

## Problem

sandbox-cli is hardcoded to Claude Code CLI. Users who want to dispatch background tasks using OpenAI's Codex CLI (e.g. to use their ChatGPT subscription) have no way to do so. The CLI needs a provider abstraction that lets the user choose which agent CLI runs inside the Docker container.

## Scope

- Background task mode only (`--task` / `--task-file`). Interactive mode stays Claude-only.
- Two providers: `claude` (default) and `codex`.
- No changes to `rm`, `ls`, `ports`, `post-exit`, or interactive mode.
- No new auth commands. Codex auth is handled by the user running `codex login` on the host; sandbox-cli mounts the resulting `~/.codex` directory.
- No persistent provider config. Provider is selected per-sandbox via `--provider` flag.

## CLI Interface

```
sandbox start <name> --task "prompt" --provider codex
sandbox start <name> --task "prompt" --provider codex --model gpt-4.1
sandbox start <name> --task "prompt"                    # defaults to claude
```

`--provider` accepts `claude` or `codex`. Default is `claude`. Only valid with `--task` or `--task-file`. Using `--provider codex` without `--task`/`--task-file` exits with an error: `"Codex provider only supports background task mode"`.

## Provider Abstraction

A `get_provider(name)` function returns a dict of callables that encapsulate provider-specific behavior:

| Key | Signature | Purpose |
|-----|-----------|---------|
| `build_cmd` | `(task, model, worktree_path)` | Returns the CLI command list for a new background task |
| `build_resume_cmd` | `(model, worktree_path)` | Returns the CLI command for resuming a session (or raises error if unsupported) |
| `env_vars` | `()` | Returns list of `"KEY=VALUE"` strings. Caller adds `-e` prefixes. |
| `volume_mounts` | `(home)` | Returns list of `"src:dest[:opts]"` strings. Caller adds `-v` prefixes. |
| `extract_response` | `(worktree_path, log_path)` | Extracts the final response text |
| `auth_check` | `()` | Returns `None` if auth is configured, error string if not |

The caller (`run_sandbox_background`) is responsible for provider-agnostic mounts and env vars:
- `-v {worktree_path}:{worktree_path}` (worktree)
- `-v {main_git}:{main_git}` (git objects)

Provider-specific mounts (auth dirs, package stores) come from `volume_mounts()`. This separation keeps the provider dict focused on what differs between providers.

This is not a class hierarchy. It is a dict of functions, kept in `__init__.py`. Two providers do not warrant a plugin system.

### Claude Provider

```python
{
    "name": "claude",
    "build_cmd": lambda task, model, worktree_path: [
        "claude", "-p", task, "--print", "--verbose",
        "--output-format", "stream-json", "--dangerously-skip-permissions",
        *(["--model", model] if model else []),
    ],
    "build_resume_cmd": lambda model, worktree_path: [
        "claude", "--continue", "--print", "--verbose",
        "--output-format", "stream-json", "--dangerously-skip-permissions",
        *(["--model", model] if model else []),
    ],
    "env_vars": lambda: [
        f"CLAUDE_CODE_OAUTH_TOKEN={get_auth_token()}",
        f"GH_TOKEN={get_gh_token()}",
    ],
    "volume_mounts": lambda home: [
        f"{home}/.ssh:/home/agent/.ssh:ro",
        f"{home}/.config/gh:/home/agent/.config/gh:ro",
        "pnpm-store:/pnpm-store",
    ],
    "extract_response": lambda worktree_path, log_path: extract_response(log_path),
    "auth_check": lambda: None if get_auth_token() else
        "No auth token configured. Run: claude setup-token && sandbox auth <token>",
}
```

### Codex Provider

```python
{
    "name": "codex",
    "build_cmd": lambda task, model, worktree_path: [
        "codex", "exec", "--yolo",
        "-o", str(worktree_path / ".sandbox-result.txt"),
        *(["--model", model] if model else []),
        task,
    ],
    "build_resume_cmd": lambda model, worktree_path: (_ for _ in ()).throw(
        click.UsageError("Codex provider does not support --continue")
    ),
    "env_vars": lambda: [
        "CODEX_HOME=/home/agent/.codex",
        f"GH_TOKEN={get_gh_token()}",
    ],
    "volume_mounts": lambda home: [
        f"{home}/.codex:/home/agent/.codex",
        f"{home}/.ssh:/home/agent/.ssh:ro",
        f"{home}/.config/gh:/home/agent/.config/gh:ro",
        "pnpm-store:/pnpm-store",
    ],
    "extract_response": extract_codex_response,
    "auth_check": lambda: None if (Path.home() / ".codex" / "auth.json").exists() else
        "No Codex auth found. Run: codex login",
}
```

The `-o` flag uses the absolute worktree path (`str(worktree_path / ".sandbox-result.txt")`) because the worktree is identity-mounted into the container (`-v {path}:{path}`), so the host path equals the container path. This is explicit and resilient to future mount changes.

`build_resume_cmd` raises `click.UsageError` for Codex because session resume behavior is undocumented and unreliable. `--continue` with `--provider codex` is not supported initially.

When `--continue` is used, the provider is always read from the state file, not the `--provider` CLI flag. If `--provider` is also passed alongside `--continue`, it is ignored with a warning to stderr. This prevents mismatches between the provider that created the sandbox and the one used to resume it.

## Codex Response Extraction

`extract_codex_response(worktree_path, log_path)`:

1. Check if `<worktree_path>/.sandbox-result.txt` exists. If so, read and return its contents.
2. Fall back to reading `log_path` (raw `docker logs` output). Return the last non-empty line that is not JSON (Codex prints the final answer as plain text to stdout when not using `--json`).
3. Return `None` if neither source has content.

The `-o` file is the primary extraction mechanism. The fallback is best-effort and may pick up log noise; it exists only as a safety net if `-o` fails to write.

The `.sandbox-result.txt` file must be excluded from git commits. Add it to the `git reset HEAD --` step in `_collect_and_finalize()` alongside `.env*`.

## State File

Add a `"provider"` field to the state file written at task launch:

```json
{
  "status": "running",
  "provider": "codex",
  "container": "sandbox-myproject-proto-1",
  "name": "proto-1",
  "branch": "proto-1",
  "worktreePath": "...",
  "baseCommit": "..."
}
```

The `read` command loads the provider from the state file and passes the provider dict to `_collect_and_finalize()` for correct response extraction. If the field is absent (pre-existing state files), default to `"claude"`.

The provider field is preserved in the final result JSON so callers know which provider was used.

## Dockerfile

Install Codex CLI alongside Claude Code CLI:

```dockerfile
RUN npm install -g @anthropic-ai/claude-code@latest @openai/codex@latest pnpm
```

Both CLIs are npm packages. A single image avoids managing multiple tags. Users who only use one provider pay a modest image size cost (~50MB for the unused CLI). This is an acceptable tradeoff for two providers; if more are added, separate image tags may be warranted.

## Auth

Codex auth uses `~/.codex/auth.json` created by `codex login` on the host. sandbox-cli mounts `~/.codex` into the container at `/home/agent/.codex` (rw) and sets `CODEX_HOME=/home/agent/.codex`.

The mount is read-write because Codex may refresh tokens in-place. This is analogous to how Claude's `~/.claude` is mounted rw for interactive sessions. Note: a rw auth mount is a security surface if the container is compromised. This is accepted because the container is already trusted with code execution and git push credentials.

No changes to `sandbox auth`. The auth validation for Codex checks for `~/.codex/auth.json` existence and returns a clear error if missing.

## Changes to Existing Code

### Modify

- **`start` command** — Add `--provider` option (type `click.Choice(["claude", "codex"])`, default `"claude"`). Validate that `--provider codex` requires `--task` or `--task-file`. Pass provider name to `run_sandbox_background()`.
- **`run_sandbox_background()`** — Accept `provider` parameter (string name). Call `get_provider(name)` to get provider dict. Call `provider["auth_check"]()` **before worktree creation** to fail fast without needing cleanup. Replace hardcoded `claude_cmd` with `provider["build_cmd"](task, model, worktree_path)`. Replace hardcoded env vars: caller iterates `provider["env_vars"]()` adding `-e` prefix to each. Replace hardcoded volume mounts: caller iterates `provider["volume_mounts"](home)` adding `-v` prefix to each. Provider-agnostic mounts (worktree, main_git) stay in the caller. On resume path (structural change — currently does not read state file): load state file, extract `provider` field, call `get_provider()`, use `provider["build_resume_cmd"](model, worktree_path)`. If `--provider` CLI flag was also passed, ignore it and warn to stderr. Write `"provider"` to state file for new tasks.
- **`_collect_and_finalize()`** — Accept provider dict as parameter. Call `provider["extract_response"](worktree_path, log_path)` instead of `extract_response(log_path)`. Add `.sandbox-result.txt` to `git reset HEAD --` step (harmless no-op for Claude). Replace hardcoded `"Claude exited with code {exit_code}"` with `f"{provider['name']} exited with code {exit_code}"`.
- **`read` command** — Load provider name from state file (`state.get("provider", "claude")`), call `get_provider(name)`, pass provider dict to `_collect_and_finalize()`.
- **`Dockerfile`** — Add `@openai/codex@latest` to the npm install line.

### Add

- **`get_provider(name)`** — Returns provider config dict. Raises `click.UsageError` for unknown providers.
- **`extract_codex_response(worktree_path, log_path)`** — Reads `.sandbox-result.txt` from worktree, falls back to docker logs.

### Keep as-is

- `rm`, `ls`, `ports`, `post-exit` commands.
- `run_sandbox()` (interactive mode).
- `sandbox auth` command.
- `extract_response()` (Claude NDJSON parser, now called via provider dict wrapper).
- All git/worktree/container utility functions.

## Not Included

- Interactive mode for Codex.
- Auth management for Codex (`sandbox auth` changes).
- Codex NDJSON parsing.
- Persistent provider config.
- Additional providers beyond claude and codex.
- `--continue` support for Codex (errors with clear message).
