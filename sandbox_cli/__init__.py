#!/usr/bin/env python3
"""Docker Sandbox CLI - Manage sandboxed Claude Code environments with git worktrees."""

import fcntl
import json
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
    dockerfile = repo_root / "Dockerfile.sandbox"
    if not dockerfile.exists():
        return ensure_default_image()

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
    """Get GitHub token from gh CLI."""
    result = run(["gh", "auth", "token"])
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


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

        base_result = run(["git", "-C", str(worktree_path), "rev-parse", "HEAD"])
        base_commit = base_result.stdout.strip()

        claude_cmd = ["claude", "--continue", "--print", "--verbose", "--output-format", "stream-json", "--dangerously-skip-permissions"]
        if model:
            claude_cmd.extend(["--model", model])

        # Start container if stopped, then exec
        if not container_running(container_name):
            run(["docker", "start", container_name])
        exec_result = run(["docker", "exec", container_name] + claude_cmd)

        result = _collect_and_finalize(sb, exec_result.returncode, base_commit, repo_root, name, push=push, cleanup=cleanup)
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
    claude_cmd = ["claude", "-p", task, "--print", "--verbose", "--output-format", "stream-json", "--dangerously-skip-permissions"]
    if model:
        claude_cmd.extend(["--model", model])

    auth_token = get_auth_token()
    if not auth_token:
        git_worktree_remove(worktree_path, force=True)
        run(["git", "branch", "-D", name])
        log_file.unlink(missing_ok=True)
        return {**error_result, "error": "No auth token configured. Run: claude setup-token && sandbox auth <token>"}

    docker_cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "-v", f"{worktree_path}:{worktree_path}",
        "-v", f"{main_git}:{main_git}",
        "-v", f"{home}/.ssh:/home/agent/.ssh:ro",
        "-v", f"{home}/.config/gh:/home/agent/.config/gh:ro",
        "-v", "pnpm-store:/pnpm-store",
        "-e", f"CLAUDE_CODE_OAUTH_TOKEN={auth_token}",
        "-e", f"GH_TOKEN={get_gh_token()}",
        "-w", str(worktree_path),
        image,
    ] + claude_cmd

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

    result = _collect_and_finalize(sb, exit_code, base_commit, repo_root, name, push=push, cleanup=cleanup)
    return result


