#!/usr/bin/env python3
"""Docker Sandbox CLI - Manage sandboxed Claude Code environments with git worktrees."""

import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import click


def run(cmd: list[str], capture: bool = True, check: bool = False) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
    )


@contextmanager
def build_lock(lock_path: Path):
    """Acquire an exclusive file lock for image builds."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def get_config_dir() -> Path:
    """Get the sandbox-cli config directory."""
    return Path.home() / ".config" / "sandbox-cli"


def get_logs_dir() -> Path:
    """Get the logs directory path."""
    return get_config_dir() / "logs"


def get_auth_token() -> str | None:
    """Read the saved Claude auth token."""
    token_file = get_config_dir() / "auth_token"
    if token_file.exists():
        return token_file.read_text().strip()
    return None


def save_auth_token(token: str) -> None:
    """Save a Claude auth token."""
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    token_file = config_dir / "auth_token"
    token_file.write_text(token)
    token_file.chmod(0o600)


def resolve_sandbox(repo_root: Path, name: str, logs_dir: Path | None = None, repo_name: str | None = None) -> dict:
    """Derive all sandbox artifact names/paths from repo root and user-supplied name."""
    sname = safe_name(name)
    if repo_name is None:
        repo_name = repo_root.name
    container = f"sandbox-{repo_name}-{sname}"
    if logs_dir is None:
        logs_dir = get_logs_dir()
    return {
        "sname": sname,
        "repo_name": repo_name,
        "branch": name,
        "container": container,
        "worktree": get_worktree_path(repo_root, sname),
        "log_json": logs_dir / f"{container}.json",
        "log_raw": logs_dir / f"{container}.log",
        "log_err": logs_dir / f"{container}.err",
    }


def extract_response(log_path: Path) -> str | None:
    """Extract response text from Claude's stream-json NDJSON log output.

    Finds the last 'type: result' object, falls back to last assistant message text.
    """
    last_result = None
    last_assistant = None
    for line in log_path.read_text().splitlines():
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") == "result":
            last_result = obj.get("result")
        elif obj.get("type") == "assistant":
            # stream-json format: message.content[0].text
            msg = obj.get("message", {})
            if isinstance(msg, dict):
                content = msg.get("content", [])
                if content and isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_assistant = block.get("text")
            elif isinstance(msg, str):
                last_assistant = msg
    if last_result is not None:
        return last_result
    if last_assistant is not None:
        return last_assistant
    return None


def extract_codex_response(worktree_path: Path, log_path: Path) -> str | None:
    """Extract response text from Codex --json JSONL output.

    Finds the last agent_message item text from item.completed events.
    worktree_path is accepted for interface consistency but not used.
    """
    if not log_path.exists():
        return None

    last_text = None
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if (obj.get("type") == "item.completed"
                    and isinstance(obj.get("item"), dict)
                    and obj["item"].get("type") == "agent_message"):
                last_text = obj["item"].get("text")
        except (json.JSONDecodeError, ValueError):
            continue

    return last_text


def extract_gemini_response(worktree_path: Path, log_path: Path) -> str | None:
    """Extract response text from Gemini CLI JSON output.

    1. Parse log_path as a single JSON object and return the 'response' field.
    2. Fall back to last non-empty, non-JSON line if no 'response' found.
    3. Return None if no content.

    worktree_path is accepted for interface consistency but not used.
    """
    if not log_path.exists():
        return None

    content = log_path.read_text().strip()
    if not content:
        return None

    # Try each line as JSON, look for 'response' key
    last_non_json = None
    for line in content.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue
        try:
            obj = json.loads(line_stripped)
            if isinstance(obj, dict) and "response" in obj:
                return obj["response"]
        except (json.JSONDecodeError, ValueError):
            last_non_json = line_stripped

    # Try the whole content as a single JSON object
    try:
        obj = json.loads(content)
        if isinstance(obj, dict) and "response" in obj:
            return obj["response"]
    except (json.JSONDecodeError, ValueError):
        pass

    return last_non_json


def parse_diff_stats(numstat: str) -> dict:
    """Parse git diff --numstat output into structured stats."""
    files = 0
    insertions = 0
    deletions = 0
    for line in numstat.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[0] == "-":
            continue
        insertions += int(parts[0])
        deletions += int(parts[1])
        files += 1
    return {"filesChanged": files, "insertions": insertions, "deletions": deletions}


def get_repo_root() -> Path | None:
    """Get the main git repository root directory (resolves through worktrees)."""
    result = run(["git", "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    toplevel = Path(result.stdout.strip())
    # If we're inside a worktree, resolve to the main repo root
    git_common = run(["git", "rev-parse", "--git-common-dir"])
    if git_common.returncode == 0:
        common = git_common.stdout.strip()
        if common != ".git":
            # common is an absolute path like /Users/test/myrepo/.git
            return Path(common).parent
    return toplevel


def get_main_git_dir(repo_root: Path) -> Path:
    """Get the main .git directory (handles worktrees)."""
    result = run(["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"])
    git_dir = result.stdout.strip()
    if git_dir == ".git":
        return repo_root / ".git"
    return Path(git_dir)


def safe_name(branch: str) -> str:
    """Convert branch name to safe sandbox name (replace / with -)."""
    return branch.replace("/", "-")


def get_worktrees_dir() -> Path:
    """Get the worktrees directory path."""
    return Path.home() / ".config" / "sandbox-cli" / "worktrees"


def get_worktree_path(repo_root: Path, name: str) -> Path:
    """Get the worktree path for a given name."""
    repo_name = repo_root.name
    return get_worktrees_dir() / f"{repo_name}__{name}"


def docker_container_ls(prefix: str = "sandbox-") -> list[dict]:
    """List all sandbox containers."""
    result = run(["docker", "ps", "-a", "--filter", f"name={prefix}", "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}"])
    if result.returncode != 0:
        return []

    containers = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            containers.append({
                "id": parts[0],
                "name": parts[1],
                "status": parts[2],
            })
    return containers


def docker_container_rm(name: str) -> bool:
    """Remove a docker container by name."""
    result = run(["docker", "rm", "-f", name])
    return result.returncode == 0


def git_worktree_list() -> list[dict]:
    """List all git worktrees."""
    result = run(["git", "worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return []

    worktrees = []
    current = {}
    for line in result.stdout.strip().split("\n"):
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line[9:]}
        elif line.startswith("branch "):
            current["branch"] = line[7:]
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
    if current:
        worktrees.append(current)

    return worktrees


def git_worktree_add(path: Path, branch: str, new_branch: bool = False) -> bool:
    """Add a git worktree."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "worktree", "add", str(path)]
    if new_branch:
        cmd.extend(["-b", branch])
    else:
        cmd.append(branch)
    result = run(cmd, capture=False)
    return result.returncode == 0


def git_worktree_remove(path: Path, force: bool = False) -> bool:
    """Remove a git worktree."""
    cmd = ["git", "worktree", "remove", str(path)]
    if force:
        cmd.append("--force")
    result = run(cmd)
    return result.returncode == 0


def copy_env_files(src: Path, dest: Path) -> list[str]:
    """Copy .env* files from src to dest directory."""
    import shutil
    copied = []
    for env_file in src.glob(".env*"):
        if env_file.is_file():
            shutil.copy2(env_file, dest / env_file.name)
            copied.append(env_file.name)
    return copied


def get_sandbox_cli_dir() -> Path:
    """Get the sandbox-cli installation directory."""
    return Path(__file__).parent


def _build_lock_path() -> Path:
    """Get the path for the build lock file."""
    return Path.home() / ".config" / "sandbox-cli" / "build.lock"


