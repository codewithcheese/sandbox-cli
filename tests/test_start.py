"""Tests for the start command."""

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


def test_help_shows_task_flags(runner, cli):
    result = runner.invoke(cli, ["start", "--help"])
    assert result.exit_code == 0
    assert "--task" in result.output
    assert "--task-file" in result.output
    assert "--model" in result.output


@patch("sandbox_cli.get_repo_root")
def test_not_in_repo(mock_get_repo, runner, cli):
    mock_get_repo.return_value = None
    result = runner.invoke(cli, ["start", "my-task"])
    assert result.exit_code == 1
    assert "Not in a git repository" in result.output


@patch("sandbox_cli.get_repo_root")
def test_task_and_task_file_exclusive(mock_get_repo, runner, cli):
    mock_get_repo.return_value = Path("/tmp/repo")
    result = runner.invoke(cli, ["start", "test", "--task", "do thing", "--task-file", "f.txt"])
    assert result.exit_code != 0


@patch("sandbox_cli.get_repo_root")
def test_empty_task_rejected(mock_get_repo, runner, cli):
    mock_get_repo.return_value = Path("/tmp/repo")
    result = runner.invoke(cli, ["start", "test", "--task", ""])
    assert result.exit_code == 1
    assert "empty" in result.output.lower()


@patch("sandbox_cli.run_sandbox_background")
@patch("sandbox_cli.build_template_if_exists")
@patch("sandbox_cli.get_main_git_dir")
@patch("sandbox_cli.get_repo_root")
def test_task_calls_background(mock_repo, mock_git_dir, mock_build, mock_bg, runner, cli, tmp_path):
    mock_repo.return_value = tmp_path
    mock_git_dir.return_value = tmp_path / ".git"
    mock_build.return_value = "test-image"
    mock_bg.return_value = {"container": "sandbox-foo", "name": "foo", "branch": "foo", "exitCode": 0}

    result = runner.invoke(cli, ["start", "foo", "--task", "build it"])
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert output["container"] == "sandbox-foo"
    mock_bg.assert_called_once()


@patch("sandbox_cli.run_sandbox_background")
@patch("sandbox_cli.build_template_if_exists")
@patch("sandbox_cli.get_main_git_dir")
@patch("sandbox_cli.get_repo_root")
def test_task_file_reads_prompt(mock_repo, mock_git_dir, mock_build, mock_bg, runner, cli, tmp_path):
    mock_repo.return_value = tmp_path
    mock_git_dir.return_value = tmp_path / ".git"
    mock_build.return_value = "test-image"
    mock_bg.return_value = {"container": "sandbox-foo", "name": "foo", "branch": "foo", "exitCode": 0}

    task_file = tmp_path / "prompt.txt"
    task_file.write_text("build the thing")

    result = runner.invoke(cli, ["start", "foo", "--task-file", str(task_file)])
    assert result.exit_code == 0
    call_kwargs = mock_bg.call_args
    assert call_kwargs.kwargs.get("task") == "build the thing" or call_kwargs[1].get("task") == "build the thing"


@patch("builtins.print")
@patch("sandbox_cli.container_exists")
@patch("sandbox_cli.ensure_default_image")
@patch("sandbox_cli.get_gh_token")
@patch("sandbox_cli.find_available_ports")
def test_interactive_no_settings_or_sandbox_mount(mock_ports, mock_token, mock_image,
                                                    mock_container, mock_print):
    mock_container.return_value = False
    mock_image.return_value = "sandbox-cli:default"
    mock_token.return_value = "ghp_test"
    mock_ports.return_value = [49152, 49153, 49154]

    from sandbox_cli import run_sandbox
    run_sandbox(
        "test", "myrepo",
        Path("/tmp/.git"), Path("/tmp/myrepo__test"),
        template="sandbox-cli:default",
    )

    printed = " ".join(str(c) for c in mock_print.call_args_list)
    assert "--settings" not in printed
    assert "/opt/sandbox-claude" not in printed
