"""Tests for run_sandbox_remote — Modal remote runtime.

Follows red-green TDD: tests express desired behavior, mocking Modal SDK
so these run without Modal installed.
"""

import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, call, patch

import pytest

from sandbox_cli import run_sandbox_remote, safe_name


# ---------------------------------------------------------------------------
# Modal mock helpers
# ---------------------------------------------------------------------------

class _MockNotFoundError(Exception):
    pass


class _MockSandboxTimeoutError(Exception):
    pass


class _MockSandboxTerminatedError(Exception):
    pass


class _MockExecTimeoutError(Exception):
    pass


def _make_modal_mock():
    """Return a MagicMock representing the modal package with realistic sub-objects."""
    mock_exception = MagicMock()
    mock_exception.NotFoundError = _MockNotFoundError
    mock_exception.SandboxTimeoutError = _MockSandboxTimeoutError
    mock_exception.SandboxTerminatedError = _MockSandboxTerminatedError
    mock_exception.ExecTimeoutError = _MockExecTimeoutError

    mock_modal = MagicMock()
    mock_modal.exception = mock_exception
    mock_modal.App.lookup.return_value = MagicMock()
    mock_modal.Secret.from_dict.return_value = MagicMock()
    # Image chain: .apt_install().run_commands() returns the image mock
    image_mock = MagicMock()
    mock_modal.Image.debian_slim.return_value.apt_install.return_value = image_mock
    image_mock.run_commands.return_value = image_mock

    return mock_modal, mock_exception


def _make_sandbox_mock(object_id="sb-abc123", stdout_chunks=None, returncode=0):
    """Return a mock Modal Sandbox object."""
    sb = MagicMock()
    sb.object_id = object_id
    sb.poll.return_value = returncode  # already exited
    sb.returncode = returncode
    sb.wait.return_value = returncode

    chunks = stdout_chunks or ['{"type": "result", "result": "Task done."}\n',
                                '__SANDBOX_RESULT__ {"exitCode": 0, "commitSha": "abc123", '
                                '"modifiedFiles": ["src/app.py"], '
                                '"diffStats": {"filesChanged": 1, "insertions": 10, "deletions": 2}, '
                                '"pushed": true}\n']

    proc_mock = MagicMock()
    proc_mock.stdout.__iter__ = lambda self: iter(chunks)
    proc_mock.wait.return_value = returncode

    sb.exec.return_value = proc_mock
    sb.open.return_value.__enter__ = MagicMock(return_value=MagicMock())
    sb.open.return_value.__exit__ = MagicMock(return_value=False)

    return sb, proc_mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_auth_token():
    with patch("sandbox_cli.get_auth_token", return_value="test-token"):
        yield


@pytest.fixture(autouse=True)
def mock_modal_modules(tmp_path):
    """Inject a mock modal module into sys.modules for the duration of each test."""
    mock_modal, mock_exc = _make_modal_mock()
    modules = {
        "modal": mock_modal,
        "modal.exception": mock_exc,
    }
    with patch.dict(sys.modules, modules):
        yield mock_modal


