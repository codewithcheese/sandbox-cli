# Sandbox Operations Guide

How to write tasks, run sandboxes, integrate results, and avoid the failure modes we've hit in this project.

## Writing Task Prompts

See `docs/tasks/claude-sandbox-prototype-authoring-guide.md` for the template and authoring rules. This section covers what we learned applying it.

### Default workflow

1. Write or update the design spec first.
2. Write the sandbox task as a thin execution wrapper around that spec.
3. Keep the task prompt focused on sandbox operation: what to read, what not to do, how to verify, what counts as blocked, and what artifacts to leave behind.

If the task prompt starts looking like a second design doc, stop and move that material back into the spec.

### What works

- **A current design spec as source of truth.** The spec should own architecture, behavior, data shape, and detailed decisions. If that material already lives in the spec, do not restate it in the task prompt.
- **Thin task wrappers.** The best task files tell the agent how to operate in the sandbox, not how to redesign the feature.
- **Outcome-focused acceptance criteria.** "The editor renders markdown with headings, bold, and lists" beats "Create a PlateEditorRoot.tsx with MarkdownPlugin." Let the agent decide implementation.
- **Explicit scope boundaries.** "No canvas integration, no agent SDK" prevents scope creep. Sandbox agents will build whatever you don't exclude.
- **Execution-critical project context.** Svelte 5 runes mode, pnpm, vitest config details, adapter-node. Without these, the agent wastes time discovering them or writes Svelte 4 code. Keep this section short.
- **Source-of-truth references plus a few targeted files.** Pointing to the spec and exact files prevents the agent from guessing at patterns that already exist in the codebase.
- **Task-scoped decision artifacts.** `docs/task-runs/<task-slug>-decisions.md` instead of a shared `DECISIONS.md`. We had constant merge conflicts on DECISIONS.md because every sandbox wrote to the same file.

### What doesn't work

- **Duplicating the design spec inside the task prompt.** When the task restates architecture, schema details, long behavior descriptions, and file inventories, it gets noisy and drifts out of sync with the spec.
- **Prescriptive implementation steps.** "Create file X with function Y using approach Z" makes the agent follow blindly rather than adapt. We got better results from "prove that X works" and letting the agent figure out how.
- **Vague acceptance criteria.** "Works well" and "clean integration" are unverifiable. Every criterion must be something the agent can check with a command or assertion.
- **Missing edge cases.** The agent will satisfy the happy path. If lifecycle cleanup, field preservation, round-trip fidelity, or error handling matter, name them explicitly in the criteria.
- **Assuming the agent will question design decisions.** The agent implements what it's told. If you don't say "do not truncate data," it might add truncation as an optimization. If you don't say "do not filter elements," it might filter.

### What belongs in the task prompt vs the design spec

Put this in the design spec:

- architecture
- data model and schema rules
- UX behavior and interaction rules
- detailed file removal or migration lists
- rationale for design choices

Put this in the sandbox task prompt:

- source-of-truth references
- sandbox autonomy rules
- blockers and environment prerequisites
- scope guardrails
- observable acceptance checks
- verification commands
- required output artifacts

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
