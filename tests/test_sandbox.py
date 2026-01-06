"""Tests for sandbox CLI."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from sandbox import (
    safe_name,
    get_worktree_path,
    get_repo_root,
    get_main_git_dir,
    docker_container_ls,
    docker_container_rm,
    git_worktree_list,
)


class TestSafeName:
    """Tests for safe_name function."""

    def test_simple_name(self):
        assert safe_name("feature") == "feature"

    def test_with_slashes(self):
        assert safe_name("feature/auth") == "feature-auth"

    def test_multiple_slashes(self):
        assert safe_name("claude/integrate/api") == "claude-integrate-api"

    def test_no_slashes(self):
        assert safe_name("fix-bug-123") == "fix-bug-123"


class TestGetWorktreePath:
    """Tests for get_worktree_path function."""

    def test_basic_path(self):
        repo_root = Path("/Users/test/projects/myrepo")
        result = get_worktree_path(repo_root, "feature-auth")
        assert result == Path("/Users/test/projects/myrepo__feature-auth")

    def test_nested_repo(self):
        repo_root = Path("/Users/test/deep/nested/repo")
        result = get_worktree_path(repo_root, "task")
        assert result == Path("/Users/test/deep/nested/repo__task")


class TestGetRepoRoot:
    """Tests for get_repo_root function."""

    @patch("sandbox.run")
    def test_in_repo(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="/Users/test/myrepo\n"
        )
        result = get_repo_root()
        assert result == Path("/Users/test/myrepo")

    @patch("sandbox.run")
    def test_not_in_repo(self, mock_run):
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        result = get_repo_root()
        assert result is None


class TestGetMainGitDir:
    """Tests for get_main_git_dir function."""

    @patch("sandbox.run")
    def test_in_main_repo(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=".git\n")
        repo_root = Path("/Users/test/myrepo")
        result = get_main_git_dir(repo_root)
        assert result == Path("/Users/test/myrepo/.git")

    @patch("sandbox.run")
    def test_in_worktree(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="/Users/test/myrepo/.git\n"
        )
        repo_root = Path("/Users/test/myrepo__feature")
        result = get_main_git_dir(repo_root)
        assert result == Path("/Users/test/myrepo/.git")


class TestDockerContainerLs:
    """Tests for docker_container_ls function."""

    @patch("sandbox.run")
    def test_parses_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123\tsandbox-repo-test\tUp 2 hours\ndef456\tsandbox-repo-other\tExited (0) 1 hour ago"
        )
        result = docker_container_ls()
        assert len(result) == 2
        assert result[0]["name"] == "sandbox-repo-test"
        assert result[0]["status"] == "Up 2 hours"
        assert result[1]["name"] == "sandbox-repo-other"

    @patch("sandbox.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = docker_container_ls()
        assert result == []

    @patch("sandbox.run")
    def test_command_fails(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = docker_container_ls()
        assert result == []


class TestGitWorktreeList:
    """Tests for git_worktree_list function."""

    @patch("sandbox.run")
    def test_parses_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="""worktree /Users/test/repo
HEAD abc123
branch refs/heads/main

worktree /Users/test/repo__feature
HEAD def456
branch refs/heads/feature/auth"""
        )
        result = git_worktree_list()
        assert len(result) == 2
        assert result[0]["path"] == "/Users/test/repo"
        assert result[0]["branch"] == "refs/heads/main"
        assert result[1]["path"] == "/Users/test/repo__feature"

    @patch("sandbox.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = git_worktree_list()
        assert result == []


class TestCLIIntegration:
    """Integration tests for CLI commands using Click's test runner."""

    @pytest.fixture
    def runner(self):
        from click.testing import CliRunner
        return CliRunner()

    @pytest.fixture
    def cli(self):
        from sandbox import cli
        return cli

    def test_help(self, runner, cli):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Docker Sandbox CLI" in result.output
        assert "start" in result.output
        assert "checkout" not in result.output  # Removed, start handles both

    @patch("sandbox.get_repo_root")
    def test_ls_not_in_repo(self, mock_get_repo, runner, cli):
        mock_get_repo.return_value = None
        result = runner.invoke(cli, ["ls"])
        assert result.exit_code == 1
        assert "Not in a git repository" in result.output

    @patch("sandbox.get_repo_root")
    @patch("sandbox.docker_container_rm")
    @patch("sandbox.git_worktree_remove")
    def test_rm_not_in_repo(self, mock_wt_rm, mock_container_rm, mock_get_repo, runner, cli):
        mock_get_repo.return_value = None
        result = runner.invoke(cli, ["rm", "test"])
        assert result.exit_code == 1
        assert "Not in a git repository" in result.output

    @patch("sandbox.get_repo_root")
    @patch("sandbox.docker_container_rm")
    @patch("sandbox.git_worktree_remove")
    def test_rm_with_slash_in_name(self, mock_wt_rm, mock_container_rm, mock_get_repo, runner, cli):
        mock_get_repo.return_value = Path("/Users/test/myrepo")
        mock_container_rm.return_value = True
        mock_wt_rm.return_value = True

        result = runner.invoke(cli, ["rm", "feature/auth"])

        # Should convert to safe name (container name includes repo)
        mock_container_rm.assert_called_with("sandbox-myrepo-feature-auth")
        assert "feature-auth" in result.output

    @patch("sandbox.get_repo_root")
    def test_start_not_in_repo(self, mock_get_repo, runner, cli):
        mock_get_repo.return_value = None
        result = runner.invoke(cli, ["start", "my-task"])
        assert result.exit_code == 1
        assert "Not in a git repository" in result.output

    @patch("sandbox.get_repo_root")
    @patch("sandbox.docker_container_ls")
    @patch("sandbox.git_worktree_list")
    @patch("sandbox.docker_container_rm")
    @patch("sandbox.git_worktree_remove")
    @patch("sandbox.run")
    def test_purge(self, mock_run, mock_wt_rm, mock_container_rm, mock_wt_list, mock_container_ls, mock_get_repo, runner, cli):
        mock_get_repo.return_value = Path("/Users/test/myrepo")
        mock_container_ls.return_value = [
            {"id": "abc123", "name": "sandbox-myrepo-test", "status": "Up 2 hours"},
            {"id": "def456", "name": "sandbox-other-feature", "status": "Up 1 hour"},
        ]
        mock_wt_list.return_value = [
            {"path": "/Users/test/myrepo", "branch": "refs/heads/main"},
            {"path": "/Users/test/myrepo__test", "branch": "refs/heads/task/test"},
        ]
        mock_container_rm.return_value = True
        mock_wt_rm.return_value = True
        mock_run.return_value = MagicMock(returncode=0)

        result = runner.invoke(cli, ["purge"])

        assert result.exit_code == 0
        # Should only remove container matching repo name
        mock_container_rm.assert_called_once_with("sandbox-myrepo-test")
        # Should only remove worktree matching repo name pattern
        mock_wt_rm.assert_called_once()
