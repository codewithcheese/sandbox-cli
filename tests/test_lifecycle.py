"""Tests for the overall sandbox lifecycle across commands."""

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


class TestNameReservation:
    """A name is permanently reserved once used. Only rm frees it."""

    @patch("sandbox_cli.run_sandbox_background")
    @patch("sandbox_cli.build_template_if_exists")
    @patch("sandbox_cli.get_main_git_dir")
    @patch("sandbox_cli.get_repo_root")
    def test_start_task_then_read_returns_result(self, mock_repo, mock_git, mock_build,
                                                   mock_bg, runner, cli, tmp_path):
        mock_repo.return_value = tmp_path
        mock_git.return_value = tmp_path / ".git"
        mock_build.return_value = "img"

        repo_name = tmp_path.name
        result_data = {"container": f"sandbox-{repo_name}-foo", "name": "foo", "branch": "foo",
                        "exitCode": 0, "response": "Built it."}
        mock_bg.return_value = result_data

        result = runner.invoke(cli, ["start", "foo", "--task", "build"])
        assert result.exit_code == 0

        # Now read should find the saved result
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(exist_ok=True)
        (logs_dir / f"sandbox-{repo_name}-foo.json").write_text(json.dumps(result_data))

        with patch("sandbox_cli.get_logs_dir", return_value=logs_dir):
            result = runner.invoke(cli, ["read", "foo"])
            assert result.exit_code == 0
            output = json.loads(result.output)
            assert output["response"] == "Built it."

    def test_rm_frees_name_for_reuse(self, runner, cli, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "sandbox-myrepo-foo.json").write_text('{"exitCode": 0}')
        (logs_dir / "sandbox-myrepo-foo.log").write_text('raw logs')

        with patch("sandbox_cli.get_logs_dir", return_value=logs_dir), \
             patch("sandbox_cli.get_repo_root", return_value=Path("/Users/test/myrepo")), \
             patch("sandbox_cli.docker_container_rm", return_value=False), \
             patch("sandbox_cli.git_worktree_remove", return_value=False), \
             patch("sandbox_cli.git_worktree_list", return_value=[]):
            result = runner.invoke(cli, ["rm", "foo"])

        assert not (logs_dir / "sandbox-myrepo-foo.json").exists()
        assert not (logs_dir / "sandbox-myrepo-foo.log").exists()


class TestHelpShowsAllCommands:
    def test_help_lists_commands(self, runner, cli):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "start" in result.output
        assert "read" in result.output
        assert "rm" in result.output
        assert "ls" in result.output
