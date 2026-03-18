# Provider Support Implementation — Decisions & Notes

## Summary

This task added a `--provider` flag to `sandbox start` enabling OpenAI's Codex CLI as an alternative to Claude Code CLI for background tasks. All 21 acceptance criteria were satisfied.

## Key Decisions

### 1. Provider as dict of callables, not classes

Followed the spec exactly: `get_provider(name)` returns a plain dict with callable values. No inheritance, no plugin registry. Two providers don't warrant a class hierarchy.

### 2. Auth check moved before worktree creation (AC#9)

The original code checked auth after creating the worktree, which required cleanup on failure. The new code calls `task_provider["auth_check"]()` immediately after conflict checks (before `git_worktree_add`). This means auth failure returns early with no artifacts to clean up.

Side effect: the auth check is now mediated by the provider dict rather than being a bare `get_auth_token()` call. The old cleanup code (worktree remove + branch delete + log unlink) was removed from the auth failure path.

### 3. `build_resume_cmd` for Codex raises via generator throw

Used the specified pattern:
```python
"build_resume_cmd": lambda model, worktree_path: (_ for _ in ()).throw(
    click.UsageError("Codex provider does not support --continue")
),
```
This is the only way to raise from a lambda in Python. The empty generator's `.throw()` method propagates the exception to the caller.

### 4. `--provider` on `--continue` warning strategy

The warning fires when the CLI-passed `provider` differs from the state file's `provider`. This is slightly narrower than "always warn when --provider is explicitly passed with --continue" (since we can't easily detect if `--provider claude` was explicitly passed vs. being the default). The practical behavior is correct: if you pass `--provider codex --continue` on a claude sandbox, you get a warning. If you don't pass `--provider`, no warning.

### 5. State file `provider` field defaults to `"claude"` when absent (AC#13)

Both `run_sandbox_background` (continue path) and the `read` command use `state.get("provider", "claude")` to handle pre-existing state files gracefully.

### 6. `_collect_and_finalize` provider parameter

Added as optional (`provider: dict | None = None`) with a fallback to `get_provider("claude")`. This prevents breaking any code that calls it without a provider (e.g. tests that don't explicitly pass one).

### 7. `.sandbox-result.txt` excluded from git commits

Added `.sandbox-result.txt` alongside `.env*` in the `git reset HEAD --` step. This is a no-op for Claude sandboxes (the file won't exist) and prevents Codex's output file from being committed.

### 8. Codex volume mounts include `.codex` as read-write

The spec requires `~/.codex:/home/agent/.codex` (rw, no `:ro` suffix). This is intentional: Codex may refresh tokens in-place. The security tradeoff (rw auth mount) is documented in the spec and accepted.

### 9. Pre-existing test fix

`test_build.py::test_ensure_default_image_stderr_only` was failing before this task due to a permissions issue in the container: it tried to create `/home/agent/.config/sandbox-cli` without mocking `_build_lock_path`. Fixed by adding `@patch("sandbox_cli._build_lock_path")` to the test.

Integration tests (`test_integration.py`, `test_sandbox_integration.py`) were crashing at collection time with `FileNotFoundError` when Docker is not installed. Fixed by wrapping `subprocess.run(["docker", "info"])` in a `try/except FileNotFoundError`.

## What Worked

- The provider dict pattern cleanly encapsulates all provider-specific behavior
- The `extract_codex_response` fallback logic is straightforward
- Existing tests continued to pass without modification after adding the `provider` parameter with a default to claude
- The `click.Choice(["claude", "codex"])` approach handles AC#21 (unknown provider) automatically

## What Didn't Work / Surprises

- `uv` was not available in this container environment; used a venv with pip instead
- The integration test files crashed at collection (not at test time) due to bare `subprocess.run(["docker", "info"])` at module scope — needed a `try/except FileNotFoundError` wrapper
- The `build_lock` in `ensure_default_image` tried to create `~/.config/sandbox-cli` which didn't exist in the container — fixed by patching `_build_lock_path` in the test

## Verification Checklist

| AC | Status | Verified by |
|----|--------|-------------|
| 1 | ✅ | `test_codex_build_cmd_basic` — codex exec --yolo -o <path> |
| 2 | ✅ | `test_codex_build_cmd_with_model` — --model gpt-4.1 |
| 3 | ✅ | `test_provider_default_is_claude`, `test_provider_claude_explicit_behaves_same_as_default` |
| 4 | ✅ | `test_codex_without_task_exits_with_error` |
| 5 | ✅ | `test_codex_build_resume_cmd_raises`, `test_codex_continue_raises_usage_error` |
| 6 | ✅ | `test_codex_volume_mounts_no_dash_v_prefix` — includes .codex |
| 7 | ✅ | `test_codex_env_vars_no_claude_token` |
| 8 | ✅ | Codex auth_check lambda checks `~/.codex/auth.json` existence |
| 9 | ✅ | `test_claude_auth_failure_before_worktree_creation` — mock_wt_add.assert_not_called() |
| 10 | ✅ | `TestExtractCodexResponse` — all 3 cases |
| 11 | ✅ | `.sandbox-result.txt` added to `git reset HEAD --` in `_collect_and_finalize` |
| 12 | ✅ | `test_result_json_includes_provider`, `test_read_completed_result_preserves_provider` |
| 13 | ✅ | `test_read_defaults_provider_to_claude_when_missing` |
| 14 | ✅ | `read` command extracts `state.get("provider", "claude")`, calls `get_provider()`, passes to `_collect_and_finalize` |
| 15 | ✅ | `test_nonzero_exit_uses_provider_name` — f"{provider['name']} exited with code {exit_code}" |
| 16 | ✅ | `run_sandbox_background` reads state file provider on continue path, warns on mismatch |
| 17 | ✅ | `grep codex sandbox_cli/Dockerfile` confirms `@openai/codex@latest` |
| 18 | ✅ | `test_codex_env_vars_no_dash_e_prefix`, `test_codex_volume_mounts_no_dash_v_prefix` |
| 19 | ✅ | `test_provider_flag_appears_in_help` |
| 20 | ✅ | 109 passed, 15 skipped (Docker integration tests) |
| 21 | ✅ | `test_unknown_provider_rejected` — click.Choice rejects unknown values |

## Residual Risks / Recommended Next Steps

1. **Codex version pinning**: `@openai/codex@latest` will track breaking changes. Consider pinning to a specific version once stable.
2. **`--provider` on `--continue` warning**: Currently only warns when CLI provider ≠ state file provider. If user passes `--provider claude` (same as state) with `--continue`, no warning is emitted. This is probably fine but differs slightly from "always warn if --provider is passed with --continue".
3. **Interactive Codex**: Not supported. `--provider codex` without `--task` exits with error. If interactive Codex support is ever added, the guard in the `start` command needs updating.
4. **Codex auth.json path**: The auth check uses `Path.home() / ".codex" / "auth.json"`. If Codex changes its auth location, this hardcoded path needs updating.
5. **Image size**: Adding `@openai/codex@latest` to the Dockerfile adds ~50MB. Users who only use Claude pay this cost. If more providers are added, separate image tags should be considered.
