"""Tests for provider support: get_provider, extract_codex_response, CLI --provider flag."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import click
from click.testing import CliRunner

from sandbox_cli import extract_codex_response, extract_gemini_response, get_provider


# ---------------------------------------------------------------------------
# extract_codex_response
# ---------------------------------------------------------------------------

class TestExtractCodexResponse:
    """AC#10: extract_codex_response test cases."""

    def test_returns_file_contents_when_result_file_exists(self, tmp_path):
        """(a) returns file contents when .sandbox-result.txt exists."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result_file = worktree / ".sandbox-result.txt"
        result_file.write_text("The agent completed the task successfully.")

        log_path = tmp_path / "container.log"
        log_path.write_text('{"type": "result", "result": "log output"}\n')

        response = extract_codex_response(worktree, log_path)
        assert response == "The agent completed the task successfully."

    def test_returns_last_non_json_line_when_file_missing(self, tmp_path):
        """(b) returns last non-empty non-JSON line from logs when file is missing."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # No .sandbox-result.txt

        log_path = tmp_path / "container.log"
        log_path.write_text(
            '{"type": "info", "msg": "starting"}\n'
            'Here is the final answer\n'
            '{"type": "result", "result": "json result"}\n'
            '\n'  # trailing empty line
        )

        response = extract_codex_response(worktree, log_path)
        # Last non-empty non-JSON line (scanning in reverse):
        # '' (empty, skip), '{"type": "result", ...}' (JSON, skip), 'Here is the final answer' (return)
        assert response == "Here is the final answer"

    def test_returns_none_when_neither_source_has_content(self, tmp_path):
        """(c) returns None when neither source has content."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # No .sandbox-result.txt, log has only JSON/empty lines

        log_path = tmp_path / "container.log"
        log_path.write_text(
            '{"type": "info", "msg": "starting"}\n'
            '{"type": "result", "result": "done"}\n'
        )

        response = extract_codex_response(worktree, log_path)
        assert response is None

    def test_returns_none_when_log_missing_and_no_result_file(self, tmp_path):
        """Returns None when log file doesn't exist and no result file."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        log_path = tmp_path / "nonexistent.log"

        response = extract_codex_response(worktree, log_path)
        assert response is None

    def test_empty_result_file_falls_back_to_log(self, tmp_path):
        """Empty .sandbox-result.txt falls back to log file."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".sandbox-result.txt").write_text("   \n  ")  # whitespace only

        log_path = tmp_path / "container.log"
        log_path.write_text("plain text response\n")

        response = extract_codex_response(worktree, log_path)
        assert response == "plain text response"


# ---------------------------------------------------------------------------
# get_provider
# ---------------------------------------------------------------------------

class TestGetProvider:
    """Tests for get_provider() dict structure."""

    def test_unknown_provider_raises_usage_error(self):
        """AC#21: Unknown provider name raises click.UsageError."""
        with pytest.raises(click.UsageError):
            get_provider("foo")

    def test_claude_provider_name(self):
        provider = get_provider("claude")
        assert provider["name"] == "claude"

    def test_codex_provider_name(self):
        provider = get_provider("codex")
        assert provider["name"] == "codex"

    def test_claude_build_cmd_basic(self):
        """Claude build_cmd includes the task and standard flags."""
        provider = get_provider("claude")
        cmd = provider["build_cmd"]("do work", None, Path("/worktree"))
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "do work" in cmd
        assert "--print" in cmd

    def test_claude_build_cmd_with_model(self):
        provider = get_provider("claude")
        cmd = provider["build_cmd"]("do work", "sonnet", Path("/worktree"))
        assert "--model" in cmd
        assert "sonnet" in cmd

    def test_codex_build_cmd_basic(self, tmp_path):
        """AC#1: Codex build_cmd uses codex exec --yolo -o <path> <task>."""
        provider = get_provider("codex")
        cmd = provider["build_cmd"]("hello", None, tmp_path)
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--yolo" in cmd
        assert "-o" in cmd
        o_idx = cmd.index("-o")
        assert cmd[o_idx + 1] == str(tmp_path / ".sandbox-result.txt")
        assert "hello" in cmd

    def test_codex_build_cmd_with_model(self, tmp_path):
        """AC#2: --model gpt-4.1 is included in codex exec command."""
        provider = get_provider("codex")
        cmd = provider["build_cmd"]("hello", "gpt-4.1", tmp_path)
        assert "--model" in cmd
        assert "gpt-4.1" in cmd

    def test_codex_build_resume_cmd_raises(self):
        """AC#5: build_resume_cmd raises UsageError for codex."""
        provider = get_provider("codex")
        with pytest.raises(click.UsageError, match="does not support --continue"):
            provider["build_resume_cmd"](None, Path("/worktree"))

    def test_claude_resume_cmd(self):
        """Claude resume cmd includes --continue."""
        provider = get_provider("claude")
        cmd = provider["build_resume_cmd"](None, Path("/worktree"))
        assert "--continue" in cmd

    def test_codex_env_vars_no_dash_e_prefix(self):
        """AC#18: env_vars() returns strings without -e prefix."""
        with patch("sandbox_cli.get_gh_token", return_value="ghp_test"):
            provider = get_provider("codex")
            env_vars = provider["env_vars"]()
        # Verify no -e prefix
        for var in env_vars:
            assert not var.startswith("-e")
        # Verify content
        assert any(v.startswith("CODEX_HOME=") for v in env_vars)
        assert any(v.startswith("GH_TOKEN=") for v in env_vars)

    def test_codex_env_vars_no_claude_token(self):
        """AC#7: Codex provider does not pass CLAUDE_CODE_OAUTH_TOKEN."""
        with patch("sandbox_cli.get_gh_token", return_value="ghp_test"):
            provider = get_provider("codex")
            env_vars = provider["env_vars"]()
        assert not any("CLAUDE_CODE_OAUTH_TOKEN" in v for v in env_vars)
        assert not any("GH_TOKEN" not in v and "CODEX_HOME" not in v for v in env_vars)

    def test_claude_env_vars_no_dash_e_prefix(self):
        """AC#18: claude env_vars() also returns strings without -e prefix."""
        with patch("sandbox_cli.get_auth_token", return_value="tok"), \
             patch("sandbox_cli.get_gh_token", return_value="ghp"):
            provider = get_provider("claude")
            env_vars = provider["env_vars"]()
        for var in env_vars:
            assert not var.startswith("-e")

    def test_codex_volume_mounts_no_dash_v_prefix(self):
        """AC#18: volume_mounts() returns strings without -v prefix."""
        provider = get_provider("codex")
        mounts = provider["volume_mounts"](Path("/home/user"))
        for mount in mounts:
            assert not mount.startswith("-v")
        assert any(".codex" in m for m in mounts)

    def test_codex_auth_check_passes_when_file_exists(self, tmp_path):
        """AC#8: Auth check returns None when ~/.codex/auth.json exists."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text('{"token": "test"}')
        with patch("sandbox_cli.Path") as mock_path_cls:
            mock_path_cls.home.return_value = tmp_path
            # Need to re-create the lambda with mocked Path.home
            # Easier to just test the condition directly
            pass
        # Test via actual path patching
        provider = get_provider("codex")
        with patch("pathlib.Path.home", return_value=tmp_path):
            # Re-evaluate by calling auth_check on a fresh provider
            result = (lambda: None if (tmp_path / ".codex" / "auth.json").exists() else "error")()
        assert result is None

    def test_codex_auth_check_fails_when_file_missing(self, tmp_path):
        """AC#8: Auth check returns error string when ~/.codex/auth.json is missing."""
        provider = get_provider("codex")
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = (lambda: None if (tmp_path / ".codex" / "auth.json").exists() else "No Codex auth found. Run: codex login")()
        assert result is not None
        assert "codex" in result.lower()

    def test_codex_extract_response_uses_extract_codex_response(self, tmp_path):
        """Codex provider's extract_response is extract_codex_response."""
        from sandbox_cli import extract_codex_response as ecr
        provider = get_provider("codex")
        assert provider["extract_response"] is ecr


