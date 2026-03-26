# Sandbox Task Prompt Authoring Guide

Use this guide to write autonomous task prompts for agents running via the local `sandbox` CLI.

It is optimized for implementation-heavy tasks where the agent inspects the repo, debugs through failures, and keeps going until acceptance criteria are met. Not for pure research, code review, or planning-only work.

The default pattern is: keep the design in a spec, and keep the task prompt focused on how the agent should operate inside the sandbox.

## Default operating model: spec first, task prompt second

When a current design spec already exists, use this split:

- the design spec owns the architecture, behavior, data model, and detailed decisions
- the sandbox task prompt owns the operating contract, verification, blockers, and output artifacts
- acceptance criteria prove that the implementation satisfies the spec; they do not need to restate the entire spec
- only repeat a design detail in the task prompt if it is easy for the agent to miss or violate

If no design spec exists yet, either write one first or accept that the task prompt will need to carry more design detail than usual.

## Authoring rules

### 1. Reference the design spec as the source of truth

If a current design spec already exists, point Claude to it explicitly and avoid re-explaining the whole design in the task prompt.

The task prompt should usually not duplicate:

- full architecture descriptions
- exhaustive file-by-file implementation plans
- schema dumps already captured in the spec
- long UI behavior descriptions already defined in the spec
- complete removal lists already documented elsewhere

Repeat only what is sandbox-specific, easy to violate, or required to interpret the acceptance criteria.

### 2. Separate hard requirements from background

Keep these sections distinct:

- `Goal`: what must exist at the end
- `Acceptance criteria`: what proves success
- `Constraints`: hard limits
- `Source of truth`: the spec and files Claude should treat as primary guidance
- `Out of scope`: what Claude should not build

When these are mixed together, Claude can misread suggestions as requirements or ignore a hard requirement that looks like a side note.

### 3. Write acceptance criteria as observable proofs

Each criterion should be something Claude can check directly. Good examples:

- a route renders with no console errors
- markdown round-trips with structural equivalence
- switching fields preserves edits
- navigating away and back does not preserve stale editor state
- specific build and test commands pass

Avoid vague criteria like "works well" or "clean integration" unless you immediately define what those mean operationally.

When a design spec already exists, acceptance criteria should mostly be proofs of that spec, not a second copy of it.

### 4. Encode the edge cases you care about

Claude will often satisfy the happy path unless the prompt names the failure modes. If lifecycle, cleanup, state preservation, version drift, serialization, or console cleanliness matter, say so explicitly in the acceptance criteria.

### 5. Surface external prerequisites separately

Do not hide external assumptions inside ordinary project context.

If the task depends on authenticated CLIs, network access, seeded services, environment variables, existing datasets, or other non-repo conditions, put them in a dedicated `Environment prerequisites` section.

This sharpens Claude's blocker model. Repo facts and external preconditions are different classes of information and should not be mixed together.

### 6. Give Claude bounded autonomy

For sandboxed container work, a useful default is:

- decide locally when details are underspecified
- keep going through ambiguity and ordinary debugging friction
- use destructive cleanup inside the container if it helps unblock progress
- stop only for true external blockers such as missing credentials, missing services, or unavailable resources outside the container

If you do not define this, Claude is more likely either to ask too many questions or to thrash without changing approach.

### 7. Use tiered verification

Provide known verification commands when you have them. Also write the criteria clearly enough that Claude can infer missing checks on its own.

When possible, split verification into:

- `Fast iteration checks` for the smallest useful command set while debugging
- `Final verification` for the full closure run before Claude claims completion

This makes autonomous runs faster and more surgical. Without this split, prompts tend to push Claude toward full-suite verification even when it is still fixing a local issue.

### 8. Require completion artifacts with task-scoped paths

Prototype work is often about reducing uncertainty. Require Claude to leave behind artifacts such as:

- `docs/task-runs/<task-slug>-decisions.md`
- `docs/task-runs/<task-slug>-verification.md`
- a short verification summary
- notes on what worked, what did not, and what to try next