@pytest.fixture
def git_run_mock():
    """Standard git command mock responses for remote sandbox flow."""
    def side_effect(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "fetch" in cmd_str:
            return MagicMock(returncode=0, stdout="")
        if "show-ref" in cmd_str and "remotes/origin" in cmd_str:
            return MagicMock(returncode=1, stdout="")  # branch does NOT exist
        if "remote" in cmd_str and "get-url" in cmd_str:
            return MagicMock(returncode=0, stdout="https://github.com/user/repo.git\n")
        if "push" in cmd_str and "HEAD:refs/heads" in cmd_str:
            return MagicMock(returncode=0, stdout="")
        if "rev-parse" in cmd_str and "HEAD" in cmd_str:
            return MagicMock(returncode=0, stdout="base-sha-abc\n")
        return MagicMock(returncode=0, stdout="")
    return side_effect


# ---------------------------------------------------------------------------
# Auth & validation tests (RED: run these before implementation)
# ---------------------------------------------------------------------------

class TestValidation:
    def test_missing_gh_token_returns_error(self, tmp_path, mock_modal_modules, git_run_mock):
        with patch("sandbox_cli.get_gh_token", return_value=""):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                result = run_sandbox_remote(
                    name="test", repo_root=tmp_path, repo_name="myrepo",
                    task="do it", logs_dir=tmp_path / "logs",
                )
        assert "error" in result
        assert "GH_TOKEN" in result["error"]

    def test_state_file_conflict_returns_error(self, tmp_path, mock_modal_modules, git_run_mock):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "sandbox-myrepo-test.json").write_text('{"status": "running"}')
        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                result = run_sandbox_remote(
                    name="test", repo_root=tmp_path, repo_name="myrepo",
                    task="do it", logs_dir=logs_dir,
                )
        assert "error" in result
        assert "exists" in result["error"].lower()

    def test_remote_branch_exists_returns_error(self, tmp_path, mock_modal_modules):
        def run_side(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "fetch" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "show-ref" in cmd_str and "remotes/origin" in cmd_str:
                return MagicMock(returncode=0, stdout="abc123 refs/remotes/origin/test\n")
            return MagicMock(returncode=0, stdout="")

        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=run_side):
                result = run_sandbox_remote(
                    name="test", repo_root=tmp_path, repo_name="myrepo",
                    task="do it", logs_dir=tmp_path / "logs",
                )
        assert "error" in result
        assert "remote branch" in result["error"].lower()

    def test_initial_push_failure_returns_error(self, tmp_path, mock_modal_modules):
        def run_side(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "fetch" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "show-ref" in cmd_str and "remotes/origin" in cmd_str:
                return MagicMock(returncode=1, stdout="")
            if "remote" in cmd_str and "get-url" in cmd_str:
                return MagicMock(returncode=0, stdout="https://github.com/user/repo.git\n")
            if "push" in cmd_str and "HEAD:refs/heads" in cmd_str:
                return MagicMock(returncode=1, stdout="", stderr="remote: Permission denied\n")
            if "rev-parse" in cmd_str:
                return MagicMock(returncode=0, stdout="base-sha\n")
            return MagicMock(returncode=0, stdout="")

        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=run_side):
                result = run_sandbox_remote(
                    name="test", repo_root=tmp_path, repo_name="myrepo",
                    task="do it", logs_dir=tmp_path / "logs",
                )
        assert "error" in result
        assert "remote branch" in result["error"].lower() or "Failed" in result["error"]

    def test_modal_auth_failure_returns_error(self, tmp_path, mock_modal_modules, git_run_mock):
        mock_modal_modules.App.lookup.side_effect = Exception("Not authenticated")
        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                result = run_sandbox_remote(
                    name="test", repo_root=tmp_path, repo_name="myrepo",
                    task="do it", logs_dir=tmp_path / "logs",
                )
        assert "error" in result
        assert "Modal auth" in result["error"] or "modal" in result["error"].lower()

    def test_provider_auth_failure_returns_error(self, tmp_path, mock_modal_modules, git_run_mock):
        with patch("sandbox_cli.get_auth_token", return_value=None):
            with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
                with patch("sandbox_cli.run", side_effect=git_run_mock):
                    result = run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=tmp_path / "logs",
                        provider="claude",
                    )
        assert "error" in result
        assert "auth" in result["error"].lower() or "token" in result["error"].lower()


# ---------------------------------------------------------------------------
# State file tests
# ---------------------------------------------------------------------------

