"""Tests for the read command."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest



@pytest.fixture
def runner():
    from click.testing import CliRunner
    return CliRunner()


@pytest.fixture
def cli():
    from sandbox_cli import cli
    return cli


def test_requires_name(runner, cli):
    result = runner.invoke(cli, ["read"])
    assert result.exit_code != 0


@patch("sandbox_cli.get_repo_root")
@patch("sandbox_cli.get_logs_dir")
def test_returns_completed_result(mock_logs_dir, mock_repo, runner, cli, tmp_path):
    mock_repo.return_value = Path("/Users/test/myrepo")
    mock_logs_dir.return_value = tmp_path
    result_data = {"container": "sandbox-myrepo-test", "name": "test", "branch": "test", "exitCode": 0, "response": "Done."}
    (tmp_path / "sandbox-myrepo-test.json").write_text(json.dumps(result_data))

    result = runner.invoke(cli, ["read", "test"])
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert output["response"] == "Done."


@patch("sandbox_cli.container_exists")
@patch("sandbox_cli.get_repo_root")
@patch("sandbox_cli.get_logs_dir")
def test_not_found(mock_logs_dir, mock_repo, mock_container, runner, cli, tmp_path):
    mock_repo.return_value = Path("/Users/test/myrepo")
    mock_logs_dir.return_value = tmp_path
    mock_container.return_value = False
    result = runner.invoke(cli, ["read", "test"])
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert "error" in output


@patch("sandbox_cli.container_exists")
@patch("sandbox_cli.get_repo_root")
@patch("sandbox_cli.get_logs_dir")
def test_running_state_file_with_no_container_reports_error(mock_logs_dir, mock_repo, mock_container, runner, cli, tmp_path):
    mock_repo.return_value = Path("/Users/test/myrepo")
    mock_logs_dir.return_value = tmp_path
    mock_container.return_value = False
    (tmp_path / "sandbox-myrepo-test.json").write_text(json.dumps({"status": "running", "container": "sandbox-myrepo-test"}))

    result = runner.invoke(cli, ["read", "test"])
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert "error" in output


@patch("sandbox_cli.docker_container_rm")
@patch("sandbox_cli.git_worktree_remove")
@patch("sandbox_cli.container_exists")
@patch("sandbox_cli.container_running")
@patch("sandbox_cli.run")
@patch("sandbox_cli.get_repo_root")
@patch("sandbox_cli.get_logs_dir")
def test_recovery_from_exited_container_commits_and_cleans_up(
    mock_logs_dir, mock_repo, mock_run, mock_running, mock_exists,
    mock_wt_rm, mock_container_rm, runner, cli, tmp_path
):
    """read should perform full lifecycle recovery for an exited container."""
    mock_repo.return_value = Path("/Users/test/myrepo")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    mock_logs_dir.return_value = logs_dir
    mock_exists.return_value = True
    mock_running.return_value = False  # already exited
    mock_wt_rm.return_value = True
    mock_container_rm.return_value = True

    # State file with recovery metadata
    state = {
        "status": "running",
        "container": "sandbox-myrepo-test",
        "name": "test",
        "branch": "test",
        "worktreePath": "/tmp/myrepo__test",
        "baseCommit": "abc123",
    }
    (logs_dir / "sandbox-myrepo-test.json").write_text(json.dumps(state))

    def run_side_effect(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "docker logs" in cmd_str:
            return MagicMock(returncode=0, stderr="", stdout='{"type": "result", "result": "Recovered."}\n')
        if "docker wait" in cmd_str:
            return MagicMock(returncode=0, stdout="0\n")
        if "docker inspect" in cmd_str and "ExitCode" in cmd_str:
            return MagicMock(returncode=0, stdout="0\n")
        if "status" in cmd_str and "--porcelain" in cmd_str:
            return MagicMock(returncode=0, stdout="M file.py\n")
        if "add" in cmd_str and "-A" in cmd_str:
            return MagicMock(returncode=0, stderr="", stdout="")
        if "reset" in cmd_str and ".env" in cmd_str:
            return MagicMock(returncode=0, stderr="", stdout="")
        if "commit" in cmd_str and "-m" in cmd_str:
            return MagicMock(returncode=0, stderr="", stdout="")
        if "push" in cmd_str and "origin" in cmd_str:
            return MagicMock(returncode=0, stderr="", stdout="")
        if "diff" in cmd_str and "--numstat" in cmd_str:
            return MagicMock(returncode=0, stdout="10\t2\tfile.py\n")
        if "rev-parse" in cmd_str:
            return MagicMock(returncode=0, stdout="def456\n")
        return MagicMock(returncode=0, stderr="", stdout="")

    mock_run.side_effect = run_side_effect

    result = runner.invoke(cli, ["read", "test"])
    assert result.exit_code == 0
    output = json.loads(result.output)

    # Should have full result shape
    assert output["exitCode"] == 0
    assert output["response"] == "Recovered."
    assert output["container"] == "sandbox-myrepo-test"
    assert "worktreePath" in output
    assert "modifiedFiles" in output
    # Container removed, worktree preserved for inspection
    mock_container_rm.assert_called_once()
    mock_wt_rm.assert_not_called()


from unittest.mock import MagicMock


@patch("sandbox_cli.get_repo_root")
@patch("sandbox_cli.get_logs_dir")
def test_corrupted_state_file_handled(mock_logs_dir, mock_repo, runner, cli, tmp_path):
    mock_repo.return_value = Path("/Users/test/myrepo")
    mock_logs_dir.return_value = tmp_path
    (tmp_path / "sandbox-myrepo-test.json").write_text("not valid json{{{")

    with patch("sandbox_cli.container_exists", return_value=False):
        result = runner.invoke(cli, ["read", "test"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert "error" in output
