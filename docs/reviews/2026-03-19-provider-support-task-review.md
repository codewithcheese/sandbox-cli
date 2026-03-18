# Code Review: Provider Support Task Prompt vs Design Spec

**Date:** 2026-03-19
**Reviewer:** Claude (code-reviewer agent)
**Files reviewed:**
- `docs/tasks/provider-support.md` (task prompt)
- `docs/superpowers/specs/2026-03-19-provider-support.md` (design spec)
- `sandbox_cli/__init__.py` (codebase)
- `sandbox_cli/docs/prompt-guide.md` (prompt guide)

---

## 1. Spec Coverage — Does the task prompt cover everything in the spec?

**Overall: Strong coverage.** The 18 acceptance criteria map well to the spec's requirements. However, there are specific gaps:

**Missing from acceptance criteria:**

- **Spec line 21, `--model` flag forwarding**: The spec CLI interface section (line 21) shows `--provider codex --model gpt-4.1` as a usage example. The task prompt never explicitly requires that `--model` is forwarded to the Codex command. AC #1 shows the codex command without `--model`. The agent might not test model-forwarding for the Codex provider. While the spec's `build_cmd` lambda at line 83-88 shows the model parameter, the AC should state that `--model` is forwarded to both providers.

- **Spec line 96-100, Codex provider `.ssh` and `.config/gh` and `pnpm-store` mounts**: AC #5 only mentions `~/.codex:/home/agent/.codex` and `CODEX_HOME`. But the spec's Codex `volume_mounts` (spec lines 96-100) also includes `.ssh`, `.config/gh`, and `pnpm-store`. These are technically visible in the spec, but a criterion saying "Codex provider includes .ssh and .config/gh mounts" would prevent the agent from only implementing the codex-specific mount and forgetting the shared ones.

- **Spec line 142, backward compatibility for state files**: The spec says "If the field is absent (pre-existing state files), default to `claude`." AC #11 says state file includes `"provider"` field and `read` uses it, but does not explicitly require the default-to-claude fallback for old state files. This is a regression-sensitive edge case.

- **Spec line 144, provider field preserved in final result JSON**: The spec says "The provider field is preserved in the final result JSON so callers know which provider was used." No AC covers this. After `_collect_and_finalize` overwrites the state file with the result dict, the `provider` key should still be present.

**Verdict: 4 spec requirements are not covered by any acceptance criterion.**

---

## 2. Contradictions — Does the task prompt contradict the spec?

**No hard contradictions found.** The task prompt and spec are aligned on all stated behaviors. One minor tension:

- AC #1 shows the Codex command as `codex exec --yolo -o <path> "hello"`. The spec (line 83-88) shows `*(["--model", model] if model else [])` between the `-o` flag and the task string. The AC's omission of `--model` is not a contradiction (it just shows the no-model case), but it could mislead the agent into thinking model is not part of the codex command.

---

## 3. Prompt Guide Compliance

Evaluating against the prompt guide's authoring rules:

| Rule | Status |
|------|--------|
| 1. Reference spec as source of truth | Compliant |
| 2. Separate hard requirements from background | Compliant |
| 3. Observable acceptance criteria | Mostly compliant (see Section 4) |
| 4. Encode edge cases | Partially compliant — misses backward-compatible state file default and provider-in-final-result |
| 5. Surface prerequisites separately | Compliant |
| 6. Bounded autonomy | Compliant |
| 7. Tiered verification | Compliant |
| 8. Completion artifacts with task-scoped paths | Compliant |
| 9. Core sections present | Compliant |
| 10. Narrow blocker definition | Compliant |

**Guide anti-pattern: "copying large parts of the design spec into the task prompt":** The task prompt does repeat some design detail in the "Known decisions / invariants" section and in the acceptance criteria themselves. AC items like #15-16 get close to restating architecture from the spec. This is borderline but acceptable given these are the most likely points of implementation error.

---

## 4. Acceptance Criteria Quality — Observable and Testable?

**Strong criteria (clear, testable):**
- AC #1-4: Command construction and error messages are directly testable
- AC #5-8: Mount paths, env vars, auth checks are inspectable
- AC #14: Dockerfile change is trivially verifiable
- AC #17: pytest passing is binary
- AC #18: Unknown provider error is testable

**Weaker criteria that need improvement:**

- **AC #9:** "Response extraction for Codex reads `.sandbox-result.txt` from the worktree, with fallback to last non-JSON line from docker logs" — This is a description of behavior, not an observable proof. It should say something like: "A test demonstrates that `extract_codex_response` returns file contents when `.sandbox-result.txt` exists, and falls back to the last non-empty non-JSON line from docker logs when the file is missing."