class TestStateFile:
    def test_writes_running_state_before_sandbox_create(self, tmp_path, mock_modal_modules, git_run_mock):
        """State file must exist with status=running before Sandbox.create() is called."""
        logs_dir = tmp_path / "logs"
        state_at_create = {}

        sb_mock, _ = _make_sandbox_mock()

        def capture_state(*args, **kwargs):
            state_file = logs_dir / "sandbox-myrepo-test.json"
            if state_file.exists():
                state_at_create.update(json.loads(state_file.read_text()))
            return sb_mock

        mock_modal_modules.Sandbox.create.side_effect = capture_state

        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                with patch("sandbox_cli.get_sandbox_cli_dir") as mock_dir:
                    runner = tmp_path / "modal_runner.sh"
                    runner.write_bytes(b"#!/bin/bash\necho done")
                    mock_dir.return_value = tmp_path
                    (tmp_path / "scripts").mkdir(exist_ok=True)
                    (tmp_path / "scripts" / "modal_runner.sh").write_bytes(b"#!/bin/bash\necho done")
                    run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=logs_dir,
                    )

        assert state_at_create.get("status") == "running"
        assert state_at_create.get("runtime") == "modal"

    def test_updates_state_with_sandbox_id(self, tmp_path, mock_modal_modules, git_run_mock):
        logs_dir = tmp_path / "logs"
        sb_mock, _ = _make_sandbox_mock(object_id="sb-xyz999")
        mock_modal_modules.Sandbox.create.return_value = sb_mock

        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                with patch("sandbox_cli.get_sandbox_cli_dir") as mock_dir:
                    mock_dir.return_value = tmp_path
                    (tmp_path / "scripts").mkdir(exist_ok=True)
                    (tmp_path / "scripts" / "modal_runner.sh").write_bytes(b"#!/bin/bash\necho done")
                    run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=logs_dir,
                    )

        # Final state should NOT have status:running (it's overwritten with result)
        state_file = logs_dir / "sandbox-myrepo-test.json"
        assert state_file.exists()
        final = json.loads(state_file.read_text())
        assert "status" not in final
        assert final.get("runtime") == "modal"

    def test_final_state_has_correct_shape(self, tmp_path, mock_modal_modules, git_run_mock):
        logs_dir = tmp_path / "logs"
        sb_mock, _ = _make_sandbox_mock()
        mock_modal_modules.Sandbox.create.return_value = sb_mock

        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                with patch("sandbox_cli.get_sandbox_cli_dir") as mock_dir:
                    mock_dir.return_value = tmp_path
                    (tmp_path / "scripts").mkdir(exist_ok=True)
                    (tmp_path / "scripts" / "modal_runner.sh").write_bytes(b"#!/bin/bash\necho done")
                    result = run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=logs_dir,
                    )

        assert result["runtime"] == "modal"
        assert result["provider"] == "claude"
        assert result["name"] == "test"
        assert result["branch"] == "test"
        assert "exitCode" in result
        assert "modifiedFiles" in result
        assert "pushed" in result


# ---------------------------------------------------------------------------
# Success lifecycle tests
# ---------------------------------------------------------------------------

