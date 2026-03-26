# Sandbox Operations Guide

How to write tasks, run sandboxes, integrate results, and avoid the failure modes we've hit in this project.

## Writing Task Prompts

For the full template and authoring rules, run: `sandbox docs prompt-guide`

### Critical rule: No unspecified optimizations

Add this to every task prompt that handles data:

> Do not truncate, filter, summarize, or optimize data unless the acceptance criteria explicitly call for it. Preserve all content in full. If you think an optimization is needed, document it in the decisions file but do not implement it.

We lost identity/other content from system concepts and had serialization silently truncating to first paragraphs — both went undetected through multiple code reviews because the reviews checked format correctness, not content completeness.

## Running Sandboxes

### Launch pattern

```bash
# Write task to file first
sandbox start <name> --task-file docs/tasks/<task>.md --model sonnet --push
```

- Use `--push` to push the branch to origin (easier to inspect)
- Use `--model sonnet` for implementation tasks (good balance of speed and quality)
- Use `--model haiku` for simple/cheap tasks (quick tests, file listings)
- Always run with `run_in_background` — sandboxes are designed to run as background tasks so you can continue working or launch multiple in parallel

### Custom Docker image (Dockerfile.sandbox)

If the default image is missing dependencies your project needs, add a `Dockerfile.sandbox` to the repo root. The sandbox CLI will build and use it automatically — no flags required.

Use `FROM sandbox-cli:default` to extend the built-in image. The base image is always built first, so `FROM` just works.

```dockerfile
# Dockerfile.sandbox
FROM sandbox-cli:default

# Add project-specific system deps (as root)
USER root
RUN apt-get update && apt-get install -y postgresql-client && rm -rf /var/lib/apt/lists/*

# Add project-specific global packages
RUN npm install -g @prisma/cli

# Switch back to agent user
USER agent
```

The image is rebuilt every time you run `sandbox start`. The base image (`sandbox-cli:default`) includes node, git, gh CLI, claude/codex/gemini CLIs, playwright, pnpm, and the `agent` user.

To view the built-in Dockerfile as a reference: `sandbox docs dockerfile`

### Custom volume mounts

Mount additional host directories into the container with `--mount`. Uses the same `host:container[:ro]` format as Docker's `-v` flag.

```bash
# Mount a shared fixtures directory (read-only)
sandbox start <name> --task "..." --mount /data/fixtures:/fixtures:ro

# Mount a local package for development
sandbox start <name> --task "..." --mount /home/user/my-lib:/packages/my-lib

# Multiple mounts
sandbox start <name> --task "..." \
  --mount /data/fixtures:/fixtures:ro \
  --mount /home/user/my-lib:/packages/my-lib
```

Common uses:
- Shared test fixtures or seed data too large to commit
- Local packages under development that the task needs to import
- Config or credential files beyond what the provider already mounts

### Parallel safety

Tasks can run in parallel if they touch different files. Check for conflicts:
- Two tasks both modifying `src/lib/dsrp/types.ts` → conflict
- One task modifying `ChatPanel.svelte`, another modifying `visibility.ts` → safe

### When sandboxes fail

- Exit code 1 with no modified files usually means Claude hit an early error (package install failure, missing auth)
- Check `sandbox read <name>` for the result, or `docker logs` if the container still exists
- Common fix: clean up with `sandbox rm <name> --force` and retry

## Integrating Results

### Before merging

1. **Read the task-run decisions file.** Capture any important findings into `docs/prototype-findings.md` before they get lost in later work. If you are integrating an older branch that still wrote `DECISIONS.md`, read that too.
2. **Check the diff stats.** Understand what changed. Read implementation files for anything non-trivial.
3. **Look for unspecified behaviors.** Does the agent add filtering, truncation, or optimization that wasn't in the criteria? This is how bugs get smuggled in.

### Merging

```bash
git merge <branch> --no-ff -m "Merge <branch>: <description>"
```

If an older branch still uses `DECISIONS.md` and it conflicts:
```bash
git checkout --theirs DECISIONS.md && git add DECISIONS.md
```

If code files conflict, resolve manually — understand what both branches changed.

### After merging

1. **Always run `pnpm test:unit -- --run`** after every merge.
2. **Run `pnpm test:e2e`** after merging any branch that touches UI files (components, routes, pages, CSS).
3. **Run `pnpm check`** periodically to catch type errors. Sandbox agents don't run type checking unless told to.
4. **Clean up:** `sandbox rm <name> --force`

### Testing checklist

| What changed | Run |
|---|---|
| Pure TS (types, logic) | `pnpm test:unit -- --run` |
| UI components, routes | `pnpm test:unit -- --run` + `pnpm test:e2e` |
| Any merge | `pnpm test:unit -- --run` |
| Periodic | `pnpm check` (type errors) |
| Before release | All three |