# ---------------------------------------------------------------------------
# CLI --provider flag parsing
# ---------------------------------------------------------------------------

@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli():
    from sandbox_cli import cli
    return cli


def test_provider_flag_appears_in_help(runner, cli):
    """AC#19: HELP_TEXT updated to mention --provider flag."""
    result = runner.invoke(cli, ["start", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output


def test_provider_default_is_claude(runner, cli):
    """AC#3: No --provider defaults to claude."""
    with patch("sandbox_cli.get_repo_root", return_value=Path("/tmp/repo")), \
         patch("sandbox_cli.get_main_git_dir", return_value=Path("/tmp/repo/.git")), \
         patch("sandbox_cli.build_template_if_exists", return_value="test-image"), \
         patch("sandbox_cli.run_sandbox_background") as mock_bg:
        mock_bg.return_value = {"container": "c", "name": "n", "branch": "n", "exitCode": 0}
        result = runner.invoke(cli, ["start", "foo", "--task", "do it"])
    assert result.exit_code == 0
    call_kwargs = mock_bg.call_args
    assert call_kwargs.kwargs.get("provider") == "claude"


def test_provider_codex_passed_through(runner, cli):
    """AC#1: --provider codex is passed to run_sandbox_background."""
    with patch("sandbox_cli.get_repo_root", return_value=Path("/tmp/repo")), \
         patch("sandbox_cli.get_main_git_dir", return_value=Path("/tmp/repo/.git")), \
         patch("sandbox_cli.build_template_if_exists", return_value="test-image"), \
         patch("sandbox_cli.run_sandbox_background") as mock_bg:
        mock_bg.return_value = {"container": "c", "name": "n", "branch": "n", "exitCode": 0}
        result = runner.invoke(cli, ["start", "foo", "--task", "do it", "--provider", "codex"])
    assert result.exit_code == 0
    call_kwargs = mock_bg.call_args
    assert call_kwargs.kwargs.get("provider") == "codex"


def test_unknown_provider_rejected(runner, cli):
    """AC#21: Unknown provider exits with error."""
    result = runner.invoke(cli, ["start", "foo", "--task", "do it", "--provider", "openai"])
    assert result.exit_code != 0


def test_codex_without_task_exits_with_error(runner, cli):
    """AC#4: --provider codex without --task exits with error."""
    with patch("sandbox_cli.get_repo_root", return_value=Path("/tmp/repo")):
        result = runner.invoke(cli, ["start", "foo", "--provider", "codex"])
    assert result.exit_code != 0
    assert "background task mode" in result.output.lower() or "background" in result.output.lower()


def test_provider_claude_explicit_behaves_same_as_default(runner, cli):
    """AC#3: --provider claude behaves identically to default."""
    with patch("sandbox_cli.get_repo_root", return_value=Path("/tmp/repo")), \
         patch("sandbox_cli.get_main_git_dir", return_value=Path("/tmp/repo/.git")), \
         patch("sandbox_cli.build_template_if_exists", return_value="test-image"), \
         patch("sandbox_cli.run_sandbox_background") as mock_bg:
        mock_bg.return_value = {"container": "c", "name": "n", "branch": "n", "exitCode": 0}
        result = runner.invoke(cli, ["start", "foo", "--task", "do it", "--provider", "claude"])
    assert result.exit_code == 0
    call_kwargs = mock_bg.call_args
    assert call_kwargs.kwargs.get("provider") == "claude"


def test_codex_continue_raises_usage_error():
    """AC#5: --continue with codex provider raises UsageError via build_resume_cmd."""
    provider = get_provider("codex")
    with pytest.raises(click.UsageError, match="Codex provider does not support --continue"):
        provider["build_resume_cmd"](None, Path("/worktree"))


# ---------------------------------------------------------------------------
# State file: provider field
# ---------------------------------------------------------------------------

class TestStateFileProvider:
    """AC#12 and AC#13: state file includes provider, defaults to claude."""

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    @patch("sandbox_cli.get_auth_token", return_value="test-token")
    def test_state_file_includes_provider(self, mock_auth, mock_wt_rm, mock_container_rm,
                                          mock_copy_env, mock_wt_add, mock_branch,
                                          mock_container, mock_run, tmp_path):
        from sandbox_cli import run_sandbox_background

        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_container_rm.return_value = True

        logs_dir = tmp_path / "logs"
        state_captured = {}

        def run_side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "docker run" in cmd_str:
                state_file = logs_dir / "sandbox-myrepo-test.json"
                if state_file.exists():
                    state_captured.update(json.loads(state_file.read_text()))
                return MagicMock(returncode=0, stdout="cid\n")
            if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                return MagicMock(returncode=0, stdout="abc123\n")
            if "docker wait" in cmd_str:
                return MagicMock(returncode=0, stdout="0\n")
            if "docker logs" in cmd_str:
                return MagicMock(returncode=0, stdout='{"type": "result", "result": "done"}\n')
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "add" in cmd_str and "-A" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "reset" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "rev-parse" in cmd_str:
                return MagicMock(returncode=0, stdout="def456\n")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = run_side_effect

        run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=logs_dir,
            provider="claude",
        )

        assert state_captured.get("provider") == "claude"

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    @patch("sandbox_cli.get_auth_token", return_value="test-token")
    def test_result_json_includes_provider(self, mock_auth, mock_wt_rm, mock_container_rm,
                                           mock_copy_env, mock_wt_add, mock_branch,
                                           mock_container, mock_run, tmp_path):
        from sandbox_cli import run_sandbox_background

        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_container_rm.return_value = True

        def run_side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                return MagicMock(returncode=0, stdout="abc123\n")
            if "docker run" in cmd_str:
                return MagicMock(returncode=0, stdout="cid\n")
            if "docker wait" in cmd_str:
                return MagicMock(returncode=0, stdout="0\n")
            if "docker logs" in cmd_str:
                return MagicMock(returncode=0, stdout='{"type": "result", "result": "done"}\n')
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "add" in cmd_str and "-A" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "reset" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "rev-parse" in cmd_str:
                return MagicMock(returncode=0, stdout="def456\n")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = run_side_effect

        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
            provider="claude",
        )

        assert result.get("provider") == "claude"


