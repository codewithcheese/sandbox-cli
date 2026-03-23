# Modal Remote Runtime — Research Notes

Research conducted via ChatGPT extended thinking sessions on 2026-03-23, covering Modal Sandbox APIs, lifecycle management, file sync, execution patterns, and constraints.

## ChatGPT Conversations

| Topic | Conversation ID | Status |
|-------|----------------|--------|
| Q1: File sync mechanisms | `69c11fa6-9f18-83a0-9bbb-c6ac9cf85a2b` | Complete |
| Q2: Sandbox lifecycle | `69c11faf-9244-839c-9c22-26a3c241ab4c` | Complete (+ follow-up on idle_timeout) |
| Q3: Runtime abstraction design | `69c11fbb-ed20-839d-b715-02512922242f` | Complete |
| Q4: Claude Code in Modal patterns | `69c11fd1-3028-83a1-87bf-c519d8b44041` | Complete (recovered after timeout) |
| Q5: Pricing, limits & trade-offs | `69c11fdb-8310-8395-aff7-4e37e2f4e7e0` | Abandoned (stuck streaming) |
| Q6: Modal SDK API verification | `69c132bc-df28-839a-b491-8b40df8a618a` / `69c13739-818c-83a1-9c01-8b80684b2725` | Complete (split across 2 conversations) |
| Q7: PTY + stream-json interaction | `69c132c1-69fc-83a0-8da2-5707407037e2` | Complete |
| Q8: Modal stdout chunk types | `69c132c6-c620-8399-a127-649da3e7c292` | Complete |
| Q9: Modal pricing (retry) | `69c132ca-931c-83a0-9511-8f776cfad06d` | Complete |

To continue a conversation: `auto-chat chatgpt submit --conversation <id> --model thinking --thinking standard "follow-up question"`

## Sources