- **AC #12:** "Provider name appears in error messages instead of hardcoded 'Claude'" — Observable but underspecified. Which error messages? The spec specifically calls out `f"{provider['name']} exited with code {exit_code}"`. The AC should reference at least that one concrete case.

- **AC #15:** "Provider-agnostic mounts stay in the caller; provider-specific mounts come from the provider dict" — This is an architectural constraint, not an observable proof. Cannot be verified by a test or command. Consider reframing as: "The `docker run` command in `run_sandbox_background` applies `-v` prefixes to `provider['volume_mounts'](home)` results; worktree and main_git mounts are not in the provider dict."

- **AC #16:** "`env_vars()` returns `'KEY=VALUE'` strings..." — Same issue as #15. This is an API contract, not directly observable via a test command. A test could call `get_provider("codex")["env_vars"]()` and assert the format.

---

## 5. Missing Edge Cases

Based on the spec and codebase, these edge cases are not mentioned:

1. **`get_auth_token()` call timing for Claude provider:** Currently (codebase line 429-434), the auth check happens *after* worktree creation, and on failure the code has to clean up the worktree and branch. AC #8 correctly says "Auth check runs before worktree creation." But the task must refactor the auth check to happen earlier for *both* providers, not just Codex. The task prompt does not explicitly call this out as a code migration for the Claude path.

2. **The `--continue` path does not currently read a state file:** The spec notes this as a structural change. The current `continue_session` code path (codebase lines 370-388) has no state file reading. The task prompt does not flag this as a known migration step, which could confuse the agent.

3. **`extract_codex_response` receiving a `Path` vs string for `worktree_path`:** The spec shows `worktree_path` as a `Path` object. The `sb["worktree"]` value from `resolve_sandbox` is already a `Path`. But the codebase stores `worktreePath` as a string in the state file. When the `read` command recovers, `worktree_path` comes from the state file as a string. The `extract_codex_response` function will need to handle both or the caller will need to convert.

4. **The `read` command currently does not pass a provider to `_collect_and_finalize`:** The codebase shows `_collect_and_finalize(sb, exit_code, base_commit, repo_root, name)`. AC #11 says the read command should use the provider from the state file, but the prompt should note that `_collect_and_finalize`'s signature must change.

---

## 6. Scope Creep Risk

The "Out of scope" section is solid but has two gaps:

- **`run_sandbox()` (interactive mode)** is not explicitly excluded. The agent could conceivably try to add `--provider` plumbing there. Adding "No changes to `run_sandbox()` (interactive mode)" would be a cheap safeguard.

- **`HELP_TEXT` string** (codebase lines 617-684): The agent should update it to mention `--provider`, but this is not mentioned in the AC or out-of-scope. It's arguably in-scope since it is part of adding the flag.

---

## 7. Verification Adequacy

**Fast iteration checks:** `uv run pytest tests/ -x -q` — Appropriate.

**Final verification:** `uv run pytest tests/ -v` — Necessary but not sufficient.

**Missing verification:**

- No verification tests the actual CLI flag via `click.testing.CliRunner`. The prompt should ensure the agent writes CLI integration tests that exercise `--provider codex` without needing Docker.

- No verification for the Dockerfile change. A simple `grep codex sandbox_cli/Dockerfile` would suffice.

---

## Summary of Required Changes

### Critical (must fix before dispatching)

| # | Issue | Fix |
|---|-------|-----|
| 1 | Missing AC for backward-compatible state file default | Add: "When `read` loads a state file without a `provider` field, it defaults to `claude`" |
| 2 | Missing AC for provider field in final result JSON | Add: "The final result JSON includes the `provider` field" |
| 3 | Auth check migration for Claude path not flagged | Add to invariants: "The auth check refactoring applies to both providers" |

### Important (should fix)

| # | Issue | Fix |
|---|-------|-----|
| 4 | AC #1 missing `--model` forwarding | Add model variant as separate AC |
| 5 | AC #9 not testable as written | Rewrite as: "A test demonstrates extract_codex_response returns..." |
| 6 | Out of scope missing `run_sandbox()` | Add: "No changes to `run_sandbox()` (interactive mode)" |
| 7 | `--continue` structural change not flagged | Add to invariants: "The `--continue` path must add state file reading" |

### Suggestions (nice to have)

| # | Issue | Fix |
|---|-------|-----|
| 8 | AC #12 underspecified | Reference the exact error format from the spec |
| 9 | AC #15-16 not observable | Suggest asserting return format in tests |
| 10 | Missing CliRunner mention | Add note to verification section |
| 11 | Missing Dockerfile grep | Add to verification section |
| 12 | HELP_TEXT update not mentioned | Add AC or mention as expected side-effect |
