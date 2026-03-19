# sandbox-cli — Gemini Provider

Add `gemini` as a third provider option for `--provider` so that background tasks can run using Google's Gemini CLI.

## Problem

sandbox-cli supports `claude` and `codex` providers. Users who want to use Google's Gemini CLI (e.g. to use their Gemini API key or Google account subscription) have no way to do so. The provider abstraction already exists; this adds a third provider following the same pattern.

## Scope

- Background task mode only (`--task` / `--task-file`). Interactive mode stays Claude-only.
- Adds `gemini` to the existing `--provider` choice list.
- No changes to `rm`, `ls`, `ports`, `post-exit`, or interactive mode.
- No new auth commands. Gemini auth is handled by either `GEMINI_API_KEY` env var or mounting cached OAuth credentials from a prior `gemini` login.
- No persistent provider config.

## CLI Interface

```
sandbox start <name> --task "prompt" --provider gemini
sandbox start <name> --task "prompt" --provider gemini --model gemini-2.5-flash
sandbox start <name> --task "prompt" --provider gemini --model gemini-2.5-pro
```

`--provider` choice list updated from `["claude", "codex"]` to `["claude", "codex", "gemini"]`.

## Gemini CLI Integration

### Headless command

```
gemini -p "prompt" --output-format json --yolo
```