def _collect_and_finalize(sb: dict, exit_code: int, base_commit: str, repo_root: Path, name: str,
                          push: bool = False, cleanup: bool = False) -> dict:
    """Shared post-wait finalization: save logs, commit, optionally push/cleanup, build result."""
    container_name = sb["container"]
    sname = sb["sname"]
    worktree_path = sb["worktree"]
    log_file = sb["log_json"]

    # Save raw logs
    log_result = run(["docker", "logs", container_name])
    sb["log_raw"].parent.mkdir(parents=True, exist_ok=True)
    sb["log_raw"].write_text(log_result.stdout)

    # Commit
    commit_succeeded = False
    nothing_to_commit = False
    run(["git", "-C", str(worktree_path), "add", "-A"])
    run(["git", "-C", str(worktree_path), "reset", "HEAD", "--", ".env*"])

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

    # Extract response text
    response = extract_response(sb["log_raw"])

    result = {
        "container": container_name,
        "name": sname,
        "branch": name,
        "exitCode": exit_code,
        "worktreePath": str(worktree_path),
        "modifiedFiles": modified_files,
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
        result["error"] = f"Claude exited with code {exit_code}"

    if not commit_succeeded and not nothing_to_commit:
        result["error"] = f"Commit failed, worktree preserved at {worktree_path}"

    # Save result (overwrites state file)
    log_file.write_text(json.dumps(result))

    return result


def run_sandbox(name: str, repo_name: str, main_git: Path, worktree_path: Path, template: str | None = None) -> None:
    """Output sandbox run command for shell wrapper to execute."""
    home = Path.home()
    image = template or ensure_default_image()
    container_name = f"sandbox-{repo_name}-{name}"

    claude_cmd = ["claude", "--dangerously-skip-permissions"]

    # Will be set after ports are determined for new containers
    ports_prompt = None

    # Check if container already exists - session lives in the container
    if container_exists(container_name):
        claude_cmd.append("--continue")
        if container_running(container_name):
            # Exec into running container
            cmd_parts = ["docker", "exec", "-it", container_name] + claude_cmd
        else:
            # Start stopped container and exec
            cmd_parts = ["docker", "start", container_name, "&&",
                        "docker", "exec", "-it", container_name] + claude_cmd
    else:
        # Create new container - fresh session
        cmd_parts = [
            "docker", "run", "-it",
            "--name", container_name,
            "-v", f"{worktree_path}:{worktree_path}",
            "-v", f"{main_git}:{main_git}",
            "-v", f"{home}/.claude:/home/agent/.claude",
            "-v", f"{home}/.config/gh:/home/agent/.config/gh:ro",
            "-e", f"GH_TOKEN={get_gh_token()}",
            "-e", "CLAUDE_CONFIG_DIR=/home/agent/.claude",
            "-e", "FORCE_COLOR=1",
            "-e", "COLORTERM=truecolor",
            "-e", "npm_config_store_dir=/pnpm-store",
            "-v", f"{home}/.ssh:/home/agent/.ssh:ro",
            "-v", "pnpm-store:/pnpm-store",
        ]
        # Add dynamic port mappings
        ports = find_available_ports(3)
        for port in ports:
            cmd_parts.extend(["-p", f"{port}:{port}"])
        cmd_parts.extend([
            "-e", f"SANDBOX_PORTS={','.join(map(str, ports))}",
            "-w", str(worktree_path),
            image,
        ])
        cmd_parts.extend(claude_cmd)
        ports_prompt = f"You are running in a sandbox. Available ports for dev servers: {', '.join(map(str, ports))}. When starting dev servers, use --port {ports[0]} --host 0.0.0.0 (host binding required for port forwarding)."
        cmd_parts.extend(["--append-system-prompt", f"'{ports_prompt}'"])

    # To wrap with VibeTunnel for session monitoring, uncomment:
    # import os
    # vt_check = run(["which", "vt"])
    # if vt_check.returncode == 0 and not os.environ.get("VIBETUNNEL_SESSION_ID"):
    #     cmd_parts = ["vt"] + cmd_parts

    # Output for shell wrapper: CD and EXEC directives
    print(f"__SANDBOX_CD__:{worktree_path}")
    print(f"__SANDBOX_EXEC__:{' '.join(cmd_parts)}")
    print(f"__SANDBOX_NAME__:{name}")
    print(f"__SANDBOX_REPO__:{repo_name}")


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
           --model <model>         Model to pass to Claude (e.g. haiku, sonnet)
           --push                  Push branch to origin after successful commit
           --cleanup               Remove worktree after completion
           Interactive: launches Docker container with Claude CLI
           Background:  commits changes, returns JSON to stdout

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

OUTPUT (background mode):
  All results include: container, name, branch, exitCode, worktreePath, modifiedFiles
  On commit:  + response, diffStats, commitSha
  On push:    + pushed: true
  On failure: + error
  All progress goes to stderr. Only final JSON goes to stdout.

USAGE WITH CLAUDE CODE:
  Run background tasks with run_in_background. They block until complete,
  so you will be notified when done. Do NOT poll or sleep.

  Workflow:
    1. sandbox start proto-1 --task "implement the auth module" --model sonnet
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
@click.option("--model", default=None, help="Model to pass to Claude.")
@click.option("--push", is_flag=True, help="Push branch to origin after successful commit.")
@click.option("--cleanup", is_flag=True, help="Remove worktree and container after completion.")
def start(name, continue_session, task, task_file, model, push, cleanup):
    """Start a sandbox (creates if new, resumes if exists)."""
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

    if not name:
        name = generate_sandbox_name()
        click.echo(f"Generated name: {name}", err=True)

    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    if task or continue_session:
        # Background mode
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
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)
    elif worktree_path.exists():
        # Worktree exists but no sandbox - start fresh
        template = build_template_if_exists(repo_root)
        click.echo(f"Starting sandbox: {sname}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)
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
            run_sandbox(sname, repo_name, main_git, worktree_path, template=template)
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
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)
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
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)


@cli.command()
@click.argument("name")
def read(name):
    """Read results from a completed or running sandbox task."""
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
        # Running state file — try to recover
        base_commit = data.get("baseCommit", "")
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

        # Get base_commit from state file if available
        if not result_path.exists():
            base_commit = ""
        elif "base_commit" not in dir():
            state = json.loads(result_path.read_text())
            base_commit = state.get("baseCommit", "")

        result = _collect_and_finalize(sb, exit_code, base_commit, repo_root, name)
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
    for log_path in (sb["log_json"], sb["log_raw"]):
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




if __name__ == "__main__":
    cli()
