"""Modal remote runtime smoke tests — require Modal auth and CLAUDE_CODE_OAUTH_TOKEN.

Run with: pytest tests/test_modal_integration.py -v -s

First run will be slow (Modal builds and caches the image). Subsequent runs are fast.
"""

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------

def _modal_available():
    try:
        import modal  # noqa: F401
        return True
    except ImportError:
        return False


def _modal_authenticated():
    """Check Modal auth by attempting an App lookup (makes a real API call)."""
    if not _modal_available():
        return False
    try:
        import modal
        modal.App.lookup("sandbox-cli-smoke-test", create_if_missing=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _modal_available() or not _modal_authenticated(),
    reason="Modal not available or not authenticated (run: modal token set)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_claude_token():
    """Read CLAUDE_CODE_OAUTH_TOKEN from env var or sandbox-cli auth file."""
    import os
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        return token
    token_path = Path.home() / ".config" / "sandbox-cli" / "auth_token"
    if token_path.exists():
        token = token_path.read_text().strip()
        if token:
            return token
    return None


def _make_image():
    """Build the Modal image as defined in the spec (minimal — no codex/gemini for smoke test)."""
    import modal
    return (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install("ca-certificates", "curl", "git", "jq")
        .env({"PATH": "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})
        .run_commands("curl -fsSL https://claude.ai/install.sh | bash")
    )


def _exec(sb, cmd: str):
    """Run a bash command in sandbox synchronously, wait for it, return the process."""
    proc = sb.exec("bash", "-c", cmd)
    proc.wait()
    return proc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def modal_app():
    import modal
    return modal.App.lookup("sandbox-cli-smoke-test", create_if_missing=True)


@pytest.fixture(scope="module")
def image():
    return _make_image()


@pytest.fixture(scope="class")
def sandbox(modal_app, image):
    """Shared sandbox for image verification tests (no secrets)."""
    import modal
    sb = modal.Sandbox.create(app=modal_app, image=image, timeout=300, cpu=2.0, memory=4096)
    yield sb
    try:
        sb.terminate()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests: Image contents
# ---------------------------------------------------------------------------

class TestModalImage:
    """Verify the Modal image has all tools required by the spec."""

    def test_git_installed(self, sandbox):
        proc = _exec(sandbox, "git --version")
        assert proc.returncode == 0

    def test_jq_installed(self, sandbox):
        proc = _exec(sandbox, "jq --version")
        assert proc.returncode == 0

    def test_claude_installed(self, sandbox):
        proc = _exec(sandbox, "claude --version")
        assert proc.returncode == 0

    def test_claude_on_path(self, sandbox):
        proc = _exec(sandbox, "which claude")
        assert proc.returncode == 0
        out = proc.stdout.read().strip()
        assert "/claude" in out


# ---------------------------------------------------------------------------
# Tests: End-to-end Claude execution
# ---------------------------------------------------------------------------

class TestModalSmoke:
    """Claude runs in a Modal sandbox and produces valid stream-json output."""

    def test_claude_stream_json(self, modal_app, image):
        """Claude -p with stream-json returns parseable NDJSON lines."""
        import modal

        token = _get_claude_token()
        if not token:
            pytest.skip("No Claude auth token at ~/.config/sandbox-cli/auth_token")

        sb = modal.Sandbox.create(
            app=modal_app,
            image=image,
            timeout=120,
            cpu=2.0,
            memory=4096,
            secrets=[modal.Secret.from_dict({"CLAUDE_CODE_OAUTH_TOKEN": token})],
        )
        try:
            proc = sb.exec(
                "bash", "-c",
                'echo "" | claude -p "Say hello. Keep it very brief." --output-format stream-json --verbose',
            )
            output = proc.stdout.read()
            proc.wait()

            assert proc.returncode == 0, f"exit {proc.returncode}\noutput:\n{output}"

            json_lines = [ln for ln in output.splitlines() if ln.strip().startswith("{")]
            assert json_lines, f"No JSON lines in output:\n{output}"

            # Every JSON line must be valid and have a 'type' field
            for ln in json_lines:
                data = json.loads(ln)
                assert "type" in data, f"Missing 'type' in: {ln}"

        finally:
            sb.terminate()

    def test_sandbox_result_marker(self, modal_app, image):
        """Runner script pattern: __SANDBOX_RESULT__ marker is printed after agent exits."""
        import modal

        token = _get_claude_token()
        if not token:
            pytest.skip("No Claude auth token at ~/.config/sandbox-cli/auth_token")

        # Minimal inline runner: run claude, then print the result marker
        runner_cmd = (
            'echo "" | claude -p "Print the word DONE and nothing else." '
            "--output-format stream-json --verbose > /tmp/agent.log 2>&1; "
            "EXIT=$?; "
            'echo "__SANDBOX_RESULT__{\\"exitCode\\": $EXIT}"'
        )

        sb = modal.Sandbox.create(
            app=modal_app,
            image=image,
            timeout=120,
            cpu=2.0,
            memory=4096,
            secrets=[modal.Secret.from_dict({"CLAUDE_CODE_OAUTH_TOKEN": token})],
        )
        try:
            proc = sb.exec("bash", "-c", runner_cmd)
            output = proc.stdout.read()
            proc.wait()

            marker_lines = [ln for ln in output.splitlines() if "__SANDBOX_RESULT__" in ln]
            assert marker_lines, f"No __SANDBOX_RESULT__ line in output:\n{output}"

            payload = marker_lines[-1].split("__SANDBOX_RESULT__", 1)[1]
            data = json.loads(payload)
            assert "exitCode" in data
            assert data["exitCode"] == 0, f"Agent exited non-zero: {data}"

        finally:
            sb.terminate()