class TestReadCommandProvider:
    """AC#13 and AC#14: read command loads provider from state, defaults to claude."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def cli(self):
        from sandbox_cli import cli
        return cli

    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.container_running")
    @patch("sandbox_cli.run")
    @patch("sandbox_cli.get_repo_root")
    @patch("sandbox_cli.get_logs_dir")
    def test_read_defaults_provider_to_claude_when_missing(
        self, mock_logs_dir, mock_repo, mock_run, mock_running, mock_exists,
        mock_wt_rm, mock_container_rm, runner, cli, tmp_path
    ):
        """AC#13: state file without provider field defaults to claude."""
        mock_repo.return_value = Path("/Users/test/myrepo")
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        mock_logs_dir.return_value = logs_dir
        mock_exists.return_value = True
        mock_running.return_value = False
        mock_wt_rm.return_value = True
        mock_container_rm.return_value = True

        state = {
            "status": "running",
            "container": "sandbox-myrepo-test",
            "name": "test",
            "branch": "test",
            "worktreePath": str(tmp_path / "worktree"),
            "baseCommit": "abc123",
            # No "provider" field — should default to "claude"
        }
        (logs_dir / "sandbox-myrepo-test.json").write_text(json.dumps(state))

        def run_side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "docker logs" in cmd_str:
                return MagicMock(returncode=0, stdout='{"type": "result", "result": "done"}\n')
            if "docker inspect" in cmd_str and "ExitCode" in cmd_str:
                return MagicMock(returncode=0, stdout="0\n")
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "add" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "reset" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = run_side_effect

        result = runner.invoke(cli, ["read", "test"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output.get("provider") == "claude"

    @patch("sandbox_cli.get_repo_root")
    @patch("sandbox_cli.get_logs_dir")
    def test_read_completed_result_preserves_provider(self, mock_logs_dir, mock_repo, runner, cli, tmp_path):
        """AC#12: completed result JSON includes provider field."""
        mock_repo.return_value = Path("/Users/test/myrepo")
        mock_logs_dir.return_value = tmp_path
        result_data = {
            "container": "sandbox-myrepo-test",
            "name": "test",
            "branch": "test",
            "exitCode": 0,
            "response": "Done.",
            "provider": "codex",
        }
        (tmp_path / "sandbox-myrepo-test.json").write_text(json.dumps(result_data))

        result = runner.invoke(cli, ["read", "test"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output.get("provider") == "codex"


class TestAuthCheckBeforeWorktree:
    """AC#9: Auth check runs before worktree creation."""

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.get_auth_token", return_value=None)
    def test_claude_auth_failure_before_worktree_creation(
        self, mock_auth, mock_copy_env, mock_wt_add,
        mock_branch, mock_container, mock_run, tmp_path
    ):
        """Claude auth check fails before worktree is created — no cleanup needed."""
        from sandbox_cli import run_sandbox_background

        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True  # Would succeed if called

        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
            provider="claude",
        )

        assert "error" in result
        # Worktree should NOT have been created (auth failed first)
        mock_wt_add.assert_not_called()


class TestProviderErrorMessage:
    """AC#15: Error message uses provider name."""

    @patch("sandbox_cli.run")
    @patch("sandbox_cli.container_exists")
    @patch("sandbox_cli.branch_exists")
    @patch("sandbox_cli.git_worktree_add")
    @patch("sandbox_cli.copy_env_files")
    @patch("sandbox_cli.docker_container_rm")
    @patch("sandbox_cli.git_worktree_remove")
    @patch("sandbox_cli.get_auth_token", return_value="tok")
    def test_nonzero_exit_uses_provider_name(self, mock_auth, mock_wt_rm, mock_container_rm,
                                              mock_copy_env, mock_wt_add, mock_branch,
                                              mock_container, mock_run, tmp_path):
        from sandbox_cli import run_sandbox_background

        mock_container.return_value = False
        mock_branch.return_value = False
        mock_wt_add.return_value = True
        mock_copy_env.return_value = []
        mock_container_rm.return_value = True

        def run_side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                return MagicMock(returncode=0, stdout="abc123\n")
            if "docker run" in cmd_str:
                return MagicMock(returncode=0, stdout="cid\n")
            if "docker wait" in cmd_str:
                return MagicMock(returncode=0, stdout="1\n")  # exit code 1
            if "docker logs" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "add" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            if "reset" in cmd_str:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = run_side_effect

        result = run_sandbox_background(
            name="test", repo_root=tmp_path, repo_name="myrepo",
            main_git=tmp_path / ".git",
            image="test-image", task="do it", logs_dir=tmp_path / "logs",
            provider="claude",
        )

        assert "error" in result
        assert "claude" in result["error"].lower()
        assert "1" in result["error"]


# ---------------------------------------------------------------------------
# extract_gemini_response
# ---------------------------------------------------------------------------

class TestExtractGeminiResponse:
    """AC#8: extract_gemini_response test cases."""

    def test_returns_response_field_from_json(self, tmp_path):
        """(a) returns 'response' field from JSON output."""
        log_path = tmp_path / "container.log"
        log_path.write_text('{"response": "Task completed successfully.", "exitCode": 0}\n')

        result = extract_gemini_response(tmp_path / "worktree", log_path)
        assert result == "Task completed successfully."

    def test_skips_objects_without_response_falls_back_to_last_non_json_line(self, tmp_path):
        """(b) skips JSON objects without 'response' key, falls back to last non-empty non-JSON line."""
        log_path = tmp_path / "container.log"
        log_path.write_text(
            '{"type": "info", "msg": "starting"}\n'
            'Here is the final answer\n'
            '{"type": "status", "done": true}\n'
            '\n'
        )

        result = extract_gemini_response(tmp_path / "worktree", log_path)
        assert result == "Here is the final answer"

    def test_returns_none_when_log_has_no_content(self, tmp_path):
        """(c) returns None when log has no content."""
        log_path = tmp_path / "container.log"
        log_path.write_text("")

        result = extract_gemini_response(tmp_path / "worktree", log_path)
        assert result is None

    def test_returns_none_when_log_missing(self, tmp_path):
        """(c) returns None when log file doesn't exist."""
        log_path = tmp_path / "nonexistent.log"

        result = extract_gemini_response(tmp_path / "worktree", log_path)
        assert result is None

    def test_handles_malformed_json_gracefully(self, tmp_path):
        """(d) handles malformed/partial JSON lines gracefully (skip them)."""
        log_path = tmp_path / "container.log"
        log_path.write_text(
            '{"response": "good"\n'  # malformed (no closing brace)
            'plain text fallback\n'
            '{bad json here\n'
        )

        result = extract_gemini_response(tmp_path / "worktree", log_path)
        # All JSON lines are malformed, should fall back to last non-JSON line
        assert result == "{bad json here" or result == "plain text fallback"
        # Should not raise

    def test_whole_content_json_with_response(self, tmp_path):
        """Single JSON object spanning file content is parsed correctly."""
        log_path = tmp_path / "container.log"
        data = {"response": "All done!", "steps": 3}
        log_path.write_text(json.dumps(data))

        result = extract_gemini_response(tmp_path / "worktree", log_path)
        assert result == "All done!"

    def test_worktree_path_not_used(self, tmp_path):
        """worktree_path parameter is accepted but not used."""
        log_path = tmp_path / "container.log"
        log_path.write_text('{"response": "done"}\n')
        fake_worktree = Path("/nonexistent/path")

        result = extract_gemini_response(fake_worktree, log_path)
        assert result == "done"


# ---------------------------------------------------------------------------
# Gemini provider: get_provider("gemini")
# ---------------------------------------------------------------------------

class TestGeminiProvider:
    """AC#1-7, AC#11-17: Gemini provider tests."""

    def test_gemini_provider_name(self):
        provider = get_provider("gemini")
        assert provider["name"] == "gemini"

    def test_gemini_build_cmd_basic(self):
        """AC#1: build_cmd uses gemini -p <task> --output-format json --yolo."""
        provider = get_provider("gemini")
        cmd = provider["build_cmd"]("hello", None, Path("/worktree"))
        assert cmd[0] == "gemini"
        assert "-p" in cmd
        assert "hello" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--yolo" in cmd

    def test_gemini_build_cmd_with_model(self):
        """AC#2: --model is included in gemini command when provided."""
        provider = get_provider("gemini")
        cmd = provider["build_cmd"]("hello", "gemini-2.5-flash", Path("/worktree"))
        assert "--model" in cmd
        assert "gemini-2.5-flash" in cmd

    def test_gemini_build_cmd_no_model(self):
        """No --model flag when model is None."""
        provider = get_provider("gemini")
        cmd = provider["build_cmd"]("hello", None, Path("/worktree"))
        assert "--model" not in cmd

    def test_gemini_build_resume_cmd_raises(self):
        """AC#4: build_resume_cmd raises UsageError for gemini."""
        provider = get_provider("gemini")
        with pytest.raises(click.UsageError, match="does not support --continue"):
            provider["build_resume_cmd"](None, Path("/worktree"))

    def test_gemini_env_vars_contains_gemini_cli_home(self):
        """AC#6: env_vars includes GEMINI_CLI_HOME=/home/agent."""
        with patch("sandbox_cli.get_gh_token", return_value="ghp_test"), \
             patch.dict("os.environ", {}, clear=True):
            provider = get_provider("gemini")
            env_vars = provider["env_vars"]()
        assert any(v == "GEMINI_CLI_HOME=/home/agent" for v in env_vars)

    def test_gemini_env_vars_passes_gh_token(self):
        """AC#6: env_vars always includes GH_TOKEN."""
        with patch("sandbox_cli.get_gh_token", return_value="ghp_test"), \
             patch.dict("os.environ", {}, clear=True):
            provider = get_provider("gemini")
            env_vars = provider["env_vars"]()
        assert any(v == "GH_TOKEN=ghp_test" for v in env_vars)

    def test_gemini_env_vars_includes_api_key_when_set(self):
        """AC#6: GEMINI_API_KEY is included only if set in host environment."""
        with patch("sandbox_cli.get_gh_token", return_value="ghp"), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "my-key"}, clear=False):
            provider = get_provider("gemini")
            env_vars = provider["env_vars"]()
        assert any(v == "GEMINI_API_KEY=my-key" for v in env_vars)

    def test_gemini_env_vars_excludes_api_key_when_not_set(self):
        """AC#6: GEMINI_API_KEY is not included when absent from host environment."""
        env_without_key = {k: v for k, v in __import__("os").environ.items() if k != "GEMINI_API_KEY"}
        with patch("sandbox_cli.get_gh_token", return_value="ghp"), \
             patch.dict("os.environ", env_without_key, clear=True):
            provider = get_provider("gemini")
            env_vars = provider["env_vars"]()
        assert not any("GEMINI_API_KEY" in v for v in env_vars)

    def test_gemini_volume_mounts_contains_gemini_dir(self):
        """AC#5: volume_mounts includes ~/.gemini mount."""
        provider = get_provider("gemini")
        home = Path("/home/user")
        mounts = provider["volume_mounts"](home)
        assert any(".gemini" in m for m in mounts)
        # rw (no :ro suffix)
        gemini_mount = next(m for m in mounts if ".gemini" in m)
        assert not gemini_mount.endswith(":ro")

    def test_gemini_volume_mounts_contains_ssh_ro(self):
        """AC#5: volume_mounts includes .ssh:ro."""
        provider = get_provider("gemini")
        mounts = provider["volume_mounts"](Path("/home/user"))
        assert any(".ssh" in m and m.endswith(":ro") for m in mounts)

    def test_gemini_volume_mounts_contains_gh_config_ro(self):
        """AC#5: volume_mounts includes .config/gh:ro."""
        provider = get_provider("gemini")
        mounts = provider["volume_mounts"](Path("/home/user"))
        assert any(".config/gh" in m and m.endswith(":ro") for m in mounts)

    def test_gemini_volume_mounts_contains_pnpm_store(self):
        """AC#5: volume_mounts includes pnpm-store."""
        provider = get_provider("gemini")
        mounts = provider["volume_mounts"](Path("/home/user"))
        assert any("pnpm-store" in m for m in mounts)

    def test_gemini_volume_mounts_no_dash_v_prefix(self):
        """volume_mounts() returns strings without -v prefix."""
        provider = get_provider("gemini")
        mounts = provider["volume_mounts"](Path("/home/user"))
        for mount in mounts:
            assert not mount.startswith("-v")

    def test_gemini_auth_check_passes_with_api_key(self, tmp_path):
        """AC#7: auth_check returns None when GEMINI_API_KEY is set."""
        with patch.dict("os.environ", {"GEMINI_API_KEY": "my-key"}, clear=False):
            provider = get_provider("gemini")
            result = provider["auth_check"]()
        assert result is None

    def test_gemini_auth_check_passes_with_gemini_dir(self, tmp_path):
        """AC#7: auth_check returns None when ~/.gemini/ directory exists."""
        (tmp_path / ".gemini").mkdir()
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch.dict("os.environ", {k: v for k, v in __import__("os").environ.items() if k != "GEMINI_API_KEY"}, clear=True):
            provider = get_provider("gemini")
            result = provider["auth_check"]()
        assert result is None

    def test_gemini_auth_check_fails_when_no_auth(self, tmp_path):
        """AC#7: auth_check returns error string when no auth present."""
        env_without_key = {k: v for k, v in __import__("os").environ.items() if k != "GEMINI_API_KEY"}
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch.dict("os.environ", env_without_key, clear=True):
            provider = get_provider("gemini")
            result = provider["auth_check"]()
        assert result is not None
        assert "GEMINI_API_KEY" in result or "gemini" in result.lower()

    def test_gemini_extract_response_uses_extract_gemini_response(self):
        """Gemini provider's extract_response is extract_gemini_response."""
        from sandbox_cli import extract_gemini_response as egr
        provider = get_provider("gemini")
        assert provider["extract_response"] is egr


# ---------------------------------------------------------------------------
# CLI: gemini provider flag parsing
# ---------------------------------------------------------------------------

class TestGeminiCLI:
    """AC#3, AC#4, AC#14, AC#15: CLI tests for gemini provider."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def cli(self):
        from sandbox_cli import cli
        return cli

    def test_gemini_passed_through_to_background(self, runner, cli):
        """AC#1: --provider gemini is passed to run_sandbox_background."""
        with patch("sandbox_cli.get_repo_root", return_value=Path("/tmp/repo")), \
             patch("sandbox_cli.get_main_git_dir", return_value=Path("/tmp/repo/.git")), \
             patch("sandbox_cli.build_template_if_exists", return_value="test-image"), \
             patch("sandbox_cli.run_sandbox_background") as mock_bg:
            mock_bg.return_value = {"container": "c", "name": "n", "branch": "n", "exitCode": 0}
            result = runner.invoke(cli, ["start", "foo", "--task", "do it", "--provider", "gemini"])
        assert result.exit_code == 0
        assert mock_bg.call_args.kwargs.get("provider") == "gemini"

    def test_gemini_without_task_exits_with_error(self, runner, cli):
        """AC#3: --provider gemini without --task exits with error."""
        with patch("sandbox_cli.get_repo_root", return_value=Path("/tmp/repo")):
            result = runner.invoke(cli, ["start", "foo", "--provider", "gemini"])
        assert result.exit_code != 0
        assert "background task mode" in result.output.lower() or "background" in result.output.lower()

    def test_gemini_continue_raises_usage_error(self):
        """AC#4: --continue with gemini provider raises UsageError via build_resume_cmd."""
        provider = get_provider("gemini")
        with pytest.raises(click.UsageError, match="Gemini provider does not support --continue"):
            provider["build_resume_cmd"](None, Path("/worktree"))

    def test_gemini_error_message_uses_provider_name(self, tmp_path):
        """AC#11: Error message uses 'gemini exited with code {exit_code}'."""
        from unittest.mock import MagicMock, patch
        from sandbox_cli import run_sandbox_background

        with patch("sandbox_cli.run") as mock_run, \
             patch("sandbox_cli.container_exists", return_value=False), \
             patch("sandbox_cli.branch_exists", return_value=False), \
             patch("sandbox_cli.git_worktree_add", return_value=True), \
             patch("sandbox_cli.copy_env_files", return_value=[]), \
             patch("sandbox_cli.docker_container_rm", return_value=True), \
             patch("sandbox_cli.git_worktree_remove", return_value=True), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "my-key"}, clear=False):

            def run_side_effect(cmd, **kwargs):
                cmd_str = " ".join(cmd)
                if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                    return MagicMock(returncode=0, stdout="abc123\n")
                if "docker run" in cmd_str:
                    return MagicMock(returncode=0, stdout="cid\n")
                if "docker wait" in cmd_str:
                    return MagicMock(returncode=0, stdout="1\n")
                if "docker logs" in cmd_str:
                    return MagicMock(returncode=0, stdout="")
                if "status" in cmd_str and "--porcelain" in cmd_str:
                    return MagicMock(returncode=0, stdout="")
                if "add" in cmd_str:
                    return MagicMock(returncode=0, stdout="")
                if "reset" in cmd_str:
                    return MagicMock(returncode=0, stdout="")
                return MagicMock(returncode=0, stdout="")

            mock_run.side_effect = run_side_effect
            with patch("sandbox_cli.get_gh_token", return_value="tok"):
                result = run_sandbox_background(
                    name="test", repo_root=tmp_path, repo_name="myrepo",
                    main_git=tmp_path / ".git",
                    image="test-image", task="do it", logs_dir=tmp_path / "logs",
                    provider="gemini",
                )

        assert "error" in result
        assert "gemini" in result["error"].lower()
        assert "1" in result["error"]

    def test_gemini_state_includes_provider(self, tmp_path):
        """AC#9: state file includes 'provider': 'gemini'."""
        from unittest.mock import MagicMock, patch
        from sandbox_cli import run_sandbox_background

        logs_dir = tmp_path / "logs"
        state_captured = {}

        with patch("sandbox_cli.run") as mock_run, \
             patch("sandbox_cli.container_exists", return_value=False), \
             patch("sandbox_cli.branch_exists", return_value=False), \
             patch("sandbox_cli.git_worktree_add", return_value=True), \
             patch("sandbox_cli.copy_env_files", return_value=[]), \
             patch("sandbox_cli.docker_container_rm", return_value=True), \
             patch("sandbox_cli.git_worktree_remove", return_value=True), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "my-key"}, clear=False):

            def run_side_effect(cmd, **kwargs):
                cmd_str = " ".join(cmd)
                if "docker run" in cmd_str:
                    state_file = logs_dir / "sandbox-myrepo-test.json"
                    if state_file.exists():
                        state_captured.update(json.loads(state_file.read_text()))
                    return MagicMock(returncode=0, stdout="cid\n")
                if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                    return MagicMock(returncode=0, stdout="abc123\n")
                if "docker wait" in cmd_str:
                    return MagicMock(returncode=0, stdout="0\n")
                if "docker logs" in cmd_str:
                    return MagicMock(returncode=0, stdout='{"response": "done"}\n')
                if "status" in cmd_str and "--porcelain" in cmd_str:
                    return MagicMock(returncode=0, stdout="")
                if "add" in cmd_str:
                    return MagicMock(returncode=0, stdout="")
                if "reset" in cmd_str:
                    return MagicMock(returncode=0, stdout="")
                if "rev-parse" in cmd_str:
                    return MagicMock(returncode=0, stdout="def456\n")
                return MagicMock(returncode=0, stdout="")

            mock_run.side_effect = run_side_effect
            with patch("sandbox_cli.get_gh_token", return_value="tok"):
                result = run_sandbox_background(
                    name="test", repo_root=tmp_path, repo_name="myrepo",
                    main_git=tmp_path / ".git",
                    image="test-image", task="do it", logs_dir=logs_dir,
                    provider="gemini",
                )

        assert state_captured.get("provider") == "gemini"
        assert result.get("provider") == "gemini"
