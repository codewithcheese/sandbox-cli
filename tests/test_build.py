"""Tests for image build functions."""

from pathlib import Path
from unittest.mock import MagicMock, patch, call


from sandbox_cli import ensure_default_image, build_template_if_exists


@patch("sandbox_cli.build_lock")
@patch("sandbox_cli.run")
@patch("sandbox_cli.get_sandbox_cli_dir")
def test_ensure_default_image_acquires_lock(mock_cli_dir, mock_run, mock_lock, tmp_path):
    mock_cli_dir.return_value = tmp_path
    (tmp_path / "Dockerfile").write_text("FROM node:20")
    # Image doesn't exist yet
    mock_run.return_value = MagicMock(returncode=1)
    mock_lock.return_value.__enter__ = MagicMock(return_value=None)
    mock_lock.return_value.__exit__ = MagicMock(return_value=False)

    # Build succeeds on second call
    mock_run.side_effect = [
        MagicMock(returncode=1),  # image inspect fails
        MagicMock(returncode=0),  # docker build succeeds
    ]

    ensure_default_image()
    mock_lock.assert_called_once()


@patch("sandbox_cli.build_lock")
@patch("sandbox_cli.run")
def test_build_template_acquires_lock(mock_run, mock_lock, tmp_path):
    (tmp_path / "Dockerfile.sandbox").write_text("FROM node:20")
    mock_lock.return_value.__enter__ = MagicMock(return_value=None)
    mock_lock.return_value.__exit__ = MagicMock(return_value=False)
    mock_run.return_value = MagicMock(returncode=0)

    build_template_if_exists(tmp_path)
    # Called twice: once for ensure_default_image(), once for custom build
    assert mock_lock.call_count == 2


@patch("sandbox_cli._build_lock_path")
@patch("sandbox_cli.run")
@patch("sandbox_cli.get_sandbox_cli_dir")
def test_ensure_default_image_stderr_only(mock_cli_dir, mock_run, mock_lock_path, tmp_path, capsys):
    """Build messages should go to stderr, not stdout."""
    mock_cli_dir.return_value = tmp_path
    mock_lock_path.return_value = tmp_path / "build.lock"
    (tmp_path / "Dockerfile").write_text("FROM node:20")
    mock_run.side_effect = [
        MagicMock(returncode=1),  # image inspect fails
        MagicMock(returncode=0),  # docker build succeeds
    ]

    ensure_default_image()
    captured = capsys.readouterr()
    assert "Building" not in captured.out  # should be on stderr, not stdout