- `-p` / `--prompt`: non-interactive headless mode (verified in official repo)
- `--output-format json`: returns a single JSON object with `response`, `stats`, and optional `error` fields
- `--yolo`: auto-approve all tools (equivalent to `--dangerously-skip-permissions` / Codex's `--yolo`). Container is the real isolation boundary.
- `--model <model>`: optional model selection (e.g. `gemini-2.5-pro`, `gemini-2.5-flash`)

### No `-o` file output flag

Unlike Codex, Gemini CLI has no `-o` flag to write the final response to a file. `-o` is already taken by `--output-format`. The response must be extracted from container stdout.

### Response extraction

Since `--output-format json` returns a single JSON object to stdout, the extraction is:

1. Read the raw docker logs (stdout).
2. Find the last valid JSON object in the output.
3. Extract the `response` field from it.
4. Fall back to the last non-empty non-JSON line if no valid JSON with `response` is found.
5. Return `None` if nothing found.

This is simpler than both Claude's NDJSON parsing and Codex's file-based extraction.

### Auth

Two supported paths:

**A. API key (recommended for CI/headless):**
- Set `GEMINI_API_KEY` env var on the host
- sandbox-cli passes it through as `-e GEMINI_API_KEY` on the Docker run command
- This is the officially recommended headless auth path

**B. Cached OAuth credentials (for Google account subscriptions):**
- User runs `gemini` interactively on their machine once to complete OAuth login
- Credentials are cached in `~/.gemini/` directory. The key files are:
  - `oauth_creds.json` — OAuth token cache
  - `google_accounts.json` — account identity cache
  - `settings.json` — auth state including `security.auth.selectedType`
- sandbox-cli mounts `~/.gemini` into the container and sets `GEMINI_CLI_HOME` env var
- `GEMINI_CLI_HOME` is the official env var that controls where Gemini looks for its config root. The CLI resolves to `${GEMINI_CLI_HOME:-$HOME}/.gemini/`. Setting this avoids relying on the container user's `$HOME`.
- The CLI checks for cached credentials (`fetchCachedCredentials()`) before attempting any browser/manual auth flow. If cached creds exist, it loads them directly.
- Mount is rw because Gemini may refresh tokens and rewrite `oauth_creds.json` and `google_accounts.json` in-place
- **Caveat:** Community reports show that mounting `~/.gemini` into containers is not always reliable — permission errors, re-auth prompts, and missing sibling state files have been reported. This is a best-effort approach. API key auth is more reliable for unattended use.

**Auth check logic:**
- Check if `GEMINI_API_KEY` env var is set, OR if `~/.gemini/` directory exists
- The directory check is a best-effort heuristic. We cannot reliably verify that OAuth credential files exist within `~/.gemini/` without knowing the internal credential path (which is not documented as a stable interface). The check assumes that if the directory exists, the user has run `gemini` at least once.
- Return error if neither is available: `"No Gemini auth found. Set GEMINI_API_KEY or run: gemini (to login with Google account)"`

### Volume mounts

```python
"volume_mounts": lambda home: [
    f"{home}/.gemini:/home/agent/.gemini",   # auth + config (rw)
    f"{home}/.ssh:/home/agent/.ssh:ro",
    f"{home}/.config/gh:/home/agent/.config/gh:ro",
    "pnpm-store:/pnpm-store",
],
```

The `~/.gemini` mount serves double duty: it provides both cached OAuth credentials and any user/project configuration (settings.json, GEMINI.md).

Note: if `~/.gemini` does not exist on the host (API key path, no prior login), Docker will create an empty directory at that path on the host. This is standard Docker behavior and is benign — the empty directory has no credentials and the API key path works via env var instead.

### Environment variables

```python
"env_vars": lambda: [
    "GEMINI_CLI_HOME=/home/agent",
    *([ f"GEMINI_API_KEY={os.environ['GEMINI_API_KEY']}" ] if os.environ.get("GEMINI_API_KEY") else []),
    f"GH_TOKEN={get_gh_token()}",
],
```

Only pass `GEMINI_API_KEY` if it's actually set in the environment. The OAuth path doesn't need it.

### Resume / continue

Not supported initially. `--continue` with `--provider gemini` raises `click.UsageError("Gemini provider does not support --continue")`.

Gemini's session resume (`/chat resume`) is an interactive command, not a headless CLI flag.

## Provider Dict

```python
{
    "name": "gemini",
    "build_cmd": lambda task, model, worktree_path: [
        "gemini", "-p", task,
        "--output-format", "json",
        "--yolo",
        *(["--model", model] if model else []),
    ],
    "build_resume_cmd": lambda model, worktree_path: (_ for _ in ()).throw(
        click.UsageError("Gemini provider does not support --continue")
    ),
    "env_vars": lambda: [
        "GEMINI_CLI_HOME=/home/agent",
        *([ f"GEMINI_API_KEY={os.environ['GEMINI_API_KEY']}" ] if os.environ.get("GEMINI_API_KEY") else []),
        f"GH_TOKEN={get_gh_token()}",
    ],
    "volume_mounts": lambda home: [
        f"{home}/.gemini:/home/agent/.gemini",
        f"{home}/.ssh:/home/agent/.ssh:ro",
        f"{home}/.config/gh:/home/agent/.config/gh:ro",
        "pnpm-store:/pnpm-store",
    ],
    "extract_response": extract_gemini_response,
    "auth_check": lambda: None if (
        os.environ.get("GEMINI_API_KEY") or (Path.home() / ".gemini").is_dir()
    ) else "No Gemini auth found. Set GEMINI_API_KEY or run: gemini (to login with Google account)",
}
```

## Gemini Response Extraction

`extract_gemini_response(worktree_path, log_path)`:

1. Read `log_path` line by line.
2. Try to parse each line as JSON.
3. Find the last JSON object that has a `response` key.
4. Return the `response` value.
5. Fall back to the last non-empty non-JSON line (plain text output).
6. Return `None` if nothing found.

The `worktree_path` parameter is accepted for interface consistency but not used (Gemini has no `-o` file output).

## Dockerfile

Add Gemini CLI to the npm install line:

```dockerfile
RUN npm install -g @anthropic-ai/claude-code@latest @openai/codex@latest @google/gemini-cli@latest pnpm
```

## Changes to Existing Code

### Modify

- **`get_provider(name)`** — Add `"gemini"` case returning the provider dict above.
- **`start` command** — Update `click.Choice` from `["claude", "codex"]` to `["claude", "codex", "gemini"]`. Add `gemini` to the codex-style guard: `provider in ("codex", "gemini") and not task and not task_file and not continue_session`.
- **`HELP_TEXT`** — Update `--provider` description to include `gemini`.
- **`Dockerfile`** — Add `@google/gemini-cli@latest` to the npm install line.

### Add

- **`extract_gemini_response(worktree_path, log_path)`** — Parses JSON output for `response` field, falls back to plain text.
- **`import os`** — Add to top of file if not already present (needed for `os.environ.get` in gemini env_vars).

### Keep as-is

- `_collect_and_finalize()` — already provider-aware, no changes needed.
- `read` command — already loads provider from state file, no changes needed.
- `rm`, `ls`, `ports`, `post-exit`, `run_sandbox()`, `sandbox auth`.
- `extract_response()` (Claude), `extract_codex_response()` (Codex).

## Not Included

- Interactive mode for Gemini.
- Auth management commands for Gemini.
- Gemini NDJSON/stream-json parsing (using single JSON `--output-format json` only).
- `--continue` support for Gemini.
- Vertex AI / service account auth.