class TestSuccessLifecycle:
    def _run(self, tmp_path, mock_modal_modules, git_run_mock, stdout_chunks=None):
        logs_dir = tmp_path / "logs"
        sb_mock, proc_mock = _make_sandbox_mock(stdout_chunks=stdout_chunks)
        mock_modal_modules.Sandbox.create.return_value = sb_mock

        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                with patch("sandbox_cli.get_sandbox_cli_dir") as mock_dir:
                    mock_dir.return_value = tmp_path
                    (tmp_path / "scripts").mkdir(exist_ok=True)
                    (tmp_path / "scripts" / "modal_runner.sh").write_bytes(b"#!/bin/bash\necho done")
                    result = run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=logs_dir,
                    )
        return result, logs_dir

    def test_success_exit_code_zero(self, tmp_path, mock_modal_modules, git_run_mock):
        result, _ = self._run(tmp_path, mock_modal_modules, git_run_mock)
        assert result["exitCode"] == 0

    def test_result_includes_response(self, tmp_path, mock_modal_modules, git_run_mock):
        result, _ = self._run(tmp_path, mock_modal_modules, git_run_mock)
        assert result.get("response") == "Task done."

    def test_result_includes_commit_sha(self, tmp_path, mock_modal_modules, git_run_mock):
        result, _ = self._run(tmp_path, mock_modal_modules, git_run_mock)
        assert result.get("commitSha") == "abc123"

    def test_result_includes_diff_stats(self, tmp_path, mock_modal_modules, git_run_mock):
        result, _ = self._run(tmp_path, mock_modal_modules, git_run_mock)
        assert result.get("diffStats") == {"filesChanged": 1, "insertions": 10, "deletions": 2}

    def test_result_pushed_true(self, tmp_path, mock_modal_modules, git_run_mock):
        result, _ = self._run(tmp_path, mock_modal_modules, git_run_mock)
        assert result["pushed"] is True

    def test_log_raw_written(self, tmp_path, mock_modal_modules, git_run_mock):
        result, logs_dir = self._run(tmp_path, mock_modal_modules, git_run_mock)
        log_raw = logs_dir / "sandbox-myrepo-test.log"
        assert log_raw.exists()
        content = log_raw.read_text()
        assert "__SANDBOX_RESULT__" in content

    def test_sandbox_terminated_after_completion(self, tmp_path, mock_modal_modules, git_run_mock):
        sb_mock, _ = _make_sandbox_mock()
        mock_modal_modules.Sandbox.create.return_value = sb_mock

        logs_dir = tmp_path / "logs"
        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                with patch("sandbox_cli.get_sandbox_cli_dir") as mock_dir:
                    mock_dir.return_value = tmp_path
                    (tmp_path / "scripts").mkdir(exist_ok=True)
                    (tmp_path / "scripts" / "modal_runner.sh").write_bytes(b"#!/bin/bash\necho done")
                    run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=logs_dir,
                    )

        sb_mock.terminate.assert_called()
        sb_mock.detach.assert_called()

    def test_runner_script_uploaded_to_sandbox(self, tmp_path, mock_modal_modules, git_run_mock):
        sb_mock, _ = _make_sandbox_mock()
        mock_modal_modules.Sandbox.create.return_value = sb_mock

        logs_dir = tmp_path / "logs"
        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                with patch("sandbox_cli.get_sandbox_cli_dir") as mock_dir:
                    mock_dir.return_value = tmp_path
                    (tmp_path / "scripts").mkdir(exist_ok=True)
                    (tmp_path / "scripts" / "modal_runner.sh").write_bytes(b"#!/bin/bash\necho done")
                    run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=logs_dir,
                    )

        sb_mock.open.assert_called_once_with("/tmp/modal_runner.sh", "wb")

    def test_exec_called_with_runner_and_args(self, tmp_path, mock_modal_modules, git_run_mock):
        sb_mock, _ = _make_sandbox_mock()
        mock_modal_modules.Sandbox.create.return_value = sb_mock

        logs_dir = tmp_path / "logs"
        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                with patch("sandbox_cli.get_sandbox_cli_dir") as mock_dir:
                    mock_dir.return_value = tmp_path
                    (tmp_path / "scripts").mkdir(exist_ok=True)
                    (tmp_path / "scripts" / "modal_runner.sh").write_bytes(b"#!/bin/bash\necho done")
                    run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=logs_dir,
                    )

        args, kwargs = sb_mock.exec.call_args
        assert args[0] == "bash"
        assert args[1] == "/tmp/modal_runner.sh"
        assert "https://github.com/user/repo.git" in args
        assert "test" in args
        assert kwargs.get("pty") is False


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def _run_with_exc(self, tmp_path, mock_modal_modules, git_run_mock, exc):
        sb_mock, _ = _make_sandbox_mock()
        mock_modal_modules.Sandbox.create.return_value = sb_mock
        sb_mock.exec.side_effect = exc

        logs_dir = tmp_path / "logs"
        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                with patch("sandbox_cli.get_sandbox_cli_dir") as mock_dir:
                    mock_dir.return_value = tmp_path
                    (tmp_path / "scripts").mkdir(exist_ok=True)
                    (tmp_path / "scripts" / "modal_runner.sh").write_bytes(b"#!/bin/bash\necho done")
                    result = run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=logs_dir,
                    )
        return result, sb_mock

    def test_sandbox_timeout_returns_error(self, tmp_path, mock_modal_modules, git_run_mock):
        result, sb_mock = self._run_with_exc(
            tmp_path, mock_modal_modules, git_run_mock, _MockSandboxTimeoutError("timed out")
        )
        assert "error" in result
        assert result["exitCode"] == 1
        sb_mock.terminate.assert_called()

    def test_sandbox_terminated_returns_error(self, tmp_path, mock_modal_modules, git_run_mock):
        result, sb_mock = self._run_with_exc(
            tmp_path, mock_modal_modules, git_run_mock, _MockSandboxTerminatedError("killed")
        )
        assert "error" in result
        assert result["exitCode"] == 1
        sb_mock.terminate.assert_called()

    def test_exec_timeout_returns_error(self, tmp_path, mock_modal_modules, git_run_mock):
        result, sb_mock = self._run_with_exc(
            tmp_path, mock_modal_modules, git_run_mock, _MockExecTimeoutError("exec timeout")
        )
        assert "error" in result
        assert result["exitCode"] == 1

    def test_generic_exception_returns_error(self, tmp_path, mock_modal_modules, git_run_mock):
        result, sb_mock = self._run_with_exc(
            tmp_path, mock_modal_modules, git_run_mock, RuntimeError("unexpected")
        )
        assert "error" in result
        assert result["exitCode"] == 1
        sb_mock.terminate.assert_called()

    def test_nonzero_agent_exit_included_in_result(self, tmp_path, mock_modal_modules, git_run_mock):
        chunks = [
            '{"type": "result", "result": "Partial work."}\n',
            '__SANDBOX_RESULT__ {"exitCode": 1, "commitSha": "", '
            '"modifiedFiles": [], "diffStats": {}, "pushed": false}\n',
        ]
        sb_mock, proc_mock = _make_sandbox_mock(returncode=1, stdout_chunks=chunks)
        mock_modal_modules.Sandbox.create.return_value = sb_mock

        logs_dir = tmp_path / "logs"
        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=git_run_mock):
                with patch("sandbox_cli.get_sandbox_cli_dir") as mock_dir:
                    mock_dir.return_value = tmp_path
                    (tmp_path / "scripts").mkdir(exist_ok=True)
                    (tmp_path / "scripts" / "modal_runner.sh").write_bytes(b"#!/bin/bash\necho done")
                    result = run_sandbox_remote(
                        name="test", repo_root=tmp_path, repo_name="myrepo",
                        task="do it", logs_dir=logs_dir,
                    )

        assert result["exitCode"] == 1
        assert "error" in result