- [Modal Sandbox Guide](https://modal.com/docs/guide/sandboxes)
- [Modal Sandbox API Reference](https://modal.com/docs/reference/modal.Sandbox)
- [Modal Sandbox File Access](https://modal.com/docs/guide/sandbox-files)
- [Modal Sandbox Spawn (exec)](https://modal.com/docs/guide/sandbox-spawn)
- [Modal Sandbox Snapshots](https://modal.com/docs/guide/sandbox-snapshots)
- [Modal Exceptions Reference](https://modal.com/docs/reference/modal.exception)
- [Modal Secrets Guide](https://modal.com/docs/guide/secrets)
- [Modal Images Guide](https://modal.com/docs/reference/modal.Image)
- [Modal Volumes Guide](https://modal.com/docs/guide/volumes)
- [Modal Async Guide](https://modal.com/docs/guide/async)
- [Modal Claude Code Example](https://modal.com/docs/examples/sandbox_agent)
- [Modal Cloud Bucket Mounts](https://modal.com/docs/guide/cloud-bucket-mounts)
- [Claude Code Headless Docs](https://code.claude.com/docs/en/headless)

---

## 1. Sandbox Lifecycle — Docker-to-Modal Mapping

| Docker | Modal | Notes |
|--------|-------|-------|
| `docker run -d` | `Sandbox.create()` then `sb.detach()` | Store `sb.object_id` for later reconnection |
| `docker wait` | `sb.wait()` or `proc.wait()` | Sandbox-level wait for entrypoint, process-level for exec |
| `docker exec` | `sb.exec(...)` → `ContainerProcess` | Returns process with `wait()`, `poll()`, `returncode`, `stdout`, `stderr` |
| `docker logs` | `sb.stdout` / `sb.stderr` | StreamReader objects — iterable for streaming, `.read()` for full output |
| `docker rm -f` | `sb.terminate(wait=True)` then `sb.detach()` | No "stopped container" state — terminated sandboxes are gone |
| `docker inspect` (exists) | `Sandbox.from_id(id)` + catch `NotFoundError` | |
| `docker inspect` (running) | `sb.poll() is None` / `sb.returncode is None` | |
| Container naming | `Sandbox.create(name=...)` | Requires deployed App. Use tags for filtering instead: `Sandbox.list(tags={...})` |

### Key lifecycle differences

- **No stopped state**: Unlike Docker, there is no "stopped container you can restart." The model is: running → terminate → optionally snapshot → create new sandbox from snapshot.
- **Reconnection**: `Sandbox.from_id(sandbox_id)` reconnects from a different Python process. `sb.detach()` releases the local client connection without killing the remote sandbox.
- **Listing/filtering**: `Sandbox.list(tags={"tool": "sandbox-cli"})` with client-side filtering on `returncode`.

### Reconnection pattern

```python
import json
from pathlib import Path
import modal
from modal.exception import NotFoundError

sandbox_id = json.loads(state_file.read_text())["sandboxId"]
try:
    sb = modal.Sandbox.from_id(sandbox_id)
except NotFoundError:
    print("sandbox no longer exists")
    return

code = sb.poll()
if code is None:
    print("still running")
    sb.wait()
    code = sb.returncode

stdout = sb.stdout.read()
stderr = sb.stderr.read()
sb.detach()
```

---

## 2. File Sync Mechanisms

Six mechanisms for moving files in/out of Modal sandboxes:

| Mechanism | Direction | Best for | Limitations |
|-----------|-----------|----------|-------------|
| `Image.add_local_dir()` | In (one-way) | Seeding repo at startup | `copy=False` for speed; no way to get files back |
| Modal Volume | Both | Best general-purpose workspace sync | Up to 2.5 GB/s; commit/reload semantics, not live POSIX |
| CloudBucketMount (S3/GCS/R2) | Both (auto-sync) | Large artifacts | Poor POSIX semantics — bad for git internals |
| Filesystem API (`sb.open`) | Both | Small files | Alpha, 100MB read / 1GB write limits, throughput-restricted |
| stdio streaming | Both | Tar archives, patches | No size limit, flexible |
| Snapshots | Between sandboxes | Warm-starting future runs | Not a laptop download path |

### Decision: git clone over network

For this project, we chose **git clone inside the sandbox** over uploading local files:
- Eliminates slow local upload bottleneck
- Cloning happens at cloud network speeds
- Push happens inside the sandbox — results are on the remote
- No Volume or batch_upload needed for initial workspace

### NetworkFileSystem

Deprecated. Do not use. Modal recommends Volume instead.

---

## 3. Timeout & Idle Behavior

- **Default timeout**: 5 minutes. Configurable up to 24 hours via `timeout=` parameter.
- **Cannot extend after creation**: Must set generously upfront.
- **idle_timeout**: No default (disabled if omitted). Only triggers based on "activity."
- **Activity definition**: A sandbox is "active" if:
  1. It has an active command running via `sb.exec()`
  2. Its stdin is being written to
  3. It has an open TCP connection over a Tunnel
- **stdout/stderr output alone does NOT count as activity.**
- **Running process counts**: As long as Claude Code's process is running, idle_timeout won't trigger. It only matters after the process exits.

### Recommended configuration

```python
Sandbox.create(
    timeout=7200,       # 2 hours hard max (agents can run 30+ min)
    idle_timeout=600,   # 10 min safety net after process exits
)
```

---

## 4. Error Handling

Modal exceptions relevant to sandbox lifecycle:

| Exception | When |
|-----------|------|
| `SandboxTimeoutError` | Sandbox exceeds its `timeout` |
| `SandboxTerminatedError` | Sandbox terminated for internal reason |
| `ExecTimeoutError` | An `exec()` process exceeds its timeout |
| `ExecutionError` | General runtime failure |
| `RemoteError` | Remote execution failure |
| `NotFoundError` | `Sandbox.from_id()` with invalid/expired ID |

**OOM**: No dedicated exception. Container is OOM-killed; treat as abrupt termination. Inspect `returncode` and read stderr for context.

### Safe error handling pattern

```python
from modal.exception import (
    SandboxTimeoutError,
    SandboxTerminatedError,
    ExecTimeoutError,
    NotFoundError,
)

try:
    proc = sb.exec("bash", "/opt/runner.sh", ...)
    code = proc.wait()
except ExecTimeoutError:
    # subprocess timed out
except SandboxTimeoutError:
    # sandbox lifetime exceeded
except SandboxTerminatedError:
    # internal termination (OOM, infra issue)
finally:
    sb.terminate(wait=True)
    sb.detach()
```

---

## 5. Git Auth in Sandboxes

### Recommended: GH_TOKEN + GIT_ASKPASS

```bash
cat >/tmp/git-askpass.sh <<'EOF'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' "x-access-token" ;;
  *Password*) printf '%s\n' "${GH_TOKEN}" ;;
  *) printf '\n' ;;
esac
EOF
chmod 700 /tmp/git-askpass.sh
export GIT_ASKPASS=/tmp/git-askpass.sh
export GIT_TERMINAL_PROMPT=0
```

**Why not SSH**: Adds known_hosts, key material, passphrase/agent handling — more operational drag in ephemeral sandboxes.

**Why GIT_ASKPASS over URL embedding**: Avoids storing token in `.git/config` or printing it in remote URLs.

### Secrets injection

```python
modal.Secret.from_dict({
    "GH_TOKEN": gh_token,
    "CLAUDE_CODE_OAUTH_TOKEN": auth_token,
})
```

Inline secrets, read from user's local environment at launch time. No persistent Modal Secrets.

---

## 6. Claude Code Execution in Modal

### PTY mode

Modal's own Claude Code example uses `pty=True`. This is the documented working pattern.

### Streaming output

Use `--output-format stream-json --verbose` for Claude. Each stdout line is an NDJSON event. The Modal process stdout is an iterable StreamReader — tee to terminal and capture simultaneously.

### Response extraction

Parse captured NDJSON log for the last `"type": "result"` object, or extract text deltas:

```bash
jq -rj '
  select(.type == "stream_event" and .event.delta.type? == "text_delta")
  | .event.delta.text
' < agent-stream.ndjson
```

The existing `extract_response()` function in the Claude provider already handles this parsing from a log file/string.

### Single runner script vs multiple execs

**Recommendation: Single runner script** for the mutating git workflow. Multiple execs are fine for setup/inspection, but splitting clone → agent → commit → push across separate host exec() calls makes state recovery, log correlation, and failure semantics worse.

---

## 7. Modal Image Caching

- Modal caches unchanged image definitions automatically.
- `Image.build()` returns the cached image if already built.
- No official public TTL documented for image cache retention.
- For tighter control, build your own registry image and use `Image.from_registry(...)`.
- First build is slow (installing Node.js, Claude Code, etc.); subsequent runs reuse cache.

---

## 8. Resource Defaults & Limits

| Resource | Default | Configurable |
|----------|---------|-------------|
| CPU | 0.125 | Yes, per sandbox |
| Memory | 128 MiB | Yes, per sandbox |
| Disk | 512 GiB | Up to 3.0 TiB via `ephemeral_disk` |
| Timeout | 5 min | Up to 24 hours |
| idle_timeout | None (disabled) | Yes, per sandbox |

Over-limit requests are rejected at creation time.

---

## 9. Async vs Sync

Modal provides both blocking and async APIs. Async access via `.aio` suffix (e.g. `sb.exec.aio()`, `proc.wait.aio()`).

**Decision: Keep Click CLI synchronous.** Use Modal's blocking methods. Only switch to async if we later need multiplexed log streaming or many sandboxes in flight.

---

## 10. Runtime Abstraction Design (deferred)

The research recommended a three-layer separation:
- **Provider** = what to run (Claude/Codex/Gemini command, env, auth)
- **Runtime** = where to run (Docker container vs Modal sandbox)
- **WorkspaceSync** = how files move (bind mount vs git clone)

With a Protocol-based interface:
- `SandboxRuntime.create(spec) → SandboxHandle`
- `SandboxHandle.exec(argv) → ProcessHandle`
- `ProcessHandle.wait() → int`

**Deferred for v1.** We implement a parallel code path (`run_sandbox_remote()`) structured so these pieces can be extracted later. The abstraction should emerge from real usage rather than speculation.

---

## 11. Stdout Streaming Details (Q8)

Source: [Modal IO Streams Reference](https://modal.com/docs/reference/modal.io_streams)

### Chunk types

- `proc.stdout` is a `StreamReader`. Iterating yields **chunks, not lines**.
- With `text=True` (default for `sb.exec()`): chunks are `str`.
- With `text=False`: chunks are `bytes`.
- Chunks are **not guaranteed to be newline-delimited**. One chunk may contain multiple lines or a partial line.

### Streaming pattern

```python
proc = sb.exec(*cmd, text=True)

with log_path.open("w", encoding="utf-8") as log_file:
    for chunk in proc.stdout:
        sys.stdout.write(chunk)
        sys.stdout.flush()
        log_file.write(chunk)
        log_file.flush()

    returncode = proc.wait()

# Parse AFTER completion — split into lines
full_log = log_path.read_text(encoding="utf-8")
for line in full_log.splitlines():
    if line.startswith("__SANDBOX_RESULT__"):
        marker_payload = line[len("__SANDBOX_RESULT__"):].lstrip()
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        pass  # skip non-JSON lines
```

Key rule: tee chunks directly to terminal + file during streaming, then `splitlines()` on the completed log for NDJSON/marker parsing.

---

## 12. PTY + stream-json Interaction (Q7)

Sources: [Claude Code issue #23211](https://github.com/anthropics/claude-code/issues/23211), [#12007](https://github.com/anthropics/claude-code/issues/12007), [#9026](https://github.com/anthropics/claude-code/issues/9026), [#25670](https://github.com/anthropics/claude-code/issues/25670), [Modal sandbox_agent example](https://github.com/modal-labs/modal-examples/blob/main/13_sandboxes/sandbox_agent.py)

### Key finding: prefer `pty=False` for print mode

- PTY itself doesn't inject ANSI codes, but PTY mode can cause Claude Code (a TUI app) to emit terminal control output.
- `claude -p --output-format stream-json` is intended for headless/non-interactive use and should work without a PTY.
- Modal's example uses `pty=True` but that's for interactive Claude, not print mode.
- Real bug reports show ANSI/debug text leaking onto stdout in TTY contexts.
- Some Claude versions have hung without a TTY in `-p` mode — version-dependent.

### Recommended approach

1. **Default to `pty=False`** for `claude -p --output-format stream-json`
2. **Guard during parsing**: skip blank lines, log lines that don't start with `{` as protocol corruption
3. **Fallback**: if Claude hangs without PTY, retry with `pty=True` + ANSI stripping

### ANSI stripping (fallback only)

```python
import re
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
clean_line = ANSI_RE.sub('', raw_line)
```

---

## 13. Modal Pricing (Q9)

Source: [Modal Sandbox Pricing](https://modal.com/products/sandboxes), [Modal Pricing](https://modal.com/pricing), [Modal Billing Guide](https://modal.com/docs/guide/billing)

### Per-second rates

| Resource | Cost |
|----------|------|
| CPU | $0.00003942/core-sec |
| Memory | $0.00000672/GiB-sec |
| Min billing increment | None (per-second) |

Note: Modal CPU unit = 1 physical core = 2 vCPU equivalent.

### Cost examples (2 cores, 4 GiB)

| Duration | CPU cost | Memory cost | Total |
|----------|----------|-------------|-------|
| 10 min | $0.047 | $0.016 | **$0.063** |
| 30 min | $0.142 | $0.048 | **$0.190** |
| 1 hour | $0.284 | $0.097 | **$0.381** |

### Account limits

| Plan | Free credits | Max concurrent containers |
|------|-------------|--------------------------|
| Starter | $30/month | 100 |
| Team | $100/month | 1,000 |
| Enterprise | Custom | Custom |

### Not clearly published

- Idle billing (whether you pay while sandbox exists but CPU is idle)
- General egress/network costs
- Sandbox-specific concurrency cap (above numbers are container limits)

---

## 14. Modal SDK API Verification (Q6)

All API calls verified correct against current Modal SDK. Sources: [modal.App reference](https://modal.com/docs/reference/modal.App), [modal.Sandbox reference](https://modal.com/docs/reference/modal.Sandbox)

### `modal.App.lookup()`

```python
app = modal.App.lookup('my-app', create_if_missing=True)
```
**Correct.** `App.lookup` is explicitly still supported (most other `.lookup()` methods were removed). Documented as the way to associate an App with a Sandbox.

### `modal.Sandbox.create()` parameters

All verified correct:

| Parameter | Type | Verified |
|-----------|------|----------|
| `app` | `modal.App` | Yes |
| `image` | `modal.Image` | Yes |
| `timeout` | `int` (seconds) | Yes |
| `idle_timeout` | `int` (seconds) | Yes |
| `cpu` | `float` (cores) | Yes |
| `memory` | `int` (MiB) | Yes |
| `secrets` | `list[Secret]` | Yes |

### `sb.exec()` parameters

All verified correct:

| Parameter | Type | Verified |
|-----------|------|----------|
| `*cmd` | positional strings | Yes |
| `pty` | `bool` | Yes |
| `text` | `bool` (default `True`) | Yes |
| `timeout` | `int` (seconds) | Yes |

### Other verified APIs

| Call | Correct |
|------|---------|
| `modal.Secret.from_dict({'KEY': 'val'})` | Yes |
| `sb.object_id` | Yes |
| `modal.Sandbox.from_id(sandbox_id)` | Yes |
| `sb.poll()` / `sb.returncode` | Yes |
| `sb.stdout.read()` / `sb.stderr.read()` | Yes |
| `sb.terminate(wait=True)` | Yes |
| `sb.detach()` | Yes |
| `modal.Image.debian_slim(python_version='3.12').apt_install(...).run_commands(...)` | Yes |