def ensure_default_image() -> str:
    """Ensure the default sandbox image exists, build if needed."""
    image_name = "sandbox-cli:default"

    with build_lock(_build_lock_path()):
        # Check if image exists (inside lock to avoid TOCTOU race)
        result = run(["docker", "image", "inspect", image_name])
        if result.returncode == 0:
            return image_name

        # Build default image
        dockerfile = get_sandbox_cli_dir() / "Dockerfile"
        if not dockerfile.exists():
            click.echo(f"Default Dockerfile not found: {dockerfile}", err=True)
            sys.exit(1)

        click.echo("Building default sandbox image...", err=True)
        result = run(
            ["docker", "build", "-t", image_name, "-f", str(dockerfile), str(get_sandbox_cli_dir())],
            capture=False,
        )
        if result.returncode != 0:
            click.echo("Failed to build default image", err=True)
            sys.exit(1)

    return image_name


def build_template_if_exists(repo_root: Path) -> str:
    """Build custom template if Dockerfile.sandbox exists, otherwise use default."""
    # Always ensure default image exists (custom Dockerfiles may use FROM sandbox-cli:default)
    ensure_default_image()

    dockerfile = repo_root / "Dockerfile.sandbox"
    if not dockerfile.exists():
        return "sandbox-cli:default"

    image_name = f"sandbox-template:{repo_root.name}"
    with build_lock(_build_lock_path()):
        click.echo("Building project template...", err=True)
        result = run(
            ["docker", "build", "-t", image_name, "-f", str(dockerfile), str(repo_root)],
            capture=False,
        )
        if result.returncode != 0:
            click.echo("Failed to build project template", err=True)
            sys.exit(1)
    return image_name


def get_gh_token() -> str:
    """Get GitHub token: $GH_TOKEN env var first, then gh CLI fallback."""
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token
    result = run(["gh", "auth", "token"])
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def get_provider(name: str) -> dict:
    """Return provider config dict for the given provider name.

    Raises click.UsageError for unknown providers.
    """
    if name == "claude":
        return {
            "name": "claude",
            "build_cmd": lambda task, model, worktree_path: [
                "claude", "-p", task, "--print", "--verbose",
                "--output-format", "stream-json", "--dangerously-skip-permissions",
                *(["--model", model] if model else []),
            ],
            "build_resume_cmd": lambda model, worktree_path: [
                "claude", "--continue", "--print", "--verbose",
                "--output-format", "stream-json", "--dangerously-skip-permissions",
                *(["--model", model] if model else []),
            ],
            "env_vars": lambda: [
                f"CLAUDE_CODE_OAUTH_TOKEN={get_auth_token()}",
                f"GH_TOKEN={get_gh_token()}",
                "CLAUDE_CONFIG_DIR=/home/agent/.claude",
            ],
            "volume_mounts": lambda home: [
                f"{home}/.claude:/home/agent/.claude",
                f"{home}/.ssh:/home/agent/.ssh:ro",
                f"{home}/.config/gh:/home/agent/.config/gh:ro",
                "pnpm-store:/pnpm-store",
            ],
            "extract_response": lambda worktree_path, log_path: extract_response(log_path),
            "auth_check": lambda: None if get_auth_token() else
                "No auth token configured. Run: claude setup-token && sandbox auth <token>",
        }
    elif name == "codex":
        return {
            "name": "codex",
            "build_cmd": lambda task, model, worktree_path: [
                "codex", "exec", "--yolo", "--json",
                *(["--model", model] if model else []),
                task,
            ],
            "build_resume_cmd": lambda model, worktree_path: (_ for _ in ()).throw(
                click.UsageError("Codex provider does not support --continue")
            ),
            "env_vars": lambda: [
                "CODEX_HOME=/home/agent/.codex",
                f"GH_TOKEN={get_gh_token()}",
            ],
            "volume_mounts": lambda home: [
                f"{home}/.codex:/home/agent/.codex",
                f"{home}/.ssh:/home/agent/.ssh:ro",
                f"{home}/.config/gh:/home/agent/.config/gh:ro",
                "pnpm-store:/pnpm-store",
            ],
            "extract_response": extract_codex_response,
            "auth_check": lambda: None if (Path.home() / ".codex" / "auth.json").exists() else
                "No Codex auth found. Run: codex login",
        }
    elif name == "gemini":
        return {
            "name": "gemini",
            "build_cmd": lambda task, model, worktree_path: [
                "gemini", "-p", task, "--output-format", "json", "--yolo",
                *(["--model", model] if model else []),
            ],
            "build_resume_cmd": lambda model, worktree_path: (_ for _ in ()).throw(
                click.UsageError("Gemini provider does not support --continue")
            ),
            "env_vars": lambda: [
                "GEMINI_CLI_HOME=/home/agent",
                f"GH_TOKEN={get_gh_token()}",
                *(
                    [f"GEMINI_API_KEY={os.environ['GEMINI_API_KEY']}"]
                    if os.environ.get("GEMINI_API_KEY")
                    else []
                ),
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
            ) else
                "No Gemini auth found. Set GEMINI_API_KEY or run: gemini (to login with Google account)",
        }
    else:
        raise click.UsageError(f"Unknown provider: {name}. Choose from: claude, codex, gemini")


def find_available_ports(count: int = 3, start: int = 49152, end: int = 65535) -> list[int]:
    """Find available ports in the dynamic/private port range."""
    import socket
    ports = []
    for port in range(start, end):
        if len(ports) >= count:
            break
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", port))
                ports.append(port)
        except OSError:
            continue
    return ports


def container_exists(name: str) -> bool:
    """Check if a docker container exists."""
    result = run(["docker", "container", "inspect", name])
    return result.returncode == 0


def container_running(name: str) -> bool:
    """Check if a docker container is running."""
    result = run(["docker", "container", "inspect", "-f", "{{.State.Running}}", name])
    return result.returncode == 0 and result.stdout.strip() == "true"


