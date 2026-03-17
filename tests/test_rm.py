"""Tests for the rm command."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest



@pytest.fixture
def runner():
    from click.testing import CliRunner
    return CliRunner()


@pytest.fixture
def cli():
    from sandbox_cli import cli
    return cli


@patch("sandbox_cli.get_repo_root")
def test_not_in_repo(mock_get_repo, runner, cli):
    mock_get_repo.return_value = None
    result = runner.invoke(cli, ["rm", "test"])
    assert result.exit_code == 1
    assert "Not in a git repository" in result.output


@patch("sandbox_cli.get_logs_dir")
@patch("sandbox_cli.get_repo_root")
@patch("sandbox_cli.docker_container_rm")
@patch("sandbox_cli.git_worktree_remove")
def test_rm_uses_repo_prefix(mock_wt_rm, mock_container_rm, mock_get_repo,
                               mock_logs_dir, runner, cli, tmp_path):
    mock_get_repo.return_value = Path("/Users/test/myrepo")
    mock_container_rm.return_value = True
    mock_wt_rm.return_value = True
    mock_logs_dir.return_value = tmp_path

    result = runner.invoke(cli, ["rm", "feature/auth"])
    mock_container_rm.assert_called_with("sandbox-myrepo-feature-auth")


@patch("sandbox_cli.get_logs_dir")
@patch("sandbox_cli.get_repo_root")
@patch("sandbox_cli.docker_container_rm")
@patch("sandbox_cli.git_worktree_remove")
def test_rm_deletes_log_files(mock_wt_rm, mock_container_rm, mock_get_repo,
                                mock_logs_dir, runner, cli, tmp_path):
    mock_get_repo.return_value = Path("/Users/test/myrepo")
    mock_container_rm.return_value = True
    mock_wt_rm.return_value = True
    mock_logs_dir.return_value = tmp_path

    (tmp_path / "sandbox-myrepo-test.json").write_text('{"exitCode": 0}')
    (tmp_path / "sandbox-myrepo-test.log").write_text('some logs')

    result = runner.invoke(cli, ["rm", "test"])
    assert not (tmp_path / "sandbox-myrepo-test.json").exists()
    assert not (tmp_path / "sandbox-myrepo-test.log").exists()


@patch("sandbox_cli.container_running")
@patch("sandbox_cli.get_logs_dir")
@patch("sandbox_cli.get_repo_root")
def test_rm_refuses_running_task_without_force(mock_get_repo, mock_logs_dir,
                                                 mock_running, runner, cli, tmp_path):
    mock_get_repo.return_value = Path("/Users/test/myrepo")
    mock_logs_dir.return_value = tmp_path
    mock_running.return_value = True

    (tmp_path / "sandbox-myrepo-test.json").write_text(json.dumps({"status": "running"}))

    result = runner.invoke(cli, ["rm", "test"])
    assert result.exit_code == 1
    assert "running" in result.output.lower() or "force" in result.output.lower()


@patch("sandbox_cli.container_running")
@patch("sandbox_cli.get_logs_dir")
@patch("sandbox_cli.get_repo_root")
@patch("sandbox_cli.docker_container_rm")
@patch("sandbox_cli.git_worktree_remove")
def test_rm_force_removes_running_task(mock_wt_rm, mock_container_rm, mock_get_repo,
                                         mock_logs_dir, mock_running, runner, cli, tmp_path):
    mock_get_repo.return_value = Path("/Users/test/myrepo")
    mock_logs_dir.return_value = tmp_path
    mock_running.return_value = True
    mock_container_rm.return_value = True
    mock_wt_rm.return_value = True

    (tmp_path / "sandbox-myrepo-test.json").write_text(json.dumps({"status": "running"}))

    result = runner.invoke(cli, ["rm", "test", "--force"])
    mock_container_rm.assert_called_once()


@patch("sandbox_cli.get_logs_dir")
@patch("sandbox_cli.get_repo_root")
@patch("sandbox_cli.docker_container_ls")
@patch("sandbox_cli.git_worktree_list")
@patch("sandbox_cli.docker_container_rm")
@patch("sandbox_cli.git_worktree_remove")
@patch("sandbox_cli.run")
def test_rm_all(mock_run, mock_wt_rm, mock_container_rm, mock_wt_list,
                  mock_container_ls, mock_get_repo, mock_logs_dir, runner, cli, tmp_path):
    mock_get_repo.return_value = Path("/Users/test/myrepo")
    mock_logs_dir.return_value = tmp_path
    mock_container_ls.return_value = [
        {"id": "abc", "name": "sandbox-myrepo-test", "status": "Up 2 hours"},
        {"id": "def", "name": "sandbox-other-foo", "status": "Up 1 hour"},
    ]
    mock_wt_list.return_value = [
        {"path": "/Users/test/myrepo", "branch": "refs/heads/main"},
        {"path": "/Users/test/myrepo__test", "branch": "refs/heads/test"},
    ]
    mock_container_rm.return_value = True
    mock_wt_rm.return_value = True
    mock_run.return_value = MagicMock(returncode=0)

    (tmp_path / "sandbox-myrepo-test.json").write_text('{"exitCode": 0}')
    (tmp_path / "sandbox-myrepo-test.log").write_text('logs')

    result = runner.invoke(cli, ["rm", "--all"])
    assert result.exit_code == 0
    # Should remove repo-matching container
    mock_container_rm.assert_any_call("sandbox-myrepo-test")