## Code Reviews

### When to run them

- After completing a major integration step (vertical slice, type unification)
- After multiple sandbox branches are merged in sequence
- When you want to check spec drift or find issues before moving forward

### What review agents catch

- Spec-code mismatches (field names, behavior differences)
- Missing test coverage
- Type safety holes (`as any` casts, missing type declarations)
- Dead code and stale references
- Documented decisions not matching implementation

### What review agents miss

- **Whether the code should do what it does.** Reviews check "does this match the spec?" not "is the spec right?" The truncation bug passed multiple reviews because the code correctly implemented truncation — the problem was that truncation shouldn't have existed.
- **Premature optimizations.** Data filtering, content summarization, and scope reduction look like reasonable code to a reviewer. You need a specific "find unspecified optimizations" prompt to catch these.
- **Integration seam issues.** Reviews check individual modules well but miss format mismatches between modules (e.g., mock agent emitting different event shapes than real agent).

### Effective review prompts

**Spec alignment:** "For each module, does the code match what the spec describes?"

**Premature optimization hunt:** "Search for any place where content is trimmed, filtered, summarized, or simplified without explicit spec justification."

**Test confidence:** "For each test file, what does it prove, what's missing, and where is confidence falsely high?"

## Design Doc Maintenance

### Keep the spec current

The design spec is what sandbox agents should read first. If it's stale, agents make wrong assumptions. Update it after making design decisions:

- Removed `parts[]` → update the Concept type in the spec
- Changed to per-element acceptance → update the acceptance section
- Simplified perspective rendering → update the visual grammar

### Prototype findings as living delta

`docs/prototype-findings.md` captures implementation decisions and technical learnings. It supplements the spec, not replaces it. Update it when:

- A sandbox discovers something surprising (e.g., "svelte-flow renders parent/child as DOM siblings")
- A design decision is made (e.g., "per-element acceptance, not per-proposal")
- A technical pattern is established (e.g., "use `$state.raw` for svelte-flow arrays")

### Watch for contradiction

If the findings doc says one thing and the spec says another, sandbox agents will be confused. After updating either document, check the other for contradictions. We had "No PerspectivePointNode" in findings while the spec still described one.

## Failure Modes We Hit

### 1. Unspecified optimization smuggled in
**What happened:** Serialization truncated content to first paragraphs. System concepts lost identity/other fields.
**Root cause:** Sandbox agent added optimization without spec basis. Reviews checked format, not content.
**Prevention:** Add "do not truncate/filter/optimize without spec basis" to task prompts. Run premature optimization review after integration.

### 2. DECISIONS.md merge conflicts
**What happened:** Every sandbox wrote to repo-root DECISIONS.md, causing conflicts on every merge.
**Prevention:** Use task-scoped paths: `docs/task-runs/<task-slug>-decisions.md`.

### 3. Type errors never caught
**What happened:** 14 type errors accumulated across multiple sandboxes. `pnpm check` was never run.
**Prevention:** Add `pnpm check` to sandbox verification commands. Run it after merges.

### 4. Prototype routes left behind
**What happened:** 7 prototype routes with their own e2e tests stayed in the codebase after the integrated app was built.
**Prevention:** Plan cleanup as part of integration tasks. Prototype routes are scaffolding, not permanent.

### 5. Mock agent format mismatch
**What happened:** Mock agent emitted `{ tool, id, input }` while real agent emitted `{ tool, id, op }`. The client had to handle both formats.
**Prevention:** Define SSE event types as a shared TypeScript interface. Both mock and real agents must conform to the same shape.

### 6. Agent always returns success
**What happened:** Real agent's `applyOp` callback always returned `{ ok: true }`, so the agent never learned about failed operations.
**Root cause:** Server doesn't have a copy of the map, so it can't validate ops before returning results.
**Status:** Known gap. Needs server-side map state or client-side result feedback.

## Task Dependencies

When planning parallel work, map the dependencies:

```
Data model (types, pure logic)
  ↓
Serialization, Visibility, Proposals, Apply-op
  ↓
Canvas translation (map-to-flow)    Tool definitions    SSE bridge
  ↓                                   ↓                   ↓
Canvas components ←──── Vertical slice integration ────→ Chat panel
                              ↓
                        Detail panel
```

Independent tasks (safe to parallelize):
- Anything in `src/lib/dsrp/` vs anything in `src/lib/canvas/` vs anything in `src/lib/server/`
- New components vs existing component modifications (if different files)
- Unit-test-only tasks vs e2e-test tasks

Dependent tasks (must sequence):
- Type changes → anything that imports those types
- Translation layer changes → canvas page changes
- SSE endpoint changes → chat panel changes
