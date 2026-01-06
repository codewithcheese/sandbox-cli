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


def build_template_if_exists(repo_root: Path) -> str | None:
    """Build custom template if Dockerfile.sandbox exists."""
    dockerfile = repo_root / "Dockerfile.sandbox"
    if not dockerfile.exists():
        return None

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
    image = template or f"sandbox-template:{repo_name}"
    container_name = f"sandbox-{repo_name}-{name}"

    claude_cmd = ["claude", "--dangerously-skip-permissions"]
    if resume:
        claude_cmd.append("--continue")

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
            "-w", str(worktree_path),
            image,
        ] + claude_cmd

    # Output for shell wrapper: CD and EXEC directives
    print(f"__SANDBOX_CD__:{worktree_path}")
    print(f"__SANDBOX_EXEC__:{' '.join(cmd_parts)}")


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


@cli.command()
@click.argument("name")
def start(name):
    """Start a sandbox (creates if new, resumes if exists)."""
    repo_root = get_repo_root()
    if not repo_root:
        click.echo("Not in a git repository", err=True)
        sys.exit(1)

    sname = safe_name(name)
    repo_name = repo_root.name
    worktree_path = get_worktree_path(repo_root, sname)
    main_git = get_main_git_dir(repo_root)

    if worktree_path.exists() and sandbox_exists(repo_name, sname):
        # Existing sandbox - resume
        click.echo(f"Resuming sandbox: {sname}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, resume=True)
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

        click.echo(f"Created sandbox from local branch: {name}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)
    elif remote_branch_exists(name):
        # Existing remote branch - create worktree tracking it
        template = build_template_if_exists(repo_root)

        # Create worktree tracking the remote branch
        if not git_worktree_add(worktree_path, name, new_branch=False):
            click.echo(f"Failed to create worktree for remote branch: {name}", err=True)
            sys.exit(1)

        click.echo(f"Created sandbox from remote branch: {name}", err=True)
        run_sandbox(sname, repo_name, main_git, worktree_path, template=template)
    else:
        # New sandbox with new branch
        template = build_template_if_exists(repo_root)
        branch_name = f"task/{name}"

        if not git_worktree_add(worktree_path, branch_name, new_branch=True):
            click.echo(f"Failed to create worktree", err=True)
            sys.exit(1)

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

    removed_container = docker_container_rm(container_name)
    removed_worktree = git_worktree_remove(worktree_path)

    if removed_container:
        click.echo(f"Removed container: {container_name}")
    if removed_worktree:
        click.echo(f"Removed worktree: {worktree_path}")

    if not removed_container and not removed_worktree:
        click.echo(f"Nothing found to remove for: {sname}", err=True)
        sys.exit(1)


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