# ---------------------------------------------------------------------------
# Naming tests
# ---------------------------------------------------------------------------

class TestNaming:
    def test_branch_with_slash(self, tmp_path, mock_modal_modules):
        """Branch name feature/foo uses safe_name for container, raw name for branch field."""
        def run_side(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "fetch" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "show-ref" in cmd_str and "remotes/origin" in cmd_str:
                return MagicMock(returncode=0, stdout="abc refs/remotes/origin/feature/foo\n")
            return MagicMock(returncode=0, stdout="")

        with patch("sandbox_cli.get_gh_token", return_value="ghp_token"):
            with patch("sandbox_cli.run", side_effect=run_side):
                result = run_sandbox_remote(
                    name="feature/foo", repo_root=tmp_path, repo_name="myrepo",
                    task="do it", logs_dir=tmp_path / "logs",
                )

        # remote branch exists → should error, but name fields should still be set
        assert result["name"] == "feature-foo"
        assert result["branch"] == "feature/foo"


# ---------------------------------------------------------------------------
# sandbox read with Modal state files
# ---------------------------------------------------------------------------

class TestReadModalCommand:
    @pytest.fixture
    def runner(self):
        from click.testing import CliRunner
        return CliRunner()

    @pytest.fixture
    def cli(self):
        from sandbox_cli import cli
        return cli

    def test_read_returns_completed_modal_result(self, runner, cli, tmp_path):
        """read returns stored result without touching Modal when status is absent."""
        from unittest.mock import patch as _patch
        result_data = {
            "name": "test", "branch": "test", "runtime": "modal",
            "provider": "claude", "exitCode": 0, "pushed": True,
            "response": "Done.", "modifiedFiles": [],
        }
        with _patch("sandbox_cli.get_repo_root", return_value=Path("/repo/myrepo")):
            with _patch("sandbox_cli.get_logs_dir", return_value=tmp_path):
                (tmp_path / "sandbox-myrepo-test.json").write_text(json.dumps(result_data))
                result = runner.invoke(cli, ["read", "test"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["runtime"] == "modal"
        assert output["response"] == "Done."

    def test_read_modal_running_no_sandbox_id_returns_error(self, runner, cli, tmp_path):
        state = {"status": "running", "runtime": "modal", "provider": "claude",
                 "name": "test", "branch": "test", "sandboxId": None}
        with patch("sandbox_cli.get_repo_root", return_value=Path("/repo/myrepo")):
            with patch("sandbox_cli.get_logs_dir", return_value=tmp_path):
                (tmp_path / "sandbox-myrepo-test.json").write_text(json.dumps(state))
                result = runner.invoke(cli, ["read", "test"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert "error" in output

    def test_read_modal_sandbox_expired_returns_error(self, runner, cli, tmp_path, mock_modal_modules):
        state = {"status": "running", "runtime": "modal", "provider": "claude",
                 "name": "test", "branch": "test", "sandboxId": "sb-expired"}

        mock_modal_modules.Sandbox.from_id.side_effect = _MockNotFoundError("not found")

        with patch("sandbox_cli.get_repo_root", return_value=Path("/repo/myrepo")):
            with patch("sandbox_cli.get_logs_dir", return_value=tmp_path):
                (tmp_path / "sandbox-myrepo-test.json").write_text(json.dumps(state))
                result = runner.invoke(cli, ["read", "test"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert "error" in output
        assert "expired" in output["error"].lower() or "not found" in output["error"].lower()

    def test_read_modal_reconnects_and_finalizes(self, runner, cli, tmp_path, mock_modal_modules):
        """read reconnects to running Modal sandbox, reads remaining output, returns result."""
        state = {"status": "running", "runtime": "modal", "provider": "claude",
                 "name": "test", "branch": "test", "sandboxId": "sb-live-123",
                 "baseCommit": "base-abc"}

        modal_sb = MagicMock()
        modal_sb.poll.return_value = 0  # already done
        modal_sb.returncode = 0
        modal_sb.wait.return_value = 0
        remaining_output = (
            '{"type": "result", "result": "Recovered result."}\n'
            '__SANDBOX_RESULT__ {"exitCode": 0, "commitSha": "rec123", '
            '"modifiedFiles": ["a.py"], "diffStats": {"filesChanged": 1, '
            '"insertions": 5, "deletions": 1}, "pushed": true}\n'
        )
        modal_sb.stdout.read.return_value = remaining_output
        mock_modal_modules.Sandbox.from_id.return_value = modal_sb

        with patch("sandbox_cli.get_repo_root", return_value=Path("/repo/myrepo")):
            with patch("sandbox_cli.get_logs_dir", return_value=tmp_path):
                (tmp_path / "sandbox-myrepo-test.json").write_text(json.dumps(state))
                result = runner.invoke(cli, ["read", "test"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output.get("exitCode") == 0
        assert output.get("runtime") == "modal"
        assert output.get("response") == "Recovered result."
        assert output.get("commitSha") == "rec123"
        assert output.get("pushed") is True
        modal_sb.terminate.assert_called()
        modal_sb.detach.assert_called()


# ---------------------------------------------------------------------------
# CLI --remote flag tests
# ---------------------------------------------------------------------------

class TestStartRemoteFlag:
    @pytest.fixture
    def runner(self):
        from click.testing import CliRunner
        return CliRunner()

    @pytest.fixture
    def cli(self):
        from sandbox_cli import cli
        return cli

    def test_remote_without_task_exits_with_error(self, runner, cli):
        with patch("sandbox_cli.get_repo_root", return_value=Path("/repo/myrepo")):
            result = runner.invoke(cli, ["start", "--remote", "test"])
        assert result.exit_code != 0
        assert "background task mode" in result.output or "background task mode" in (result.output + (result.stderr or ""))

    def test_remote_with_continue_exits_with_error(self, runner, cli):
        with patch("sandbox_cli.get_repo_root", return_value=Path("/repo/myrepo")):
            result = runner.invoke(cli, ["start", "--remote", "--continue", "--task", "do it", "test"])
        assert result.exit_code != 0

    def test_remote_routes_to_run_sandbox_remote(self, runner, cli, tmp_path, mock_modal_modules):
        with patch("sandbox_cli.get_repo_root", return_value=tmp_path):
            with patch("sandbox_cli.run_sandbox_remote") as mock_remote:
                mock_remote.return_value = {"name": "test", "branch": "test",
                                             "runtime": "modal", "exitCode": 0,
                                             "modifiedFiles": [], "pushed": True}
                result = runner.invoke(cli, ["start", "--remote", "--task", "do it", "test"])

        assert result.exit_code == 0
        mock_remote.assert_called_once()
        call_kwargs = mock_remote.call_args
        assert call_kwargs.kwargs.get("task") == "do it" or call_kwargs.args[3] == "do it"