def run_sandbox_background(
    name: str,
    repo_root: Path,
    repo_name: str,
    main_git: Path,
    image: str,
    task: str | None,
    logs_dir: Path,
    model: str | None = None,
    push: bool = False,
    cleanup: bool = False,
    continue_session: bool = False,
    provider: str = "claude",
    extra_mounts: list[str] | None = None,
) -> dict:
    """Run a sandbox task in background mode. Returns result dict."""
    sb = resolve_sandbox(repo_root, name, logs_dir=logs_dir, repo_name=repo_name)
    sname = sb["sname"]
    container_name = sb["container"]
    worktree_path = sb["worktree"]
    log_file = sb["log_json"]
    error_result = {"container": container_name, "name": sname, "branch": name}

    if continue_session:
        # Resume: container must exist
        if not container_exists(container_name):
            return {**error_result, "error": f"No container to resume: {container_name}"}

        # Load provider from state file (overrides CLI flag)
        state_provider = "claude"
        if log_file.exists():
            try:
                state = json.loads(log_file.read_text())
                state_provider = state.get("provider", "claude")
            except (json.JSONDecodeError, OSError):
                pass
        if provider != state_provider:
            click.echo(
                f"Warning: --provider flag ignored for --continue; "
                f"using provider from state file: {state_provider}",
                err=True,
            )

        resume_provider = get_provider(state_provider)

        base_result = run(["git", "-C", str(worktree_path), "rev-parse", "HEAD"])
        base_commit = base_result.stdout.strip()

        agent_cmd = resume_provider["build_resume_cmd"](model, worktree_path)

        # Start container if stopped, then exec
        if not container_running(container_name):
            run(["docker", "start", container_name])
        exec_result = run(["docker", "exec", container_name] + agent_cmd)

        result = _collect_and_finalize(sb, exec_result.returncode, base_commit, repo_root, name, push=push, cleanup=cleanup, provider=resume_provider)
        return result

    # New task: check for conflicts
    if container_exists(container_name):
        return {**error_result, "error": f"Container already exists: {container_name}"}
    if branch_exists(name):
        return {**error_result, "error": f"Branch already exists: {name}"}
    if worktree_path.exists():
        return {**error_result, "error": f"Worktree already exists: {worktree_path}"}
    if log_file.exists():
        return {**error_result, "error": f"Log file already exists: {log_file}"}

    # Auth check BEFORE worktree creation (fail fast without needing cleanup)
    task_provider = get_provider(provider)
    auth_error = task_provider["auth_check"]()
    if auth_error:
        return {**error_result, "error": auth_error}

    # Create worktree
    if not git_worktree_add(worktree_path, name, new_branch=True):
        return {**error_result, "error": "Failed to create worktree"}

    # Record base commit for stable diff stats
    base_result = run(["git", "-C", str(worktree_path), "rev-parse", "HEAD"])
    base_commit = base_result.stdout.strip()

    # Write state file (name reservation + recovery metadata)
    logs_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "status": "running",
        "provider": provider,
        "container": container_name,
        "name": sname,
        "branch": name,
        "worktreePath": str(worktree_path),
        "baseCommit": base_commit,
    }
    log_file.write_text(json.dumps(state))

    # Copy .env files
    copy_env_files(repo_root, worktree_path)

    # Launch container detached
    home = Path.home()
    agent_cmd = task_provider["build_cmd"](task, model, worktree_path)

    docker_cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "-v", f"{worktree_path}:{worktree_path}",
        "-v", f"{main_git}:{main_git}",
    ]
    for mount in task_provider["volume_mounts"](home):
        docker_cmd.extend(["-v", mount])
    for mount in (extra_mounts or []):
        docker_cmd.extend(["-v", mount])
    for env_var in task_provider["env_vars"]():
        docker_cmd.extend(["-e", env_var])
    docker_cmd.extend([
        "-w", str(worktree_path),
        image,
    ])
    docker_cmd.extend(agent_cmd)

    launch = run(docker_cmd)
    if launch.returncode != 0:
        git_worktree_remove(worktree_path, force=True)
        run(["git", "branch", "-D", name])
        log_file.unlink(missing_ok=True)
        return {**error_result, "error": f"Failed to launch container: {launch.stderr}"}

    # Wait
    wait_result = run(["docker", "wait", container_name])
    try:
        exit_code = int(wait_result.stdout.strip())
    except (ValueError, AttributeError):
        exit_code = 1

    result = _collect_and_finalize(sb, exit_code, base_commit, repo_root, name, push=push, cleanup=cleanup, provider=task_provider)
    return result


def _collect_and_finalize(sb: dict, exit_code: int, base_commit: str, repo_root: Path, name: str,
                          push: bool = False, cleanup: bool = False, provider: dict | None = None) -> dict:
    """Shared post-wait finalization: save logs, commit, optionally push/cleanup, build result."""
    if provider is None:
        provider = get_provider("claude")
    container_name = sb["container"]
    sname = sb["sname"]
    worktree_path = sb["worktree"]
    log_file = sb["log_json"]

    # Save raw logs (stdout and stderr separately)
    log_result = run(["docker", "logs", container_name])
    sb["log_raw"].parent.mkdir(parents=True, exist_ok=True)
    sb["log_raw"].write_text(log_result.stdout)
    sb["log_err"].write_text(log_result.stderr)

    # Commit
    commit_succeeded = False
    nothing_to_commit = False
    run(["git", "-C", str(worktree_path), "add", "-A"])
    run(["git", "-C", str(worktree_path), "reset", "HEAD", "--", ".env*", ".sandbox-result.txt"])

    status = run(["git", "-C", str(worktree_path), "status", "--porcelain"])
    if not status.stdout.strip():
        nothing_to_commit = True
        commit_succeeded = True
    else:
        commit_result = run(["git", "-C", str(worktree_path), "commit", "-m", f"sandbox: {sname}"])
        commit_succeeded = commit_result.returncode == 0

    # Get modified files list from diff
    modified_files = []
    if base_commit:
        diff_files = run(["git", "-C", str(worktree_path) if worktree_path.exists() else str(repo_root),
                          "diff", "--name-only", base_commit, name])
        if diff_files.returncode == 0:
            modified_files = [f for f in diff_files.stdout.strip().splitlines() if f]

    # Push (only if requested and successful)
    pushed = False
    if push and exit_code == 0 and commit_succeeded and not nothing_to_commit:
        push_result = run(["git", "-C", str(worktree_path), "push", "-u", "origin", name])
        pushed = push_result.returncode == 0

    # Always remove the container
    docker_container_rm(container_name)

    # Only remove worktree if explicitly requested
    if cleanup and commit_succeeded:
        git_worktree_remove(worktree_path, force=True)

    # Extract response text via provider
    response = provider["extract_response"](worktree_path, sb["log_raw"])

    result = {
        "container": container_name,
        "name": sname,
        "branch": name,
        "exitCode": exit_code,
        "worktreePath": str(worktree_path),
        "modifiedFiles": modified_files,
        "provider": provider["name"],
        "logPath": str(sb["log_raw"]),
        "logErrPath": str(sb["log_err"]),
    }

    result["pushed"] = pushed

    if commit_succeeded and not nothing_to_commit:
        commit_sha_result = run(["git", "-C", str(worktree_path) if worktree_path.exists() else str(repo_root), "rev-parse", name])
        if commit_sha_result.returncode == 0:
            result["commitSha"] = commit_sha_result.stdout.strip()
        if base_commit:
            diff_result = run(["git", "-C", str(repo_root), "diff", "--numstat", base_commit, name])
            result["diffStats"] = parse_diff_stats(diff_result.stdout)

    if response:
        result["response"] = response

    if exit_code != 0:
        result["error"] = "Unknown error, check logErrPath for details"

    if not commit_succeeded and not nothing_to_commit:
        result["error"] = f"Commit failed, worktree preserved at {worktree_path}"

    # Save result (overwrites state file)
    log_file.write_text(json.dumps(result))

    return result


def get_modal_image():
    """Build (or return cached) Modal image with Node.js, Claude Code, Codex, and Gemini CLI."""
    try:
        import modal
    except ImportError:
        click.echo("modal package not installed. Run: pip install modal", err=True)
        sys.exit(1)
    return (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install("ca-certificates", "curl", "git", "jq", "build-essential")
        .run_commands(
            # Install Node.js 20
            "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
            "apt-get install -y nodejs",
            # Install Claude Code and agent CLIs
            "npm install -g @anthropic-ai/claude-code @openai/codex @google/gemini-cli",
        )
    )


