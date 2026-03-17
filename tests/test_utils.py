"""Tests for utility functions."""

import fcntl
from pathlib import Path
from unittest.mock import MagicMock, patch


from sandbox_cli import (
    safe_name,
    get_worktree_path,
    get_repo_root,
    get_main_git_dir,
    docker_container_ls,
    git_worktree_list,
    extract_response,
    parse_diff_stats,
    build_lock,
    get_logs_dir,
)


class TestSafeName:
    def test_simple_name(self):
        assert safe_name("feature") == "feature"

    def test_with_slashes(self):
        assert safe_name("feature/auth") == "feature-auth"

    def test_multiple_slashes(self):
        assert safe_name("claude/integrate/api") == "claude-integrate-api"

    def test_no_slashes(self):
        assert safe_name("fix-bug-123") == "fix-bug-123"


class TestGetWorktreePath:
    def test_basic_path(self):
        repo_root = Path("/Users/test/projects/myrepo")
        result = get_worktree_path(repo_root, "feature-auth")
        expected = Path.home() / ".config" / "sandbox-cli" / "worktrees" / "myrepo__feature-auth"
        assert result == expected

    def test_nested_repo(self):
        repo_root = Path("/Users/test/deep/nested/repo")
        result = get_worktree_path(repo_root, "task")
        expected = Path.home() / ".config" / "sandbox-cli" / "worktrees" / "repo__task"
        assert result == expected


class TestGetRepoRoot:
    @patch("sandbox_cli.run")
    def test_in_repo(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="/Users/test/myrepo\n"),  # --show-toplevel
            MagicMock(returncode=0, stdout=".git\n"),                 # --git-common-dir
        ]
        assert get_repo_root() == Path("/Users/test/myrepo")

    @patch("sandbox_cli.run")
    def test_not_in_repo(self, mock_run):
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        assert get_repo_root() is None

    @patch("sandbox_cli.run")
    def test_in_worktree_returns_main_repo(self, mock_run):
        """When inside a worktree like myrepo__feature, should return main repo root."""
        from sandbox_cli import get_repo_root
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="/Users/test/myrepo__feature\n"),  # rev-parse --show-toplevel
            MagicMock(returncode=0, stdout="/Users/test/myrepo/.git\n"),       # rev-parse --git-common-dir
        ]
        result = get_repo_root()
        assert result == Path("/Users/test/myrepo")


class TestGetMainGitDir:
    @patch("sandbox_cli.run")
    def test_in_main_repo(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=".git\n")
        assert get_main_git_dir(Path("/Users/test/myrepo")) == Path("/Users/test/myrepo/.git")

    @patch("sandbox_cli.run")
    def test_in_worktree(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="/Users/test/myrepo/.git\n")
        assert get_main_git_dir(Path("/Users/test/myrepo__feature")) == Path("/Users/test/myrepo/.git")