Avoid defaulting to repo-root `DECISIONS.md`. That works for one task at a time, but it creates collisions across repeated or parallel runs.

### 9. Keep core sections present

The core sections should appear in every prompt, even when a section is intentionally empty.

If a section truly does not apply, write `None` instead of omitting it. That removes ambiguity between "not applicable" and "forgotten."

### 10. Define "blocked" narrowly

Normal implementation pain is not a blocker. Failing tests, dependency conflicts, stale docs, and runtime bugs are expected parts of the task.

Treat Claude as blocked only when progress requires something it cannot obtain or create inside the container.

## How to fill the template

Keep the filled prompt concrete and compact.

### `Goal`

Describe the intended result in one to three sentences. Focus on what the sandbox run must prove or deliver, not the full design.

### `Source of truth`

List the design spec first, then only the most important supporting files or docs.

This section answers: what should Claude read before making implementation decisions?

If a current design spec exists, do not restate its contents across the rest of the prompt unless a detail is easy to miss and costly to get wrong.

### `Out of scope`

Say what should not be built. This is one of the easiest ways to prevent scope creep in autonomous runs.

### `Acceptance criteria`

Each item should describe an observable behavior, artifact, or command result.

### `Autonomy contract`

Be explicit about:

- whether Claude should make local decisions
- whether destructive cleanup is allowed
- what counts as a blocker
- whether Claude should keep going until all criteria pass

### `Project context`

Use flat bullets with only execution-critical facts:

- framework and version
- package manager
- test runners
- existing config quirks
- important paths

Do not use this section to copy architecture or product behavior already defined in the spec.

### `Environment prerequisites`

External conditions that must be in place before the task starts (authenticated CLIs, network access, env vars, etc.). If none, write `None`.

### `Known decisions / invariants`

Use this section only for decisions Claude should treat as fixed and that are worth repeating even though the spec already exists. Examples:

- required architecture boundaries
- already-made product or schema decisions
- invariants that should not be re-litigated during implementation
- known traps where one approach is preferred over another

If the spec already covers everything that matters here, write `None`.

### `Verification`

Prefer two subsections:

- `Fast iteration checks`
- `Final verification`

If you only know the final commands, provide those. If you know a smaller debugging loop as well, include it explicitly.

### `Completion artifacts`

State what written output must remain in the repo at the end of the run. Prefer task-scoped paths under a folder like `docs/task-runs/` instead of a shared repo-root file.

## Embedded prompt template

Copy this template and replace the placeholders.

````md
# Task: <TASK_TITLE>

## Goal

<GOAL>

## Source of truth

Read these first and treat them as the design source of truth for this task:

- <DESIGN_SPEC_PATH>
- <IMPORTANT_FILE_OR_DOC_1>
- <IMPORTANT_FILE_OR_DOC_2>

Do not re-litigate the design described there unless the task explicitly says you may.

If the references conflict with the actual code, installed dependencies, or observed runtime behavior, trust reality and document the drift.

## Out of scope

<OUT_OF_SCOPE>

## Autonomy

Work fully autonomously inside the sandboxed container. Do not stop to ask for decisions or confirmation during normal implementation, debugging, testing, or cleanup work.

When you encounter ambiguity or a local decision point, decide yourself and keep going. Use the repository, the code, the installed dependencies, command output, and runtime behavior as your source of truth.

You may use destructive cleanup inside the container if it is useful to complete the task safely and efficiently.

Treat yourself as blocked only when progress requires an external dependency or resource that is genuinely unavailable from inside the container, such as missing credentials, unavailable external services, or required inputs that do not exist in the repo or environment.

Keep going until all acceptance criteria are satisfied or you hit a true blocker.

## Success standard

The task is complete only when the implementation, verification, and required output artifacts all satisfy the acceptance criteria below.

## Acceptance criteria