def run_sandbox_remote(
    name: str,
    repo_root: Path,
    repo_name: str,
    task: str,
    logs_dir: Path,
    model: str | None = None,
    provider: str = "claude",
) -> dict:
    """Run a background sandbox task on Modal. Returns result dict."""
    import re

    # Lazy import Modal — only required when --remote is used
    try:
        import modal
        from modal.exception import (
            ExecTimeoutError,
            NotFoundError,
            SandboxTerminatedError,
            SandboxTimeoutError,
        )
    except ImportError:
        return {"error": "modal package not installed. Run: pip install modal"}

    sb_info = resolve_sandbox(repo_root, name, logs_dir=logs_dir, repo_name=repo_name)
    sname = sb_info["sname"]
    branch = name
    log_json = sb_info["log_json"]
    log_raw = sb_info["log_raw"]
    error_result = {"name": sname, "branch": branch, "runtime": "modal"}

    # --- Auth checks ---
    # 1. Modal authentication
    try:
        app = modal.App.lookup("sandbox-cli", create_if_missing=True)
    except Exception as exc:
        return {**error_result, "error": f"Modal auth failed: {exc}. Run: modal setup"}

    # 2. Provider auth
    task_provider = get_provider(provider)
    auth_error = task_provider["auth_check"]()
    if auth_error:
        return {**error_result, "error": auth_error}

    # 3. GH_TOKEN required for clone/push
    gh_token = get_gh_token()
    if not gh_token:
        return {**error_result, "error": "GH_TOKEN is required for --remote. Set $GH_TOKEN or run: gh auth login"}

    # --- Existing remote branch check ---
    if log_json.exists():
        return {**error_result, "error": f"State file already exists: {log_json}"}

    # Fetch and check remote branch
    run(["git", "fetch", "--quiet"])
    if run(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"]).returncode == 0:
        return {**error_result, "error": f"Remote branch already exists: {branch}"}

    # --- Resolve repo URL ---
    remote_url_result = run(["git", "remote", "get-url", "origin"])
    if remote_url_result.returncode != 0:
        return {**error_result, "error": "Could not get git remote URL for origin"}
    repo_url = remote_url_result.stdout.strip()

    # --- Create remote branch (must succeed before sandbox creation) ---
    push_result = run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"])
    if push_result.returncode != 0:
        return {**error_result, "error": f"Failed to create remote branch: {push_result.stderr.strip()}"}

    # --- Record base commit ---
    base_commit_result = run(["git", "rev-parse", "HEAD"])
    base_commit = base_commit_result.stdout.strip() if base_commit_result.returncode == 0 else ""

    # --- Write initial state file ---
    logs_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "status": "running",
        "runtime": "modal",
        "provider": provider,
        "name": sname,
        "branch": branch,
        "sandboxId": None,
        "baseCommit": base_commit,
    }
    log_json.write_text(json.dumps(state))

    # --- Build provider command ---
    worktree_path = Path("/workspace")
    agent_cmd = task_provider["build_cmd"](task, model, worktree_path)

    # --- Build secrets dict ---
    secrets_dict: dict[str, str] = {"GH_TOKEN": gh_token}
    if provider == "claude":
        auth_token = get_auth_token()
        if auth_token:
            secrets_dict["CLAUDE_CODE_OAUTH_TOKEN"] = auth_token
    elif provider == "gemini":
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        if gemini_key:
            secrets_dict["GEMINI_API_KEY"] = gemini_key

    # --- Get runner script path ---
    runner_script = get_sandbox_cli_dir() / "scripts" / "modal_runner.sh"

    sb = None
    try:
        # --- Create sandbox ---
        image = get_modal_image()
        sb = modal.Sandbox.create(
            app=app,
            image=image,
            timeout=7200,
            idle_timeout=600,
            cpu=2.0,
            memory=4096,
            secrets=[modal.Secret.from_dict(secrets_dict)],
        )

        # Update state file with sandbox ID
        state["sandboxId"] = sb.object_id
        log_json.write_text(json.dumps(state))

        click.echo(f"Modal sandbox created: {sb.object_id}", err=True)

        # Upload runner script to sandbox
        runner_content = runner_script.read_bytes()
        with sb.open("/tmp/modal_runner.sh", "wb") as f:
            f.write(runner_content)

        # Build exec args: runner script + repo_url + branch + agent_cmd
        exec_args = ["bash", "/tmp/modal_runner.sh", repo_url, branch] + agent_cmd

        # --- Exec runner with pty=False for clean NDJSON output ---
        proc = sb.exec(*exec_args, pty=False)

        # --- Stream stdout: tee to terminal and write incrementally to log file ---
        log_raw.parent.mkdir(parents=True, exist_ok=True)
        captured_chunks = []
        ansi_re = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

        with log_raw.open("w", encoding="utf-8") as log_file:
            for chunk in proc.stdout:
                sys.stderr.write(chunk)
                sys.stderr.flush()
                log_file.write(chunk)
                log_file.flush()
                captured_chunks.append(chunk)

        exit_code = proc.wait()

    except SandboxTimeoutError:
        exit_code = 1
        result_error = "Modal sandbox timed out"
        click.echo(f"Error: {result_error}", err=True)
        result = {**error_result, "exitCode": exit_code, "error": result_error}
        log_json.write_text(json.dumps(result))
        return result
    except SandboxTerminatedError:
        exit_code = 1
        result_error = "Modal sandbox terminated unexpectedly (possible OOM)"
        click.echo(f"Error: {result_error}", err=True)
        result = {**error_result, "exitCode": exit_code, "error": result_error}
        log_json.write_text(json.dumps(result))
        return result
    except ExecTimeoutError:
        exit_code = 1
        result_error = "Modal exec timed out"
        click.echo(f"Error: {result_error}", err=True)
        result = {**error_result, "exitCode": exit_code, "error": result_error}
        log_json.write_text(json.dumps(result))
        return result
    except Exception as exc:
        exit_code = 1
        result_error = f"Modal error: {exc}"
        click.echo(f"Error: {result_error}", err=True)
        result = {**error_result, "exitCode": exit_code, "error": result_error}
        log_json.write_text(json.dumps(result))
        return result
    finally:
        if sb is not None:
            try:
                sb.terminate(wait=True)
            except Exception:
                pass
            try:
                sb.detach()
            except Exception:
                pass

    # --- Parse __SANDBOX_RESULT__ marker from captured output ---
    full_log = "".join(captured_chunks)
    sandbox_result: dict = {}
    for line in full_log.splitlines():
        clean = ansi_re.sub("", line)
        if clean.startswith("__SANDBOX_RESULT__"):
            payload = clean[len("__SANDBOX_RESULT__"):].strip()
            try:
                sandbox_result = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                pass

    # --- Extract provider response from log file ---
    response = task_provider["extract_response"](worktree_path, log_raw)

    # --- Build result ---
    result: dict = {
        "name": sname,
        "branch": branch,
        "runtime": "modal",
        "provider": provider,
        "exitCode": sandbox_result.get("exitCode", exit_code),
        "modifiedFiles": sandbox_result.get("modifiedFiles", []),
        "pushed": sandbox_result.get("pushed", False),
    }

    commit_sha = sandbox_result.get("commitSha", "")
    if commit_sha:
        result["commitSha"] = commit_sha

    diff_stats = sandbox_result.get("diffStats")
    if diff_stats:
        result["diffStats"] = diff_stats

    if response:
        result["response"] = response

    if exit_code != 0:
        result["error"] = sandbox_result.get("error") or f"{provider} exited with code {exit_code}"
    elif sandbox_result.get("error"):
        result["error"] = sandbox_result["error"]

    # --- Write final state file ---
    log_json.write_text(json.dumps(result))

    return result


