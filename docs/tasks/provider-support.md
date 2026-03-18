# Task: Implement provider support for Codex CLI

## Goal

Add a `--provider` flag to `sandbox start` so that background tasks can run using OpenAI's Codex CLI as an alternative to Claude Code CLI inside Docker containers.

## Source of truth

Read these first and treat them as the design source of truth for this task:

- `docs/superpowers/specs/2026-03-19-provider-support.md`
- `sandbox_cli/__init__.py`
- `sandbox_cli/Dockerfile`

Do not re-litigate the design described there unless the task explicitly says you may.

If the references conflict with the actual code, installed dependencies, or observed runtime behavior, trust reality and document the drift.

## Out of scope

- Interactive mode for Codex (`--provider codex` without `--task` must error)
- Changes to `run_sandbox()` (interactive mode) — no provider support there
- Changes to `sandbox auth` command
- Codex NDJSON parsing (use `-o` file output only)
- Persistent provider config or config files
- Additional providers beyond `claude` and `codex`
- `--continue` support for Codex (must error with clear message)

## Autonomy

Work fully autonomously inside the sandboxed container. Do not stop to ask for decisions or confirmation during normal implementation, debugging, testing, or cleanup work.

When you encounter ambiguity or a local decision point, decide yourself and keep going. Use the repository, the code, the installed dependencies, command output, and runtime behavior as your source of truth.

You may use destructive cleanup inside the container if it is useful to complete the task safely and efficiently.

Treat yourself as blocked only when progress requires an external dependency or resource that is genuinely unavailable from inside the container, such as missing credentials, unavailable external services, or required inputs that do not exist in the repo or environment.

Keep going until all acceptance criteria are satisfied or you hit a true blocker.

## Success standard

The task is complete only when the implementation, verification, and required output artifacts all satisfy the acceptance criteria below.

## Acceptance criteria

1. `sandbox start foo --task "hello" --provider codex` builds a docker command using `codex exec --yolo -o <path> "hello"` instead of `claude -p "hello" --print ...`
2. `sandbox start foo --task "hello" --provider codex --model gpt-4.1` includes `--model gpt-4.1` in the codex exec command
3. `sandbox start foo --task "hello" --provider claude` (and the default with no `--provider`) behaves identically to the current implementation
4. `sandbox start foo --provider codex` (no `--task`) exits with error: "Codex provider only supports background task mode"
5. `sandbox start foo --task "hello" --provider codex --continue` exits with error: "Codex provider does not support --continue"
6. The Codex provider mounts `~/.codex:/home/agent/.codex` (rw), `.ssh` (ro), `.config/gh` (ro), `pnpm-store`, and sets `CODEX_HOME=/home/agent/.codex`
7. The Codex provider passes `GH_TOKEN` but not `CLAUDE_CODE_OAUTH_TOKEN`
8. Auth check for Codex verifies `~/.codex/auth.json` exists and returns a clear error if missing
9. Auth check runs before worktree creation for both providers (no cleanup needed on auth failure). This is a refactoring of the current Claude auth check which currently runs after worktree creation.
10. A test for `extract_codex_response` demonstrates: (a) returns file contents when `.sandbox-result.txt` exists, (b) returns last non-empty non-JSON line from logs when the file is missing, (c) returns `None` when neither source has content
11. `.sandbox-result.txt` is excluded from git commits via the `git reset HEAD` step
12. State file includes `"provider"` field; the final result JSON also preserves the `"provider"` field so callers know which provider was used
13. When `read` loads a state file without a `provider` field (pre-existing sandbox), it defaults to `"claude"`
14. The `read` command loads provider from state file, calls `get_provider()`, and passes the provider dict to `_collect_and_finalize()`
15. Error message for non-zero exit uses `f"{provider['name']} exited with code {exit_code}"` instead of hardcoded `"Claude exited with code {exit_code}"`
16. `--provider` flag on `--continue` is ignored; provider is read from state file with warning to stderr
17. `Dockerfile` installs `@openai/codex@latest` alongside `@anthropic-ai/claude-code@latest`
18. Provider-agnostic mounts (worktree, main_git) stay in the caller; provider-specific mounts come from the provider dict. Verify by asserting `get_provider("codex")["env_vars"]()` returns strings without `-e` prefix.
19. `HELP_TEXT` updated to mention `--provider` flag in the `start` command description
20. `pytest` passes with no failures. CLI flag parsing tests use `click.testing.CliRunner` to exercise `--provider` without requiring Docker.
21. Unknown provider name (e.g. `--provider foo`) raises `click.UsageError`

## Project context

- Python CLI using `click`
- Package manager: `uv` (pyproject.toml, no setup.py)
- Tests: `pytest` in `tests/` directory
- Single-file implementation: all code is in `sandbox_cli/__init__.py`
- The Dockerfile is at `sandbox_cli/Dockerfile`

## Environment prerequisites

None. This task does not require Docker to be running, Codex to be installed, or any auth tokens. It is a code-only change verified by unit tests and code inspection.

## Constraints

- Do not split `__init__.py` into multiple files. The provider abstraction lives in the same file.
- Do not use classes or inheritance for providers. Use dicts of callables as described in the spec.
- Do not add dependencies beyond what's in `pyproject.toml` already.

## Known decisions / invariants

- The `-o` flag for Codex must use the absolute worktree path (`str(worktree_path / ".sandbox-result.txt")`) because worktrees are identity-mounted (`-v {path}:{path}`).
- `build_resume_cmd` for Codex raises `click.UsageError` via a generator throw pattern since `raise` is not valid in a lambda.
- `volume_mounts` and `env_vars` return values only (not `-v`/`-e` prefixed). The caller adds the Docker flag prefixes.
- The auth check refactoring (moving it before worktree creation) applies to both providers. The current Claude auth check at `__init__.py` line ~429 must move earlier.
- The `--continue` code path in `run_sandbox_background` currently does not read the state file. The provider implementation must add state file reading to this path so the provider can be loaded from the persisted state.

## Problem solving

When you hit errors or unexpected behavior:

1. Read the actual error carefully.
2. Inspect the relevant code, config, docs, or package API before guessing.
3. Verify whether the issue comes from stale assumptions, version drift, environment differences, or a real bug in the current implementation.
4. Prefer evidence-based fixes over speculative edits.
5. If an approach is not working after a few serious attempts, step back and try a different approach instead of patching the same path indefinitely.
6. If a dependency or integration path is fighting the task, consider a simpler alternative that still satisfies the acceptance criteria.

## Testing and verification

Use the fast checks while iterating, then run the final verification before claiming completion. Infer any additional checks needed to prove the acceptance criteria.

### Fast iteration checks

```sh
uv run pytest tests/ -x -q
```

### Final verification

```sh
uv run pytest tests/ -v
```

Also verify:
- `grep codex sandbox_cli/Dockerfile` confirms Codex CLI is in the install line
- The provider dict pattern matches the spec (code inspection)
- All code paths in `run_sandbox_background` and `_collect_and_finalize` use the provider dict instead of hardcoded Claude commands
- The `read` command loads provider from state file
- CLI flag parsing tests use `click.testing.CliRunner` to exercise `--provider` without Docker

## Required output artifacts

Before finishing, write:

- `docs/task-runs/provider-support-decisions.md`

At minimum, document:

- the key decisions you made and why
- what worked and what did not
- anything surprising or different from the references
- any residual risks or recommended next steps

## Final response

When the work is complete, report:

1. what you changed
2. how you verified it
3. whether every acceptance criterion passed
4. any remaining risks or blockers
