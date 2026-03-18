# Gemini Provider Implementation Decisions

## Date
2026-03-18

## Overview
Added `gemini` as a third provider option alongside `claude` and `codex`, following the same provider abstraction pattern in `get_provider()`.

## Key Decisions

### 1. Command structure: `gemini -p <task> --output-format json --yolo`
The spec mandates `--output-format json` (single-object) rather than stream-json (NDJSON). This means the entire output is one JSON blob, which `extract_gemini_response` parses for the `response` field.

### 2. `extract_gemini_response` design
The function handles several cases in order:
1. Try each line as JSON, return `obj["response"]` if found
2. Try the whole content as a single JSON object (handles multi-line JSON)
3. Fall back to the last non-empty, non-JSON line
4. Return `None` if no content

The `worktree_path` parameter is accepted for interface consistency but not used (unlike `extract_codex_response` which checks `.sandbox-result.txt`).

### 3. `GEMINI_API_KEY` conditional inclusion
Per spec, `GEMINI_API_KEY` is included in `env_vars()` only if set in the host environment. The OAuth path relies on the mounted `~/.gemini` directory. This avoids passing an empty variable that could override OAuth credentials.

### 4. `GEMINI_CLI_HOME=/home/agent`
This tells Gemini CLI to look for `~/.gemini` at `/home/agent/.gemini`, which is where the mount lands. This is the mechanism by which OAuth credentials are accessible inside the container.

### 5. Auth check heuristic
Auth check passes if `GEMINI_API_KEY` is set OR `~/.gemini/` directory exists (as a directory, using `is_dir()`). The directory check is a best-effort heuristic — we cannot reliably verify whether valid OAuth credential files exist without knowing Gemini CLI's internal path.

### 6. Background-only guard updated to cover both `codex` and `gemini`
Changed from `if provider == "codex"` to `if provider in ("codex", "gemini")`. The error message uses `f"{provider.capitalize()} provider only supports background task mode"` for consistency.

### 7. `build_resume_cmd` raises `click.UsageError`
Same pattern as codex: the generator-throw trick `(_ for _ in ()).throw(...)` is used so the lambda raises on call.

### 8. Volume mounts: `~/.gemini` is rw (no `:ro`)
The `.gemini` directory serves double duty for OAuth credentials AND user configuration. Mounting it rw allows Gemini CLI to update credentials if needed.

## What Worked
- The existing provider abstraction was easy to extend: adding a new `elif name == "gemini":` block with the same dict shape.
- All 139 tests passed on first run after implementation.
- The generator-throw pattern for `build_resume_cmd` was already established in `codex` and worked identically for `gemini`.

## What Was Different from References
- The spec file `docs/superpowers/specs/2026-03-19-gemini-provider.md` was not found in the worktree (the file path in the task description did not exist). The implementation was guided entirely by the acceptance criteria, the existing `codex` provider pattern, and the task description's known decisions section.

## Residual Risks
- **Gemini CLI interface**: The `gemini -p <task> --output-format json --yolo` flags assume this is the correct CLI interface for `@google/gemini-cli`. If the actual CLI uses different flag names, the command will fail at runtime. This should be verified against the installed package documentation.
- **Auth check reliability**: The `~/.gemini` directory check is a heuristic. If Gemini CLI creates the directory during install rather than login, auth might appear to pass when it hasn't occurred.
- **`--continue` path**: The `--continue` flag is guarded in `build_resume_cmd` to raise a `UsageError`, but the flow goes through `run_sandbox_background`'s `continue_session` path which calls `build_resume_cmd`. This is tested via the unit test but not via the full CLI flow.

## Recommended Next Steps
1. Validate the actual `gemini` CLI flag syntax against the installed `@google/gemini-cli` package.
2. Add an integration test that runs a real Gemini container (with `GEMINI_API_KEY` set) to verify end-to-end behavior.
3. Consider checking for a known credential file path inside `~/.gemini/` for a more reliable auth check.