def run_sandbox(name: str, repo_name: str, main_git: Path, worktree_path: Path, template: str | None = None, extra_mounts: list[str] | None = None) -> None:
    """Launch interactive sandbox session, then start cleanup Claude on exit."""
    home = Path.home()
    image = template or ensure_default_image()
    container_name = f"sandbox-{repo_name}-{name}"
    repo_root = main_git.parent

    claude_cmd = ["claude", "--dangerously-skip-permissions"]

    # Check if container already exists - session lives in the container
    if container_exists(container_name):
        claude_cmd.append("--continue")
        if container_running(container_name):
            # Exec into running container
            cmd_parts = ["docker", "exec", "-it", container_name] + claude_cmd
        else:
            # Start stopped container, then exec
            run(["docker", "start", container_name], capture=False)
            cmd_parts = ["docker", "exec", "-it", container_name] + claude_cmd
    else:
        # Build .claude.json from host config, overlaying sandbox requirements
        import tempfile
        host_claude_json = home / ".claude.json"
        claude_json = json.loads(host_claude_json.read_text()) if host_claude_json.exists() else {}
        claude_json["hasCompletedOnboarding"] = True
        projects = claude_json.setdefault("projects", {})
        projects[str(worktree_path)] = {
            **projects.get(str(worktree_path), {}),
            "hasTrustDialogAccepted": True,
            "hasCompletedProjectOnboarding": True,
        }
        claude_json_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="claude-sandbox-", delete=False
        )
        json.dump(claude_json, claude_json_file)
        claude_json_file.close()

        # Create new container - fresh session
        cmd_parts = [
            "docker", "run", "-it",
            "--name", container_name,
            "-v", f"{worktree_path}:{worktree_path}",
            "-v", f"{main_git}:{main_git}",
            "-v", f"{home}/.claude:/home/agent/.claude",
            "-v", f"{claude_json_file.name}:/home/agent/.claude.json",
            "-v", f"{home}/.config/gh:/home/agent/.config/gh:ro",
            "-e", f"CLAUDE_CODE_OAUTH_TOKEN={get_auth_token()}",
            "-e", f"GH_TOKEN={get_gh_token()}",
            "-e", "CLAUDE_CONFIG_DIR=/home/agent/.claude",
            "-e", "FORCE_COLOR=1",
            "-e", "COLORTERM=truecolor",
            "-e", "npm_config_store_dir=/pnpm-store",
            "-v", f"{home}/.ssh:/home/agent/.ssh:ro",
            "-v", "pnpm-store:/pnpm-store",
        ]
        for mount in (extra_mounts or []):
            cmd_parts.extend(["-v", mount])
        # Add dynamic port mappings
        ports = find_available_ports(3)
        for port in ports:
            cmd_parts.extend(["-p", f"{port}:{port}"])
        ports_prompt = f"You are running in a sandbox. Available ports for dev servers: {', '.join(map(str, ports))}. When starting dev servers, use --port {ports[0]} --host 0.0.0.0 (host binding required for port forwarding)."
        cmd_parts.extend([
            "-e", f"SANDBOX_PORTS={','.join(map(str, ports))}",
            "-w", str(worktree_path),
            image,
        ])
        cmd_parts.extend(claude_cmd)
        cmd_parts.extend(["--append-system-prompt", ports_prompt])

    # Fork so child gets PTY via execvp (clean signal handling), parent waits for cleanup
    import signal
    pid = os.fork()
    if pid == 0:
        os.execvp(cmd_parts[0], cmd_parts)
    else:
        # Parent ignores SIGINT — only docker/claude should handle ctrl-C
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        _, status = os.waitpid(pid, 0)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Gather worktree status: uncommitted changes + committed changes vs main
    status_result = run(["git", "-C", str(worktree_path), "status", "--short"])
    log_result = run(["git", "-C", str(worktree_path), "log", "main.." + name, "--oneline"])
    diff_stat_result = run(["git", "-C", str(worktree_path), "diff", "--stat", "main.." + name])
    uncommitted = status_result.stdout.strip()
    commits = log_result.stdout.strip()
    diff_stats = diff_stat_result.stdout.strip()

    has_changes = uncommitted or commits

    if not has_changes:
        # No changes — clean up silently
        click.echo("No changes detected, cleaning up...", err=True)
        docker_container_rm(container_name)
        git_worktree_remove(worktree_path, force=True)
        run(["git", "branch", "-D", name])
        click.echo(f"Sandbox {name} removed.", err=True)
        return

    # Build a summary for Claude
    summary_parts = []
    if commits:
        summary_parts.append(f"Commits:\n{commits}")
    if diff_stats:
        summary_parts.append(f"Diff stats:\n{diff_stats}")
    if uncommitted:
        summary_parts.append(f"Uncommitted:\n{uncommitted}")
    summary = "\n\n".join(summary_parts)

    # Changes exist — launch Claude for integration
    cleanup_prompt = f"""\
Sandbox "{name}" has changes to integrate. Present the summary below, then \
merge into main. Do NOT run git diff or git log — everything you need is here.

{summary}

Your job:
1. Present the summary above to the user.
2. Ask: merge into main, or create a PR? (Two options only.)
3. If there are uncommitted changes, commit them first:
   git -C {worktree_path} add -A && git -C {worktree_path} commit -m "<summarize all changes on the branch>"
4. To merge: cd {repo_root} && git merge {name} --no-ff
   To PR: git -C {worktree_path} push -u origin {name} && gh pr create
5. After integration, clean up automatically — no confirmation needed:
   docker rm -f {container_name}
   git worktree remove {worktree_path}
   git branch -D {name}"""
    os.execvp("claude", ["claude", cleanup_prompt])


HELP_TEXT = """\
Sandbox CLI - run Claude Code in isolated Docker containers with git worktrees

SETUP:
  1. Run `claude setup-token` and follow the instructions to get a token
  2. Run `sandbox auth <token>` to save it
  Token is stored at ~/.config/sandbox-cli/auth_token

COMMANDS:
  start    Start a sandbox (interactive or background task)
           <name>                  Sandbox name (auto-generated if omitted)
           --task <prompt>         Run as background task (non-interactive)
           --task-file <path>      Read prompt from file (mutually exclusive with --task)
           --model <model>         Model to pass to the agent (e.g. haiku, sonnet, gpt-4.1)
           --provider <name>       Agent provider: claude (default), codex, or gemini (background only)
           --mount <host:dst[:ro]> Extra volume mount, repeatable
           --push                  Push branch to origin after successful commit
           --cleanup               Remove worktree after completion
           --remote                Run background task on Modal (no local Docker required)
           Interactive: launches Docker container with Claude CLI
           Background:  commits changes, returns JSON to stdout
           Remote:      clones repo inside Modal sandbox, pushes results from cloud

  read     Read results from a background task
           <name>                  Sandbox name (same as used with start)
           Returns saved result, or recovers from running/exited container

  rm       Remove a sandbox and all its artifacts
           <name>                  Sandbox name to remove
           --all                   Remove all sandboxes for current repo
           --yes, -y               Skip prompts and auto-delete branch
           --force                 Allow removing a running task (kills the container)
           Removes: container, worktree, log files, branch (with --yes or prompt)

  auth     Save Claude authentication token for sandbox containers
           <token>                 Token from `claude setup-token` (omit to check status)

  ls       List worktrees and containers for current repo

  ports    Show available ports for a sandbox
           <name>                  Sandbox name

  docs     Show documentation guides
           prompt-guide            How to write sandbox task prompts
           operations-guide        Running sandboxes, integrating results, failure modes

OUTPUT (background mode):
  All results include: container, name, branch, exitCode, worktreePath, modifiedFiles
  On commit:  + response, diffStats, commitSha
  On push:    + pushed: true
  On failure: + error
  All progress goes to stderr. Only final JSON goes to stdout.

INSTRUCTIONS FOR CLAUDE:
  Before first use, read the operations guide:  sandbox docs operations-guide
  Before writing a task prompt, read the prompt guide:  sandbox docs prompt-guide

  Run background tasks with run_in_background. They block until complete,
  so you will be notified when done. Do NOT poll or sleep.

  Workflow:
    1. sandbox start proto-1 --task-file prompts/proto-1.md --model sonnet
    2. Notified with JSON result containing worktreePath and modifiedFiles
    3. Read the changes from worktreePath to review the work
    4. sandbox rm proto-1 --yes  (when done, cleans up worktree + branch)

  The worktree is preserved after completion so you can inspect the changes.
  Use --push to also push the branch to origin.
  Use --cleanup to remove the worktree automatically.
  Use --task-file for large prompts that exceed shell argument limits.
  Use sandbox read <name> to recover results if start timed out.
"""


class CustomGroup(click.Group):
    def format_help(self, ctx, formatter):
        formatter.write(HELP_TEXT)