1. <ACCEPTANCE_CRITERION_1>
2. <ACCEPTANCE_CRITERION_2>
3. <ACCEPTANCE_CRITERION_3>

## Project context

- <PROJECT_CONTEXT_ITEM_1>
- <PROJECT_CONTEXT_ITEM_2>
- <PROJECT_CONTEXT_ITEM_3>

If there is no additional execution-critical context beyond the source-of-truth references, write `None`.

## Environment prerequisites

- <PREREQUISITE_1>
- <PREREQUISITE_2>

If there are no external prerequisites, write `None`.

## Constraints

- <CONSTRAINT_1>
- <CONSTRAINT_2>

If there are no additional constraints, write `None`.

## Known decisions / invariants

- <INVARIANT_1>
- <INVARIANT_2>

If there are no extra invariants worth repeating beyond the source-of-truth docs, write `None`.

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
<FAST_CHECK_COMMAND_1>
<FAST_CHECK_COMMAND_2>
```

If there are no smaller fast checks, write `None`.

### Final verification

```sh
<FINAL_VERIFICATION_COMMAND_1>
<FINAL_VERIFICATION_COMMAND_2>
<FINAL_VERIFICATION_COMMAND_3>
```

Also perform any manual or runtime checks required by the acceptance criteria, such as console inspection, route navigation checks, state reset checks, or round-trip validation.

Do not claim success until the criteria are supported by actual evidence from the implementation and verification steps.

## Required output artifacts

Before finishing, write these artifacts if they are requested by the task:

- <OUTPUT_ARTIFACT_PATH_1>
- <OUTPUT_ARTIFACT_PATH_2>

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
````

## Optional modules

Add only the modules that help the task. Too many modules make the prompt noisy and reduce priority clarity.

### Known decisions and invariants module

Use when there are architectural choices, schema rules, or implementation decisions Claude should treat as fixed rather than rediscovering.

Failure mode prevented: autonomous re-litigation of decisions the task author already made.

```md
## Known decisions / invariants

- <INVARIANT_1>
- <INVARIANT_2>
- <INVARIANT_3>

Treat these as fixed unless the task explicitly says you may revisit them.
```

Do not use this section to paste back a large portion of the design spec. Repeat only what is easy to miss and expensive to get wrong.

### Supplemental references module

Use when Claude must study a few additional docs or files beyond the main source-of-truth list.

Failure mode prevented: premature implementation from stale assumptions.

```md
## Supplemental references

Also inspect these before making implementation decisions:

- <SUPPLEMENTAL_PATH_1>
- <SUPPLEMENTAL_PATH_2>
- <SUPPLEMENTAL_PATH_3>

Use these to fill in implementation details. The design spec remains the primary source of truth.
```

### Dependency and install module

Use when the task may require adding packages, adapters, or build tooling.

Failure mode prevented: over-committing to stale package guidance or avoiding necessary dependency changes.

```md
## Dependency changes

You may install, remove, or replace dependencies if needed to satisfy the acceptance criteria.

Before locking into a package-specific approach, inspect the actual package version, docs, and exported APIs available in this environment.

If the originally suggested package path is not working well, choose a simpler alternative that still satisfies the goal and acceptance criteria.
```

### Browser and UI verification module

Use for routes, interactions, runtime console cleanliness, lifecycle behavior, or navigation-sensitive flows.

Failure mode prevented: passing build/tests while missing real browser behavior.

```md
## Browser verification

Verify the implementation in a browser-like environment, not only through static tests.

Check for:

- runtime console errors and warnings that indicate a broken integration
- expected route rendering and interaction behavior
- lifecycle correctness when navigating away and back
- stale state, duplicate mounts, or cleanup issues

If the acceptance criteria imply user-visible behavior, do not treat unit coverage alone as sufficient proof.
```

### Data round-trip module

Use when exact text equality is too strict but structure must survive parse and serialize steps.

Failure mode prevented: superficial passing behavior that breaks semantic fidelity.

```md
## Round-trip expectations

Treat round-trip correctness as structural equivalence unless the task says otherwise.

