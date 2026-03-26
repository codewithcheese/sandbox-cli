"""End-to-end integration tests for sandbox CLI with Gemini provider (require Docker + Gemini auth)."""

import json
import os
import subprocess
from pathlib import Path

import pytest


def _docker_available():
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _gemini_authenticated():
    return bool(os.environ.get("GEMINI_API_KEY")) or (Path.home() / ".gemini").is_dir()


@pytest.fixture(autouse=True, scope="session")
def require_docker_and_gemini():
    if not _docker_available():
        pytest.fail("Docker is not running")
    if not _gemini_authenticated():
        pytest.fail("Gemini is not authenticated. Set GEMINI_API_KEY or run: gemini (to login with Google account)")


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
    return f"integration-gemini-{random.randint(10000, 99999)}"


@pytest.fixture(autouse=True)
def cleanup_sandbox(sandbox_name, runner, cli):
    """Ensure sandbox is cleaned up after each test."""
    yield
    runner.invoke(cli, ["rm", sandbox_name, "--force", "--yes"])


class TestGeminiBackgroundTaskEndToEnd:
    def test_start_task_returns_result(self, runner, cli, sandbox_name):
        result = runner.invoke(cli, ["start", sandbox_name, "--task",
            "Create a file called hello.txt containing 'hello world'. Do nothing else.",
            "--provider", "gemini"])
        assert result.exit_code == 0, f"output: {result.output}"
        output = json.loads(result.output)
        assert "exitCode" in output
        assert output["container"].startswith("sandbox-")
        assert output["name"] in output["container"]
        assert output.get("provider") == "gemini"

    def test_read_returns_saved_result(self, runner, cli, sandbox_name):
        result = runner.invoke(cli, ["start", sandbox_name, "--task",
            "Create a file called test.txt containing 'test'. Do nothing else.",
            "--provider", "gemini"])
        assert result.exit_code == 0, f"output: {result.output}"
        first_output = json.loads(result.output)

        result = runner.invoke(cli, ["read", sandbox_name])
        assert result.exit_code == 0
        read_output = json.loads(result.output)
        assert read_output["container"] == first_output["container"]
        assert read_output["exitCode"] == first_output["exitCode"]

    def test_rm_cleans_up_everything(self, runner, cli, sandbox_name):
        from sandbox_cli import get_logs_dir, resolve_sandbox, get_repo_root

        result = runner.invoke(cli, ["start", sandbox_name, "--task",
            "Create a file called cleanup.txt containing 'cleanup'. Do nothing else.",
            "--provider", "gemini"])
        assert result.exit_code == 0, f"output: {result.output}"

        repo_root = get_repo_root()
        sb = resolve_sandbox(repo_root, sandbox_name)

        assert sb["log_json"].exists()

        result = runner.invoke(cli, ["rm", sandbox_name], input="n\n")
        assert result.exit_code == 0

        assert not sb["log_json"].exists()
        assert not sb["log_raw"].exists()