@click.group(cls=CustomGroup, invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Sandbox CLI - run Claude Code in isolated Docker containers."""
    if ctx.invoked_subcommand is None:
        # No subcommand given, invoke start with generated name
        ctx.invoke(start)


def branch_exists(branch: str) -> bool:
    """Check if a local branch exists."""
    result = run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    return result.returncode == 0


def get_worktree_for_branch(branch: str) -> Path | None:
    """Get the worktree path for a branch if it's already checked out."""
    for wt in git_worktree_list():
        wt_branch = wt.get("branch", "").replace("refs/heads/", "")
        if wt_branch == branch:
            return Path(wt["path"])
    return None


def remote_branch_exists(branch: str) -> bool:
    """Check if a remote branch exists."""
    # Fetch first to ensure we have latest refs
    run(["git", "fetch", "--quiet"])
    result = run(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"])
    return result.returncode == 0


def sandbox_exists(repo_name: str, name: str) -> bool:
    """Check if a sandbox container exists."""
    return container_exists(f"sandbox-{repo_name}-{name}")


def generate_sandbox_name() -> str:
    """Generate a random sandbox name."""
    import random
    adjectives = ["quick", "bright", "calm", "bold", "swift", "keen", "warm", "cool", "fair", "wise"]
    nouns = ["fox", "owl", "bear", "wolf", "hawk", "deer", "hare", "lynx", "seal", "wren"]
    return f"{random.choice(adjectives)}-{random.choice(nouns)}"


@cli.command()
@click.argument("token", required=False)
def auth(token):
    """Save or show Claude authentication token for sandbox containers."""
    if token:
        save_auth_token(token)
        click.echo("Token saved.", err=True)
    else:
        existing = get_auth_token()
        if existing:
            click.echo("Auth token is configured.", err=True)
        else:
            click.echo("No auth token configured. Run: claude setup-token", err=True)
            click.echo("Then: sandbox auth <token>", err=True)
            sys.exit(1)


@cli.command(name="start")
@click.argument("name", required=False)
@click.option("--continue", "continue_session", is_flag=True, help="Resume a crashed background task.")
@click.option("--task", default=None, help="Run in background mode with this prompt.")
@click.option("--task-file", default=None, type=click.Path(exists=True), help="Read prompt from file.")
@click.option("--model", default=None, help="Model to pass to the agent CLI.")
@click.option("--push", is_flag=True, help="Push branch to origin after successful commit.")
@click.option("--cleanup", is_flag=True, help="Remove worktree and container after completion.")
@click.option("--provider", "provider", default="claude",
              type=click.Choice(["claude", "codex", "gemini"]),
              help="Agent provider to use for background tasks (default: claude).")
@click.option("--mount", "extra_mounts", multiple=True,
              help="Extra volume mount (host:container[:ro]). Repeatable.")
@click.option("--remote", is_flag=True, help="Run background task on Modal instead of local Docker.")
def start(name, continue_session, task, task_file, model, push, cleanup, provider, extra_mounts, remote):
    """Start a sandbox (creates if new, resumes if exists)."""
    if remote and not (task or task_file):
        click.echo("--remote only supports background task mode (use --task or --task-file)", err=True)
        sys.exit(1)

    if remote and continue_session:
        click.echo("--remote is incompatible with --continue", err=True)
        sys.exit(1)

    if continue_session and (task or task_file):
        click.echo("Cannot specify both --continue and --task/--task-file", err=True)
        sys.exit(1)

    if task is not None and task_file:
        click.echo("Cannot specify both --task and --task-file", err=True)
        sys.exit(1)

    if task_file:
        task = Path(task_file).read_text()

    if task is not None and not task.strip():
        click.echo("Task prompt cannot be empty", err=True)
        sys.exit(1)

    if continue_session and not name:
        click.echo("--continue requires a sandbox name", err=True)
        sys.exit(1)

    if not get_auth_token():
        click.echo("No auth token configured.", err=True)
        click.echo("Run: claude setup-token", err=True)
        click.echo("Then: sandbox auth <token>", err=True)
        sys.exit(1)

    # Codex and Gemini only support background task mode
    if provider in ("codex", "gemini") and not task and not task_file and not continue_session:
        click.echo(f"{provider.capitalize()} provider only supports background task mode", err=True)
        sys.exit(1)

    if not name:
        name = generate_sandbox_name()
        click.echo(f"Generated name: {name}", err=True)

    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    if remote and task:
        # Remote (Modal) background mode
        result = run_sandbox_remote(
            name=name,
            repo_root=repo_root,
            repo_name=repo_root.name,
            task=task,
            logs_dir=get_logs_dir(),
            model=model,
            provider=provider,
        )
        click.echo(json.dumps(result))
        return

    if task or continue_session:
        # Local Docker background mode
        main_git = get_main_git_dir(repo_root)
        image = build_template_if_exists(repo_root)
        result = run_sandbox_background(
            name=name,
            repo_root=repo_root,
            repo_name=repo_root.name,
            main_git=main_git,
            image=image,
            task=task,
            logs_dir=get_logs_dir(),
            model=model,
            push=push,
            cleanup=cleanup,
            continue_session=continue_session,
            provider=provider,
            extra_mounts=list(extra_mounts),
        )
        click.echo(json.dumps(result))
        return

    sname = safe_name(name)
    repo_name = repo_root.name
    worktree_path = get_worktree_path(repo_root, sname)
    main_git = get_main_git_dir(repo_root)

    if worktree_path.exists() and sandbox_exists(repo_name, sname):
        # Existing sandbox - resume session
        click.echo(f"Resuming sandbox: {sname}", err=True)
        template = build_template_if_exists(repo_root)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template, extra_mounts=list(extra_mounts))
    elif worktree_path.exists():
        # Worktree exists but no sandbox - start fresh
        template = build_template_if_exists(repo_root)
        click.echo(f"Starting sandbox: {sname}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template, extra_mounts=list(extra_mounts))
    elif branch_exists(name):
        # Existing local branch - check if already in a worktree
        existing_wt = get_worktree_for_branch(name)
        if existing_wt:
            # Branch already checked out - use existing worktree
            click.echo(f"Using existing worktree for branch: {name}", err=True)
            template = build_template_if_exists(repo_root)
            # Extract sandbox name from worktree path
            wt_sname = existing_wt.name.split("__")[-1] if "__" in existing_wt.name else sname
            run_sandbox(wt_sname, repo_name, main_git, existing_wt, template=template)
        else:
            # Create worktree from existing branch
            template = build_template_if_exists(repo_root)

            if not git_worktree_add(worktree_path, name, new_branch=False):
                click.echo(f"Failed to create worktree for branch: {name}", err=True)
                sys.exit(1)

            copied = copy_env_files(repo_root, worktree_path)
            if copied:
                click.echo(f"Copied: {', '.join(copied)}", err=True)

            click.echo(f"Created sandbox from local branch: {name}", err=True)
            run_sandbox(sname, repo_name, main_git, worktree_path, template=template, extra_mounts=list(extra_mounts))
    elif remote_branch_exists(name):
        # Existing remote branch - create worktree tracking it
        template = build_template_if_exists(repo_root)

        # Create worktree tracking the remote branch
        if not git_worktree_add(worktree_path, name, new_branch=False):
            click.echo(f"Failed to create worktree for remote branch: {name}", err=True)
            sys.exit(1)

        copied = copy_env_files(repo_root, worktree_path)
        if copied:
            click.echo(f"Copied: {', '.join(copied)}", err=True)

        click.echo(f"Created sandbox from remote branch: {name}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template, extra_mounts=list(extra_mounts))
    else:
        # New sandbox with new branch
        # Pull latest changes before creating branch
        click.echo("Pulling latest changes...", err=True)
        pull_result = run(["git", "-C", str(repo_root), "pull", "--ff-only"], capture=False)
        if pull_result.returncode != 0:
            click.echo("Warning: Could not pull latest changes", err=True)

        template = build_template_if_exists(repo_root)

        if not git_worktree_add(worktree_path, name, new_branch=True):
            click.echo(f"Failed to create worktree", err=True)
            sys.exit(1)

        copied = copy_env_files(repo_root, worktree_path)
        if copied:
            click.echo(f"Copied: {', '.join(copied)}", err=True)

        click.echo(f"Created sandbox: {sname}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template, extra_mounts=list(extra_mounts))


@cli.command()
@click.argument("name")
def join(name):
    """Join a running sandbox with a new Claude session."""
    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    sname = safe_name(name)
    repo_name = repo_root.name
    container_name = f"sandbox-{repo_name}-{sname}"

    if not container_exists(container_name):
        click.echo(f"No sandbox found: {sname}", err=True)
        sys.exit(1)

    if not container_running(container_name):
        click.echo(f"Sandbox is not running: {sname}", err=True)
        sys.exit(1)

    cmd_parts = ["docker", "exec", "-it", container_name, "claude", "--dangerously-skip-permissions"]
    os.execvp(cmd_parts[0], cmd_parts)


@cli.command()
@click.argument("name")
def read(name):
    """Read results from a completed or running sandbox task."""
    import re

    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    sb = resolve_sandbox(repo_root, name)
    container_name = sb["container"]
    result_path = sb["log_json"]

    # 1. Check for saved log/state file
    if result_path.exists():
        try:
            data = json.loads(result_path.read_text())
        except (json.JSONDecodeError, OSError):
            click.echo(json.dumps({"error": f"Corrupted state file: {result_path}"}))
            return
        if "status" not in data:
            # Completed result — return as-is
            click.echo(json.dumps(data))
            return

        # Running state file — check runtime
        if data.get("runtime") == "modal":
            # Modal running state — reconnect or report gone
            sandbox_id = data.get("sandboxId")
            if not sandbox_id:
                click.echo(json.dumps({"error": "Modal sandbox ID not recorded (sandbox may have not started)"}))
                return
            try:
                import modal
                from modal.exception import NotFoundError
            except ImportError:
                click.echo(json.dumps({"error": "modal package not installed. Run: pip install modal"}))
                return
            try:
                modal_sb = modal.Sandbox.from_id(sandbox_id)
            except NotFoundError:
                click.echo(json.dumps({"error": f"Modal sandbox expired or not found: {sandbox_id}"}))
                return

            # Wait if still running
            if modal_sb.poll() is None:
                click.echo("Waiting for Modal sandbox to complete...", err=True)
                modal_sb.wait()

            # Read remaining stdout
            log_raw = sb["log_raw"]
            ansi_re = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
            try:
                remaining = modal_sb.stdout.read()
            except Exception:
                remaining = ""

            # Append remaining output to log file
            if remaining:
                log_raw.parent.mkdir(parents=True, exist_ok=True)
                with log_raw.open("a", encoding="utf-8") as f:
                    f.write(remaining)

            try:
                exit_code = modal_sb.returncode if modal_sb.returncode is not None else modal_sb.wait()
            except Exception:
                exit_code = 1

            try:
                modal_sb.terminate(wait=True)
            except Exception:
                pass
            try:
                modal_sb.detach()
            except Exception:
                pass

            # Parse __SANDBOX_RESULT__ from full log
            full_log = log_raw.read_text(encoding="utf-8") if log_raw.exists() else ""
            sandbox_result: dict = {}
            for line in full_log.splitlines():
                clean = ansi_re.sub("", line)
                if clean.startswith("__SANDBOX_RESULT__"):
                    payload = clean[len("__SANDBOX_RESULT__"):].strip()
                    try:
                        sandbox_result = json.loads(payload)
                    except (json.JSONDecodeError, ValueError):
                        pass

            provider_name = data.get("provider", "claude")
            task_provider = get_provider(provider_name)
            worktree_path = Path("/workspace")
            response = task_provider["extract_response"](worktree_path, log_raw) if log_raw.exists() else None

            result: dict = {
                "name": data.get("name", safe_name(name)),
                "branch": data.get("branch", name),
                "runtime": "modal",
                "provider": provider_name,
                "exitCode": sandbox_result.get("exitCode", exit_code),
                "modifiedFiles": sandbox_result.get("modifiedFiles", []),
                "pushed": sandbox_result.get("pushed", False),
            }
            commit_sha = sandbox_result.get("commitSha", "")
            if commit_sha:
                result["commitSha"] = commit_sha
            diff_stats = sandbox_result.get("diffStats")
            if diff_stats:
                result["diffStats"] = diff_stats
            if response:
                result["response"] = response
            if exit_code != 0:
                result["error"] = sandbox_result.get("error") or f"{provider_name} exited with code {exit_code}"
            elif sandbox_result.get("error"):
                result["error"] = sandbox_result["error"]

            result_path.write_text(json.dumps(result))
            click.echo(json.dumps(result))
            return

        # Docker running state
        base_commit = data.get("baseCommit", "")
        state_provider_name = data.get("provider", "claude")
        if not container_exists(container_name):
            click.echo(json.dumps({"error": "Task was running but container is gone"}))
            return
        # Container exists — recover below

    # 2. Check for container (running or exited)
    if container_exists(container_name):
        if container_running(container_name):
            run(["docker", "wait", container_name])

        # Get exit code (works for both just-finished and already-exited containers)
        wait_result = run(["docker", "inspect", "-f", "{{.State.ExitCode}}", container_name])

        try:
            exit_code = int(wait_result.stdout.strip())
        except (ValueError, AttributeError):
            exit_code = 1

        # Get base_commit and provider from state file if available
        if not result_path.exists():
            base_commit = ""
            state_provider_name = "claude"
        elif "base_commit" not in dir():
            state = json.loads(result_path.read_text())
            base_commit = state.get("baseCommit", "")
            state_provider_name = state.get("provider", "claude")

        provider = get_provider(state_provider_name)
        result = _collect_and_finalize(sb, exit_code, base_commit, repo_root, name, provider=provider)
        click.echo(json.dumps(result))
        return

    # 3. Not found
    click.echo(json.dumps({"error": "Sandbox not found"}))


@cli.command("ls")
def list_cmd():
    """List worktrees and containers for current repo."""
    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    repo_name = repo_root.name

    click.echo("=== Worktrees ===")
    worktrees = git_worktree_list()
    for wt in worktrees:
        branch = wt.get("branch", "").replace("refs/heads/", "")
        click.echo(f"  {wt['path']}  [{branch}]")

    if not worktrees:
        click.echo("  (none)")

    click.echo("\n=== Containers ===")
    containers = docker_container_ls()
    prefix = f"sandbox-{repo_name}-"
    repo_containers = [c for c in containers if c.get("name", "").startswith(prefix)]

    for c in repo_containers:
        click.echo(f"  {c['name']}  ({c['status']})")

    if not repo_containers:
        click.echo("  (none)")


@cli.command()
@click.argument("name", required=False)
@click.option("--all", "remove_all", is_flag=True, help="Remove all sandboxes for current repo.")
@click.option("--force", is_flag=True, help="Allow removing a running task (kills the container).")
@click.option("--yes", "-y", is_flag=True, help="Skip prompts and auto-delete branch.")
def rm(name, remove_all, force, yes):
    """Remove a sandbox and all its artifacts."""
    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    if remove_all:
        _rm_all(repo_root)
        return

    if not name:
        click.echo("Name required (or use --all)", err=True)
        sys.exit(1)

    sb = resolve_sandbox(repo_root, name)
    container_name = sb["container"]
    worktree_path = sb["worktree"]
    sname = sb["sname"]

    # Check if task is running
    if sb["log_json"].exists():
        try:
            data = json.loads(sb["log_json"].read_text())
            if data.get("status") == "running" and container_running(container_name) and not force:
                click.echo(f"Task is running. Use --force if you are sure you want to kill it.", err=True)
                sys.exit(1)
        except (json.JSONDecodeError, OSError):
            pass

    branch_name = sb["branch"]

    removed_container = docker_container_rm(container_name)
    removed_worktree = git_worktree_remove(worktree_path)

    # Remove log files
    removed_logs = False
    for log_path in (sb["log_json"], sb["log_raw"], sb["log_err"]):
        if log_path.exists():
            log_path.unlink()
            removed_logs = True

    if removed_container:
        click.echo(f"Removed container: {container_name}")
    if removed_worktree:
        click.echo(f"Removed worktree: {worktree_path}")
    if removed_logs:
        click.echo(f"Removed logs for: {sname}")

    if not removed_container and not removed_worktree and not removed_logs and not branch_exists(branch_name):
        click.echo(f"Nothing found to remove for: {sname}", err=True)
        sys.exit(1)

    # Delete branch: --yes skips prompt (for agents), otherwise ask
    if branch_exists(branch_name):
        should_delete = yes or click.confirm(f"Delete branch '{branch_name}'?", default=False)
        if should_delete:
            result = run(["git", "branch", "-D", branch_name])
            if result.returncode == 0:
                click.echo(f"Deleted branch: {branch_name}")
            else:
                click.echo(f"Failed to delete branch: {result.stderr}", err=True)


def _rm_all(repo_root: Path):
    """Remove all sandboxes for the current repo."""
    repo_name = repo_root.name
    click.echo(f"Removing all sandboxes for {repo_name}...")

    # Remove containers
    containers = docker_container_ls()
    prefix = f"sandbox-{repo_name}-"
    for c in containers:
        if c.get("name", "").startswith(prefix):
            if docker_container_rm(c["name"]):
                click.echo(f"  Removed container: {c['name']}")

    # Remove worktrees
    worktrees = git_worktree_list()
    for wt in worktrees:
        if f"{repo_name}__" in wt["path"]:
            if git_worktree_remove(Path(wt["path"])):
                click.echo(f"  Removed worktree: {wt['path']}")

    # Remove log files
    logs_dir = get_logs_dir()
    if logs_dir.exists():
        for f in logs_dir.glob(f"sandbox-{repo_name}-*"):
            f.unlink()
            click.echo(f"  Removed: {f.name}")

    run(["git", "worktree", "prune"])
    click.echo("Done")


@cli.command()
@click.argument("name")
def ports(name):
    """Show available ports for a sandbox."""
    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    sname = safe_name(name)
    repo_name = repo_root.name
    container_name = f"sandbox-{repo_name}-{sname}"

    if not container_exists(container_name):
        click.echo(f"Sandbox not found: {sname}", err=True)
        sys.exit(1)

    # Get SANDBOX_PORTS env var from container
    result = run(["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container_name])
    if result.returncode != 0:
        click.echo("Failed to inspect container", err=True)
        sys.exit(1)

    ports = None
    for line in result.stdout.strip().split("\n"):
        if line.startswith("SANDBOX_PORTS="):
            ports = line.split("=", 1)[1]
            break

    if not ports:
        click.echo("No ports configured")
        return

    # Check which ports are actively listening
    listen_result = run(["docker", "exec", container_name, "ss", "-tlnH"])
    listening = set()
    if listen_result.returncode == 0:
        for line in listen_result.stdout.strip().split("\n"):
            # Format: LISTEN 0 128 *:49152 *:*
            parts = line.split()
            if len(parts) >= 4:
                addr = parts[3]
                if ":" in addr:
                    port = addr.rsplit(":", 1)[-1]
                    if port.isdigit():
                        listening.add(port)

    click.echo(f"Ports for {sname}:")
    for port in ports.split(","):
        status = " (active)" if port in listening else ""
        click.echo(f"  http://localhost:{port}{status}")


@cli.command("post-exit")
@click.argument("name")
@click.argument("repo_name")
def post_exit(name, repo_name):
    """Cleanup prompt after exiting a sandbox."""
    sname = safe_name(name)
    container_name = f"sandbox-{repo_name}-{sname}"

    # Get repo root for worktree operations
    repo_root = get_repo_root()
    if repo_root:
        # If in worktree, get main repo root
        if "__" in repo_root.name:
            repo_root = repo_root.parent / repo_name
    worktree_path = get_worktree_path(repo_root, sname) if repo_root else None

    # Only prompt if sandbox still exists
    if not container_exists(container_name):
        return

    # Check for uncommitted changes in worktree
    has_uncommitted = False
    if worktree_path and worktree_path.exists():
        result = run(["git", "-C", str(worktree_path), "status", "--porcelain"])
        has_uncommitted = bool(result.stdout.strip())

    click.echo("", err=True)  # Blank line after Claude exits
    if has_uncommitted:
        click.echo("⚠️  Warning: There are uncommitted changes in this sandbox!", err=True)
    if not click.confirm("Cleanup sandbox?", default=not has_uncommitted, err=True):
        click.echo("Sandbox kept for later.", err=True)
        return

    # Get branch name before removing worktree
    branch_name = None
    if worktree_path:
        for wt in git_worktree_list():
            if wt.get("path") == str(worktree_path):
                branch_name = wt.get("branch", "").replace("refs/heads/", "")
                break

    docker_container_rm(container_name)
    worktree_removed = False
    if worktree_path:
        worktree_removed = git_worktree_remove(worktree_path, force=has_uncommitted)
    click.echo(f"Removed sandbox: {sname}", err=True)

    # Offer to delete branch (only if worktree was removed)
    if branch_name and worktree_removed and click.confirm(f"Delete branch '{branch_name}'?", default=False, err=True):
        if click.confirm("Confirm delete branch?", default=False, err=True):
            # Use -C to run from main repo directory
            main_repo = repo_root.parent / repo_name if repo_root else None
            if main_repo and main_repo.exists():
                result = run(["git", "-C", str(main_repo), "branch", "-D", branch_name])
            else:
                result = run(["git", "branch", "-D", branch_name])
            if result.returncode == 0:
                click.echo(f"Deleted branch: {branch_name}", err=True)
            else:
                click.echo(f"Failed to delete branch: {result.stderr}", err=True)




DOCS = {
    "prompt-guide": "How to write sandbox task prompts",
    "operations-guide": "Running sandboxes, integrating results, failure modes",
    "dockerfile": "Built-in Dockerfile (reference for Dockerfile.sandbox)",
}


@cli.command()
@click.argument("name", required=False)
def docs(name):
    """Show documentation guides."""
    docs_dir = Path(__file__).parent / "docs"
    if name is None:
        click.echo("Available guides:\n")
        for doc_name, desc in DOCS.items():
            click.echo(f"  {doc_name:25s} {desc}")
        click.echo(f"\nUsage: sandbox docs <name>")
        return
    if name not in DOCS:
        click.echo(f"Unknown guide: {name}", err=True)
        click.echo(f"Available: {', '.join(DOCS.keys())}", err=True)
        sys.exit(1)
    if name == "dockerfile":
        doc_path = Path(__file__).parent / "Dockerfile"
    else:
        doc_path = docs_dir / f"{name}.md"
    if not doc_path.exists():
        click.echo(f"Guide file not found: {doc_path}", err=True)
        sys.exit(1)
    click.echo(doc_path.read_text())


if __name__ == "__main__":
    cli()