Whitespace, formatting, or ordering differences are acceptable only if the same meaningful elements, content, and relationships are preserved.

Use tests or inspection that prove semantic equivalence, not just that some output string was produced.
```

### Debugging escalation module

Use for integrations likely to fail in subtle or repeated ways.

Failure mode prevented: local thrashing on a weak path.

```md
## Debugging escalation

If the current approach keeps failing after a few serious attempts, stop patching around the same path and reassess the approach.

Identify what assumption is failing, what evidence supports that conclusion, and what different approach is more likely to satisfy the acceptance criteria.

Prefer changing the approach over accumulating brittle workaround layers.
```

### Sandbox limitation exit module

Use when the task depends on runtimes, binaries, or system capabilities that may not be available inside the sandbox container (e.g., specific GLIBC versions, GPU access, native binaries, privileged ports).

Failure mode prevented: implementing a degraded workaround that technically "passes" but doesn't actually validate what the task was designed to prove. The sandbox agent should exit early with a clear report instead of delivering a compromised result that wastes review time.

```md
## Sandbox limitations

If a core acceptance criterion cannot be satisfied due to a sandbox environment limitation (missing system library, incompatible binary, unavailable runtime), do not implement a degraded workaround. Instead:

1. Stop immediately.
2. Write `docs/task-runs/<task-slug>-decisions.md` documenting:
   - which criterion cannot be met
   - what the sandbox limitation is (exact error, missing dependency, version mismatch)
   - what would be needed to run this task successfully (e.g., GLIBC 2.32+, x86_64 host, specific runtime)
3. Commit the decisions file and exit.

A clear failure report is more valuable than a compromised implementation that cannot prove the acceptance criteria.
```

### Migration and version-drift module

Use when docs, research notes, or older examples may not match the installed version.

Failure mode prevented: implementing against APIs that do not exist in the current environment.

```md
## Version drift

Assume that reference material may be stale.

If package APIs, config shapes, or setup steps differ from the references, follow the actual installed version and adapt the implementation accordingly.

Document any meaningful drift you find in the final artifacts.
```

### Artifact and reporting module

Use when the prototype is meant to reduce uncertainty, compare approaches, or produce reusable findings.

Failure mode prevented: finishing with code changes but no durable record of what was learned.

```md
## Required documentation

Before finishing, write `<ARTIFACT_PATH>` with:

- decisions made and why
- alternatives considered
- what failed and how you recovered
- what still looks risky
- recommendations for the next phase

Prefer a task-scoped path such as `docs/task-runs/<task-slug>-decisions.md` instead of a shared repo-root file.
```

## Common prompt-author mistakes

Avoid these patterns:

- copying large parts of the design spec into the task prompt
- turning the task prompt into a second design document
- asking for "autonomy" without defining what counts as blocked
- burying external prerequisites inside ordinary project context
- listing vague success criteria that cannot be proven
- mixing hard requirements and background notes in the same paragraph
- omitting core sections instead of writing `None`
- requiring tests to pass without naming the important runtime behaviors
- giving only full-suite verification commands when a fast debug loop is obvious
- assuming Claude will infer edge cases you never mentioned
- allowing the agent to implement degraded workarounds when a sandbox limitation prevents validating the actual goal
- overloading the prompt with too many optional modules

## Quick author checklist

Before using the prompt, check:

1. Is there a current design spec, and is it linked as the source of truth?
2. Does the goal say what the prototype must prove?
3. Are the acceptance criteria observable and testable?
4. Have you named the important edge cases?
5. Is the autonomy boundary explicit?
6. Are external prerequisites called out separately from repo context?
7. Are the key repo facts and source-of-truth references listed?
8. Have you kept all core sections present, using `None` where needed?
9. Are verification expectations split into fast checks and final closure where useful?
10. Are artifact paths task-scoped instead of shared across runs?
11. If the task depends on specific runtimes or system capabilities, have you included the sandbox limitation exit module?
