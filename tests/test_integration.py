"""Integration tests for sandbox CLI (require Docker)."""

import subprocess
from pathlib import Path

import pytest

# Skip all tests if Docker is not available
pytestmark = pytest.mark.skipif(
    subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
    reason="Docker not available"
)

IMAGE_NAME = "sandbox-cli:test"
DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"


@pytest.fixture(scope="module")
def built_image():
    """Build the default image once for all tests."""
    result = subprocess.run(
        ["docker", "build", "-t", IMAGE_NAME, "-f", str(DOCKERFILE), str(DOCKERFILE.parent)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Failed to build image: {result.stderr}"
    yield IMAGE_NAME
    # Cleanup
    subprocess.run(["docker", "rmi", IMAGE_NAME], capture_output=True)


def run_in_container(image: str, cmd: str) -> subprocess.CompletedProcess:
    """Run a command in the container and return result."""
    return subprocess.run(
        ["docker", "run", "--rm", image, "bash", "-c", cmd],
        capture_output=True,
        text=True,
    )


class TestDefaultImage:
    """Test the default sandbox image has all required tools."""

    def test_node_installed(self, built_image):
        result = run_in_container(built_image, "node --version")
        assert result.returncode == 0
        assert result.stdout.startswith("v20")

    def test_pnpm_installed(self, built_image):
        result = run_in_container(built_image, "pnpm --version")
        assert result.returncode == 0

    def test_python_installed(self, built_image):
        result = run_in_container(built_image, "python3 --version")
        assert result.returncode == 0
        assert "Python 3" in result.stdout

    def test_pip_installed(self, built_image):
        result = run_in_container(built_image, "pip3 --version")
        assert result.returncode == 0

    def test_git_installed(self, built_image):
        result = run_in_container(built_image, "git --version")
        assert result.returncode == 0

    def test_gh_installed(self, built_image):
        result = run_in_container(built_image, "gh --version")
        assert result.returncode == 0

    def test_claude_installed(self, built_image):
        result = run_in_container(built_image, "claude --version")
        assert result.returncode == 0

    def test_playwright_installed(self, built_image):
        result = run_in_container(built_image, "npx playwright --version")
        assert result.returncode == 0

    def test_ss_installed(self, built_image):
        """ss command needed for port detection."""
        result = run_in_container(built_image, "ss --version")
        assert result.returncode == 0

    def test_runs_as_agent_user(self, built_image):
        result = run_in_container(built_image, "whoami")
        assert result.returncode == 0
        assert result.stdout.strip() == "agent"

    def test_pnpm_store_dir_set(self, built_image):
        result = run_in_container(built_image, "echo $PNPM_STORE_DIR")
        assert result.returncode == 0
        assert result.stdout.strip() == "/pnpm-store"

    def test_pnpm_store_writable(self, built_image):
        result = run_in_container(built_image, "touch /pnpm-store/test && rm /pnpm-store/test")
        assert result.returncode == 0