class TestDockerContainerLs:
    @patch("sandbox_cli.run")
    def test_parses_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123\tsandbox-test\tUp 2 hours\ndef456\tsandbox-other\tExited (0) 1 hour ago"
        )
        result = docker_container_ls()
        assert len(result) == 2
        assert result[0]["name"] == "sandbox-test"
        assert result[0]["status"] == "Up 2 hours"

    @patch("sandbox_cli.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert docker_container_ls() == []

    @patch("sandbox_cli.run")
    def test_command_fails(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert docker_container_ls() == []


class TestGitWorktreeList:
    @patch("sandbox_cli.run")
    def test_parses_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="worktree /Users/test/repo\nHEAD abc123\nbranch refs/heads/main\n\nworktree /Users/test/repo__feature\nHEAD def456\nbranch refs/heads/feature/auth"
        )
        result = git_worktree_list()
        assert len(result) == 2
        assert result[0]["path"] == "/Users/test/repo"
        assert result[1]["path"] == "/Users/test/repo__feature"

    @patch("sandbox_cli.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert git_worktree_list() == []


class TestExtractSummary:
    def test_extracts_result_from_stream_json(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text(
            '{"type": "system", "subtype": "init", "session_id": "abc"}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it..."}]}}\n'
            '{"type": "result", "subtype": "success", "result": "Built prototype with 24 passing tests.", "duration_ms": 5000}\n'
        )
        assert extract_response(log) == "Built prototype with 24 passing tests."

    def test_multiple_results_returns_last(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text(
            '{"type": "result", "result": "First result"}\n'
            '{"type": "result", "result": "Final result"}\n'
        )
        assert extract_response(log) == "Final result"

    def test_skips_non_json_lines(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text(
            'WARNING: some stderr noise\n'
            '{"type": "result", "result": "Got it done."}\n'
            'another non-json line\n'
        )
        assert extract_response(log) == "Got it done."

    def test_falls_back_to_assistant_message(self, tmp_path):
        """When no result object, extracts text from assistant message.content."""
        log = tmp_path / "test.log"
        log.write_text(
            '{"type": "system", "subtype": "init"}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Here is the answer."}]}}\n'
        )
        assert extract_response(log) == "Here is the answer."

    def test_returns_none_when_no_response(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text('{"type": "system", "subtype": "init"}\n')
        assert extract_response(log) is None

    def test_returns_none_for_empty_file(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text('')
        assert extract_response(log) is None

    def test_skips_non_text_content_blocks(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text(
            '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}, {"type": "text", "text": "Done."}]}}\n'
        )
        assert extract_response(log) == "Done."


class TestParseDiffStats:
    def test_parses_numstat_output(self):
        assert parse_diff_stats("10\t2\tsrc/main.py\n5\t0\tsrc/utils.py\n") == {"filesChanged": 2, "insertions": 15, "deletions": 2}

    def test_empty_diff(self):
        assert parse_diff_stats("") == {"filesChanged": 0, "insertions": 0, "deletions": 0}

    def test_single_file(self):
        assert parse_diff_stats("482\t0\tnew_file.py\n") == {"filesChanged": 1, "insertions": 482, "deletions": 0}

    def test_binary_files_skipped(self):
        assert parse_diff_stats("10\t2\tsrc/main.py\n-\t-\timage.png\n") == {"filesChanged": 1, "insertions": 10, "deletions": 2}


class TestBuildLock:
    def test_creates_lock_file(self, tmp_path):
        with build_lock(tmp_path / "build.lock"):
            assert (tmp_path / "build.lock").exists()

    def test_lock_is_exclusive(self, tmp_path):
        lock_file = tmp_path / "build.lock"
        with build_lock(lock_file):
            f = open(lock_file, "w")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
            finally:
                f.close()
            assert not acquired

    def test_lock_released_after_context(self, tmp_path):
        lock_file = tmp_path / "build.lock"
        with build_lock(lock_file):
            pass
        f = open(lock_file, "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        finally:
            f.close()
        assert acquired

    def test_creates_parent_directory(self, tmp_path):
        lock_file = tmp_path / "nested" / "dir" / "build.lock"
        with build_lock(lock_file):
            assert lock_file.exists()


class TestGetLogsDir:
    def test_returns_config_path(self):
        assert get_logs_dir() == Path.home() / ".config" / "sandbox-cli" / "logs"


class TestResolveSandbox:
    def test_basic_resolution(self):
        from sandbox_cli import resolve_sandbox
        repo_root = Path("/Users/test/myrepo")
        s = resolve_sandbox(repo_root, "proto-1")
        assert s["sname"] == "proto-1"
        assert s["container"] == "sandbox-myrepo-proto-1"
        assert s["worktree"] == Path.home() / ".config" / "sandbox-cli" / "worktrees" / "myrepo__proto-1"
        assert s["branch"] == "proto-1"
        assert s["log_json"].name == "sandbox-myrepo-proto-1.json"
        assert s["log_raw"].name == "sandbox-myrepo-proto-1.log"

    def test_slash_in_name(self):
        from sandbox_cli import resolve_sandbox
        repo_root = Path("/Users/test/myrepo")
        s = resolve_sandbox(repo_root, "feature/auth")
        assert s["sname"] == "feature-auth"
        assert s["container"] == "sandbox-myrepo-feature-auth"
        assert s["branch"] == "feature/auth"
