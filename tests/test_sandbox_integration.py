"""End-to-end integration tests for sandbox CLI (require Docker + Claude auth)."""

import json
import subprocess
from pathlib import Path

import pytest



def _docker_available():
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _claude_authenticated():
    result = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
        return data.get("loggedIn", False)
    except (json.JSONDecodeError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available() or not _claude_authenticated(),
    reason="Requires Docker and authenticated Claude CLI"
)


@pytest.fixture
def runner():
    from click.testing import CliRunner
    return CliRunner()


@pytest.fixture
def cli():
    from sandbox_cli import cli
    return cli


@pytest.fixture
def sandbox_name():
    """Generate a unique sandbox name for each test."""
    import random
    return f"integration-test-{random.randint(10000, 99999)}"


@pytest.fixture(autouse=True)
def cleanup_sandbox(sandbox_name, runner, cli):
    """Ensure sandbox is cleaned up after each test."""
    yield
    # Force cleanup regardless of test outcome
    runner.invoke(cli, ["rm", sandbox_name, "--force", "--yes"])


class TestBackgroundTaskEndToEnd:
    def test_start_task_returns_result(self, runner, cli, sandbox_name):
        result = runner.invoke(cli, ["start", sandbox_name, "--task",
            "Create a file called hello.txt containing 'hello world'. Do nothing else.",
            "--model", "haiku"])
        assert result.exit_code == 0, f"output: {result.output}"
        output = json.loads(result.output)
        assert "exitCode" in output
        assert output["container"].startswith("sandbox-")
        assert output["name"] in output["container"]

    def test_read_returns_saved_result(self, runner, cli, sandbox_name):
        # First run a task
        result = runner.invoke(cli, ["start", sandbox_name, "--task",
            "Create a file called test.txt containing 'test'. Do nothing else.",
            "--model", "haiku"])
        assert result.exit_code == 0, f"output: {result.output}"
        first_output = json.loads(result.output)

        # Then read should return the same result
        result = runner.invoke(cli, ["read", sandbox_name])
        assert result.exit_code == 0
        read_output = json.loads(result.output)
        assert read_output["container"] == first_output["container"]
        assert read_output["exitCode"] == first_output["exitCode"]

    def test_rm_cleans_up_everything(self, runner, cli, sandbox_name):
        from sandbox_cli import get_logs_dir, resolve_sandbox, get_repo_root

        # Run a task
        result = runner.invoke(cli, ["start", sandbox_name, "--task",
            "Create a file called cleanup.txt containing 'cleanup'. Do nothing else.",
            "--model", "haiku"])
        assert result.exit_code == 0, f"output: {result.output}"

        repo_root = get_repo_root()
        sb = resolve_sandbox(repo_root, sandbox_name)

        # Verify artifacts exist
        assert sb["log_json"].exists()

        # Remove
        result = runner.invoke(cli, ["rm", sandbox_name], input="n\n")
        assert result.exit_code == 0

        # Verify cleanup
        assert not sb["log_json"].exists()
        assert not sb["log_raw"].exists()
