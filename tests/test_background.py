"""Tests for run_sandbox_background lifecycle."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sandbox_cli import run_sandbox_background


@pytest.fixture(autouse=True)
def mock_auth_token():
    with patch("sandbox_cli.get_auth_token", return_value="test-token"):
        yield


class TestConflictChecks:
    """Background mode should exit on any naming conflict."""

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    def test_exits_on_container_conflict(self, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = True
        mock_branch.return_value = False
        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="img", task="do it", logs_dir=tmp_path / "logs",
        )
        assert "error" in result
        assert "exists" in result["error"].lower()

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    def test_exits_on_branch_conflict(self, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = True
        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="img", task="do it", logs_dir=tmp_path / "logs",
        )
        assert "error" in result
        assert "branch" in result["error"].lower()

    @patch("sandbox_cli.get_worktrees_dir")
    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    def test_exits_on_worktree_conflict(self, mock_branch, mock_container, mock_run, mock_wt_dir, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_dir.return_value = tmp_path / "worktrees"
        worktree = tmp_path / "worktrees" / "myrepo__test"
        worktree.mkdir(parents=True)
        try:
            result = run_sandbox_background(
                name="test", repo_root=tmp_path, repo_name="myrepo",
                main_git=tmp_path / ".git",
                image="img", task="do it", logs_dir=tmp_path / "logs",
            )
            assert "error" in result
            assert "worktree" in result["error"].lower()
        finally:
            worktree.rmdir()

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    def test_exits_on_log_file_conflict(self, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "sandbox-myrepo-test.json").write_text('{"status": "running"}')
        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="img", task="do it", logs_dir=logs_dir,
        )
        assert "error" in result
        assert "exists" in result["error"].lower()


class TestContainerNaming:
    """Container name should be sandbox-{repo}-{sname}."""

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    def test_container_name_no_repo_prefix(self, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = True  # trigger conflict so we get result early
        mock_branch.return_value = False
        result = run_sandbox_background(
            name="proto-1", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="img", task="do it", logs_dir=tmp_path / "logs",
        )
        assert result["container"] == "sandbox-myrepo-proto-1"

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    def test_branch_field_uses_original_name(self, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = True
        mock_branch.return_value = False
        result = run_sandbox_background(
            name="feature/auth", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="img", task="do it", logs_dir=tmp_path / "logs",
        )
        assert result["container"] == "sandbox-myrepo-feature-auth"
        assert result["branch"] == "feature/auth"
        assert result["name"] == "feature-auth"


def _make_run_mock(overrides=None):
    """Create a mock for sandbox.run that handles the standard lifecycle commands."""
    custom = overrides or {}
    def side_effect(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        for pattern, response in custom.items():
            if pattern in cmd_str:
                return MagicMock(**response) if isinstance(response, dict) else response
        if "rev-parse" in cmd_str and "HEAD" in cmd_str:
            return MagicMock(returncode=0, stdout="abc123\n")
        if "docker run" in cmd_str:
            return MagicMock(returncode=0, stdout="cid\n")
        if "docker wait" in cmd_str:
            return MagicMock(returncode=0, stdout="0\n")
        if "docker logs" in cmd_str:
            return MagicMock(returncode=0, stdout='{"type": "result", "result": "All done."}\n')
        if "status" in cmd_str and "--porcelain" in cmd_str:
            return MagicMock(returncode=0, stdout="M src/main.py\n")
        if "add" in cmd_str and "-A" in cmd_str:
            return MagicMock(returncode=0, stdout="")
        if "commit" in cmd_str and "-m" in cmd_str:
            return MagicMock(returncode=0, stdout="")
        if "push" in cmd_str and "origin" in cmd_str:
            return MagicMock(returncode=0, stdout="")
        if "diff" in cmd_str and "--numstat" in cmd_str:
            return MagicMock(returncode=0, stdout="10\t2\tsrc/main.py\n")
        if "rev-parse" in cmd_str:
            return MagicMock(returncode=0, stdout="def456\n")
        return MagicMock(returncode=0, stdout="")
    return side_effect


class TestSuccessLifecycle:
    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    def test_full_success(self, mock_wt_rm, mock_container_rm, mock_copy_env,
                           mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_wt_rm.return_value = True
        mock_container_rm.return_value = True
        mock_run.side_effect = _make_run_mock()

        logs_dir = tmp_path / "logs"
        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do something", logs_dir=logs_dir,
        )

        assert result["exitCode"] == 0
        assert result["name"] == "test"
        assert result["branch"] == "test"
        assert result["container"] == "sandbox-myrepo-test"
        assert result["response"] == "All done."
        assert result["diffStats"] == {"filesChanged": 1, "insertions": 10, "deletions": 2}
        assert "error" not in result
        assert "worktreePath" in result
        assert "modifiedFiles" in result
        assert (logs_dir / "sandbox-myrepo-test.json").exists()
        assert (logs_dir / "sandbox-myrepo-test.log").exists()
        mock_container_rm.assert_called_once()
        # Worktree NOT removed by default (agent inspects it)
        mock_wt_rm.assert_not_called()

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    def test_writes_state_file_before_docker_run(self, mock_wt_rm, mock_container_rm, mock_copy_env,
                                                   mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_wt_rm.return_value = True
        mock_container_rm.return_value = True

        logs_dir = tmp_path / "logs"
        state_captured = {}

        def run_with_state_check(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "docker run" in cmd_str:
                # At this point the state file should already exist
                state_file = logs_dir / "sandbox-myrepo-test.json"
                if state_file.exists():
                    state_captured.update(json.loads(state_file.read_text()))
                return MagicMock(returncode=0, stdout="cid\n")
            return _make_run_mock()(cmd, **kwargs)

        mock_run.side_effect = run_with_state_check

        run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=logs_dir,
        )

        assert state_captured.get("status") == "running"
        assert state_captured.get("container") == "sandbox-myrepo-test"


class TestLaunchFailureCleanup:
    """When docker launch fails, both worktree and branch should be cleaned up."""

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.git_worktree_remove")
    def test_branch_deleted_on_launch_failure(self, mock_wt_rm, mock_copy_env,
                                                mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_wt_rm.return_value = True

        branch_deleted = []

        def run_side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                return MagicMock(returncode=0, stdout="abc123\n")
            if "docker run" in cmd_str:
                return MagicMock(returncode=1, stdout="", stderr="launch failed")
            if "branch" in cmd_str and "-D" in cmd_str:
                branch_deleted.append(cmd)
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = run_side_effect

        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
        )

        assert "error" in result
        assert len(branch_deleted) > 0, "Should delete the branch on launch failure"


class TestPushOutcome:
    """Result JSON should indicate whether push succeeded."""

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    def test_push_failure_surfaced_in_result(self, mock_wt_rm, mock_container_rm, mock_copy_env,
                                               mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_wt_rm.return_value = True
        mock_container_rm.return_value = True
        mock_run.side_effect = _make_run_mock({
            "push -u origin": {"returncode": 1, "stdout": "", "stderr": "rejected"},
        })

        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
            push=True,
        )

        assert result["exitCode"] == 0
        assert result["pushed"] is False

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    def test_push_success_surfaced_in_result(self, mock_wt_rm, mock_container_rm, mock_copy_env,
                                               mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_wt_rm.return_value = True
        mock_container_rm.return_value = True
        mock_run.side_effect = _make_run_mock()

        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
            push=True,
        )

        assert result["exitCode"] == 0
        assert result["pushed"] is True

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    def test_no_push_by_default(self, mock_wt_rm, mock_container_rm, mock_copy_env,
                                  mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_wt_rm.return_value = True
        mock_container_rm.return_value = True
        mock_run.side_effect = _make_run_mock()

        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
        )

        assert result["exitCode"] == 0
        assert result["pushed"] is False


class TestEnvExclusion:
    """Auto-commit should exclude .env* files to avoid pushing secrets."""

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    def test_git_reset_env_before_commit(self, mock_wt_rm, mock_container_rm, mock_copy_env,
                                           mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = [".env", ".env.local"]
        mock_wt_rm.return_value = True
        mock_container_rm.return_value = True

        reset_calls = []

        def run_with_tracking(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "reset" in cmd_str and ".env" in cmd_str:
                reset_calls.append(cmd)
                return MagicMock(returncode=0, stdout="")
            return _make_run_mock()(cmd, **kwargs)

        mock_run.side_effect = run_with_tracking

        run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
        )

        assert len(reset_calls) > 0, "Should run git reset to unstage .env files"


class TestFailurePaths:
    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    def test_preserves_worktree_on_commit_failure(self, mock_wt_rm, mock_container_rm, mock_copy_env,
                                                    mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_container_rm.return_value = True
        mock_run.side_effect = _make_run_mock({
            "commit": {"returncode": 1, "stdout": "", "stderr": "commit failed"},
        })

        logs_dir = tmp_path / "logs"
        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=logs_dir,
        )

        assert "error" in result
        assert "worktreePath" in result
        mock_wt_rm.assert_not_called()
        mock_container_rm.assert_called_once()

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    def test_no_push_on_nonzero_exit(self, mock_wt_rm, mock_container_rm, mock_copy_env,
                                      mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_wt_rm.return_value = True
        mock_container_rm.return_value = True

        push_called = []
        def run_with_push_tracking(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "push" in cmd_str and "origin" in cmd_str:
                push_called.append(True)
            if "docker wait" in cmd_str:
                return MagicMock(returncode=0, stdout="1\n")
            return _make_run_mock()(cmd, **kwargs)

        mock_run.side_effect = run_with_push_tracking

        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
        )

        assert result["exitCode"] == 1
        assert not push_called

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    def test_docker_wait_parse_error_defaults_nonzero(self, mock_wt_rm, mock_container_rm, mock_copy_env,
                                                        mock_wt_add, mock_branch, mock_container, mock_run, tmp_path):
        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_wt_rm.return_value = True
        mock_container_rm.return_value = True
        mock_run.side_effect = _make_run_mock({
            "docker wait": {"returncode": 1, "stdout": "Error: no such container\n"},
        })

        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
        )

        assert result["exitCode"] != 0
