#!/usr/bin/env python3
"""Docker Sandbox CLI - Manage sandboxed Claude Code environments with git worktrees."""

import subprocess
import sys
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


def get_repo_root() -> Path | None:
    """Get the git repository root directory."""
    result = run(["git", "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


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


def get_worktree_path(repo_root: Path, name: str) -> Path:
    """Get the worktree path for a given name."""
    repo_name = repo_root.name
    return repo_root.parent / f"{repo_name}__{name}"


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
    cmd = ["git", "worktree", "add", str(path)]
    if new_branch:
        cmd.extend(["-b", branch])
    else:
        cmd.append(branch)
    result = run(cmd, capture=False)
    return result.returncode == 0


def git_worktree_remove(path: Path) -> bool:
    """Remove a git worktree."""
    result = run(["git", "worktree", "remove", str(path)])
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


def setup_sandbox_claude_settings(worktree_path: Path) -> None:
    """Copy sandbox hooks and merge Claude settings to worktree."""
    import shutil
    import json

    sandbox_cli_dir = get_sandbox_cli_dir()
    sandbox_claude_dir = sandbox_cli_dir / ".claude"
    worktree_claude_dir = worktree_path / ".claude"

    if not sandbox_claude_dir.exists():
        return

    # Ensure .claude directory exists in worktree
    worktree_claude_dir.mkdir(exist_ok=True)

    # Copy hooks directory
    sandbox_hooks = sandbox_claude_dir / "hooks"
    if sandbox_hooks.exists():
        worktree_hooks = worktree_claude_dir / "hooks"
        if worktree_hooks.exists():
            shutil.rmtree(worktree_hooks)
        shutil.copytree(sandbox_hooks, worktree_hooks)

    # Merge settings.json
    sandbox_settings_file = sandbox_claude_dir / "settings.json"
    worktree_settings_file = worktree_claude_dir / "settings.json"

    if sandbox_settings_file.exists():
        sandbox_settings = json.loads(sandbox_settings_file.read_text())

        if worktree_settings_file.exists():
            # Merge with existing project settings
            project_settings = json.loads(worktree_settings_file.read_text())
            merged = merge_claude_settings(project_settings, sandbox_settings)
        else:
            merged = sandbox_settings

        worktree_settings_file.write_text(json.dumps(merged, indent=2))


def merge_claude_settings(project: dict, sandbox: dict) -> dict:
    """Merge sandbox settings into project settings, combining hooks."""
    import copy
    result = copy.deepcopy(project)

    # Merge hooks
    if "hooks" in sandbox:
        if "hooks" not in result:
            result["hooks"] = {}
        for event, hooks_list in sandbox["hooks"].items():
            if event in result["hooks"]:
                # Append sandbox hooks to project hooks
                result["hooks"][event].extend(hooks_list)
            else:
                result["hooks"][event] = hooks_list

    # Merge other top-level keys (sandbox overrides)
    for key, value in sandbox.items():
        if key != "hooks":
            result[key] = value

    return result


def get_sandbox_cli_dir() -> Path:
    """Get the sandbox-cli installation directory."""
    return Path(__file__).parent


def ensure_default_image() -> str:
    """Ensure the default sandbox image exists, build if needed."""
    image_name = "sandbox-cli:default"

    # Check if image exists
    result = run(["docker", "image", "inspect", image_name])
    if result.returncode == 0:
        return image_name

    # Build default image
    dockerfile = get_sandbox_cli_dir() / "Dockerfile"
    if not dockerfile.exists():
        click.echo(f"Default Dockerfile not found: {dockerfile}", err=True)
        sys.exit(1)

    click.echo("Building default sandbox image...")
    result = run(
        ["docker", "build", "-t", image_name, "-f", str(dockerfile), str(get_sandbox_cli_dir())],
        capture=False,
    )
    if result.returncode != 0:
        click.echo("Failed to build default image", err=True)
        sys.exit(1)

    return image_name


def build_template_if_exists(repo_root: Path) -> str | None:
    """Build custom template if Dockerfile.sandbox exists, otherwise use default."""
    dockerfile = repo_root / "Dockerfile.sandbox"
    if not dockerfile.exists():
        return ensure_default_image()

    image_name = f"sandbox-template:{repo_root.name}"
    click.echo("Building project template...")
    result = run(
        ["docker", "build", "-t", image_name, "-f", str(dockerfile), str(repo_root)],
        capture=False,
    )
    if result.returncode == 0:
        return image_name
    return None


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


def run_sandbox(name: str, repo_name: str, main_git: Path, worktree_path: Path, template: str | None = None, resume: bool = False) -> None:
    """Output sandbox run command for shell wrapper to execute."""
    home = Path.home()
    image = template or ensure_default_image()
    container_name = f"sandbox-{repo_name}-{name}"

    claude_cmd = ["claude", "--dangerously-skip-permissions"]
    if resume:
        claude_cmd.append("--continue")

    # Will be set after ports are determined for new containers
    ports_prompt = None

    # Check if container already exists
    if container_exists(container_name):
        if container_running(container_name):
            # Exec into running container
            cmd_parts = ["docker", "exec", "-it", container_name] + claude_cmd
        else:
            # Start stopped container and exec
            cmd_parts = ["docker", "start", container_name, "&&",
                        "docker", "exec", "-it", container_name] + claude_cmd
    else:
        # Create new container
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
            "-v", f"{home}/.ssh:/home/agent/.ssh:ro",
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


@click.group()
def cli():
    """Docker Sandbox CLI - Manage sandboxed Claude Code environments."""
    pass


def branch_exists(branch: str) -> bool:
    """Check if a local branch exists."""
    result = run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    return result.returncode == 0


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
@click.argument("name", required=False)
def start(name):
    """Start a sandbox (creates if new, resumes if exists)."""
    if not name:
        name = generate_sandbox_name()
        click.echo(f"Generated name: {name}", err=True)

    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    sname = safe_name(name)
    repo_name = repo_root.name
    worktree_path = get_worktree_path(repo_root, sname)
    main_git = get_main_git_dir(repo_root)

    if worktree_path.exists() and sandbox_exists(repo_name, sname):
        # Existing sandbox - recreate container
        container_name = f"sandbox-{repo_name}-{sname}"
        docker_container_rm(container_name)
        click.echo(f"Recreating sandbox: {sname}", err=True)
        template = build_template_if_exists(repo_root)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)
    elif worktree_path.exists():
        # Worktree exists but no sandbox - start fresh
        template = build_template_if_exists(repo_root)
        click.echo(f"Starting sandbox: {sname}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)
    elif branch_exists(name):
        # Existing local branch - create worktree from it
        template = build_template_if_exists(repo_root)

        if not git_worktree_add(worktree_path, name, new_branch=False):
            click.echo(f"Failed to create worktree for branch: {name}", err=True)
            sys.exit(1)

        copied = copy_env_files(repo_root, worktree_path)
        if copied:
            click.echo(f"Copied: {', '.join(copied)}", err=True)
        setup_sandbox_claude_settings(worktree_path)

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
        setup_sandbox_claude_settings(worktree_path)

        click.echo(f"Created sandbox from remote branch: {name}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)
    else:
        # New sandbox with new branch
        template = build_template_if_exists(repo_root)
        branch_name = f"task/{name}"

        if not git_worktree_add(worktree_path, branch_name, new_branch=True):
            click.echo(f"Failed to create worktree", err=True)
            sys.exit(1)

        copied = copy_env_files(repo_root, worktree_path)
        if copied:
            click.echo(f"Copied: {', '.join(copied)}", err=True)
        setup_sandbox_claude_settings(worktree_path)

        click.echo(f"Created sandbox: {sname}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)


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
    # Filter to containers for this repo (container names start with sandbox-)
    repo_containers = [c for c in containers if repo_name in c.get("name", "")]

    for c in repo_containers:
        click.echo(f"  {c['name']}  ({c['status']})")

    if not repo_containers:
        click.echo("  (none)")


@cli.command()
@click.argument("name")
def rm(name):
    """Remove a container and its worktree."""
    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    sname = safe_name(name)
    repo_name = repo_root.name
    worktree_path = get_worktree_path(repo_root, sname)
    container_name = f"sandbox-{repo_name}-{sname}"

    # Get branch name before removing worktree
    branch_name = None
    for wt in git_worktree_list():
        if wt.get("path") == str(worktree_path):
            branch_name = wt.get("branch", "").replace("refs/heads/", "")
            break

    removed_container = docker_container_rm(container_name)
    removed_worktree = git_worktree_remove(worktree_path)

    if removed_container:
        click.echo(f"Removed container: {container_name}")
    if removed_worktree:
        click.echo(f"Removed worktree: {worktree_path}")

    if not removed_container and not removed_worktree:
        click.echo(f"Nothing found to remove for: {sname}", err=True)
        sys.exit(1)

    # Offer to delete branch
    if branch_name and removed_worktree:
        if click.confirm(f"Delete branch '{branch_name}'?", default=False):
            result = run(["git", "branch", "-D", branch_name])
            if result.returncode == 0:
                click.echo(f"Deleted branch: {branch_name}")
            else:
                click.echo(f"Failed to delete branch: {result.stderr}", err=True)


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

    click.echo("", err=True)  # Blank line after Claude exits
    if not click.confirm("Cleanup sandbox?", default=True, err=True):
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
    if worktree_path:
        git_worktree_remove(worktree_path)
    click.echo(f"Removed sandbox: {sname}", err=True)

    # Offer to delete branch (run from main repo, not worktree)
    if branch_name and click.confirm(f"Delete branch '{branch_name}'?", default=False, err=True):
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


@cli.command()
def purge():
    """Remove all containers and worktrees for current repo."""
    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    repo_name = repo_root.name
    click.echo(f"Removing all containers for {repo_name}...")

    # Remove containers
    containers = docker_container_ls()
    for c in containers:
        if repo_name in c.get("name", ""):
            if docker_container_rm(c["name"]):
                click.echo(f"  Removed container: {c['name']}")

    # Remove worktrees
    click.echo("Removing worktrees...")
    worktrees = git_worktree_list()
    for wt in worktrees:
        if f"{repo_name}__" in wt["path"]:
            if git_worktree_remove(Path(wt["path"])):
                click.echo(f"  Removed worktree: {wt['path']}")

    # Prune
    run(["git", "worktree", "prune"])
    click.echo("Done")


if __name__ == "__main__":
    cli()
