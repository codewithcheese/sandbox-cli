# Task: Implement Gemini provider support

## Goal

Add `gemini` as a third provider option for `--provider` on `sandbox start`, following the same provider abstraction pattern used by `claude` and `codex`.

## Source of truth

Read these first and treat them as the design source of truth for this task:

- `docs/superpowers/specs/2026-03-19-gemini-provider.md`
- `sandbox_cli/__init__.py` (look at the existing `get_provider()` function and `codex` provider as the pattern to follow)
- `sandbox_cli/Dockerfile`

Do not re-litigate the design described there unless the task explicitly says you may.

If the references conflict with the actual code, installed dependencies, or observed runtime behavior, trust reality and document the drift.

## Out of scope

- Interactive mode for Gemini (`--provider gemini` without `--task` must error)
- Changes to `run_sandbox()` (interactive mode) — no provider support there
- Changes to `sandbox auth` command
- Gemini stream-json / NDJSON parsing (use `--output-format json` single-object only)
- Persistent provider config or config files
- `--continue` support for Gemini (must error with clear message)
- Vertex AI or service account auth
- Additional providers beyond claude, codex, and gemini

## Autonomy

Work fully autonomously inside the sandboxed container. Do not stop to ask for decisions or confirmation during normal implementation, debugging, testing, or cleanup work.

When you encounter ambiguity or a local decision point, decide yourself and keep going. Use the repository, the code, the installed dependencies, command output, and runtime behavior as your source of truth.

You may use destructive cleanup inside the container if it is useful to complete the task safely and efficiently.

Treat yourself as blocked only when progress requires an external dependency or resource that is genuinely unavailable from inside the container, such as missing credentials, unavailable external services, or required inputs that do not exist in the repo or environment.

Keep going until all acceptance criteria are satisfied or you hit a true blocker.

## Success standard

The task is complete only when the implementation, verification, and required output artifacts all satisfy the acceptance criteria below.

## Acceptance criteria

1. `sandbox start foo --task "hello" --provider gemini` builds a docker command using `gemini -p "hello" --output-format json --yolo` instead of claude/codex commands
2. `sandbox start foo --task "hello" --provider gemini --model gemini-2.5-flash` includes `--model gemini-2.5-flash` in the gemini command
3. `sandbox start foo --provider gemini` (no `--task`) exits with error: "Gemini provider only supports background task mode" (or similar, matching the codex pattern)
4. `sandbox start foo --provider gemini --continue` (without `--task`) exits with error: "Gemini provider does not support --continue". This tests the resume path through `build_resume_cmd`, not the `--task`+`--continue` mutual exclusion guard.
5. The Gemini provider mounts `~/.gemini:/home/agent/.gemini` (rw), `.ssh` (ro), `.config/gh` (ro), and `pnpm-store`
6. The Gemini provider sets `GEMINI_CLI_HOME=/home/agent` (so Gemini finds `~/.gemini` at the mount point), passes `GEMINI_API_KEY` only if set in the host environment, and always passes `GH_TOKEN`
7. Auth check passes if `GEMINI_API_KEY` is set OR `~/.gemini/` directory exists; returns clear error message `"No Gemini auth found. Set GEMINI_API_KEY or run: gemini (to login with Google account)"` if neither. The directory check is a best-effort heuristic (we cannot reliably verify OAuth credential files exist without knowing the internal path).
8. A test for `extract_gemini_response` demonstrates: (a) returns `response` field from JSON output, (b) skips JSON objects without a `response` key and falls back to last non-empty non-JSON line, (c) returns `None` when log has no content, (d) handles malformed/partial JSON lines gracefully (skip them)
9. State file includes `"provider": "gemini"` when using gemini provider
10. The final result JSON includes `"provider": "gemini"` field
11. Error message for non-zero exit uses `"gemini exited with code {exit_code}"`
12. `Dockerfile` installs `@google/gemini-cli@latest` alongside the other CLIs
13. `HELP_TEXT` updated to mention `gemini` in the `--provider` description
14. `click.Choice` updated to `["claude", "codex", "gemini"]`
15. The guard for background-only providers includes `gemini` alongside `codex`
16. `pytest` passes with no failures
17. Unknown provider name still raises `click.UsageError`
18. Existing claude and codex provider behavior is unchanged (no regressions)

## Project context

- Python CLI using `click`
- Package manager: `uv` (pyproject.toml, no setup.py)
- Tests: `pytest` in `tests/` directory
- Single-file implementation: all code is in `sandbox_cli/__init__.py`
- The Dockerfile is at `sandbox_cli/Dockerfile`
- The provider abstraction (`get_provider()`) already exists with `claude` and `codex` — follow the same pattern exactly

## Environment prerequisites

None. This task does not require Docker, Gemini CLI, or any auth tokens. It is a code-only change verified by unit tests.

## Constraints

- Do not split `__init__.py` into multiple files.
- Do not refactor the existing provider abstraction. Add `gemini` as a third case in `get_provider()` following the exact same pattern as `codex`.
- Do not add dependencies beyond what's in `pyproject.toml` already.
- Import `os` at the top of the file if not already imported (needed for `os.environ.get`).

## Known decisions / invariants

- Gemini uses `--output-format json` (single JSON object), NOT `--output-format stream-json` (NDJSON).
- Response extraction parses the `response` field from the JSON output, not from a file like Codex.
- `GEMINI_API_KEY` is only included in `env_vars()` if actually set in the host environment. The OAuth path relies on mounted `~/.gemini` credentials instead.
- The `worktree_path` parameter in `extract_gemini_response` is accepted for interface consistency but not used.
- The `~/.gemini` mount serves double duty: OAuth credentials AND user configuration.

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
- `grep gemini-cli sandbox_cli/Dockerfile` confirms Gemini CLI is in the install line
- The gemini provider dict in `get_provider()` follows the same pattern as codex
- All existing tests still pass (no regressions)
- CLI flag parsing tests use `click.testing.CliRunner`

## Required output artifacts

Before finishing, write:

- `docs/task-runs/gemini-provider-decisions.md`

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
