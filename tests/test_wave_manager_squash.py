"""Tests for the wave manager squash-merge CLI helper."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(tmp_path):
    """Set required env vars and configure workspace root for squash_merge tests."""
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
    }
    with patch.dict(os.environ, env):
        from agent_gtd_dispatch import config

        config.load()
        yield


# ---------------------------------------------------------------------------
# Helper: base sys.argv for a valid invocation
# ---------------------------------------------------------------------------

_BASE_ARGV = [
    "squash_merge.py",
    "--origin",
    "git@github.com:org/repo.git",
    "--branch",
    "feat/abc12345-my-task",
    "--item-id",
    "abc12345-6789-abcd-efgh",
    "--wave-run-id",
    "wr-456789ab-cdef-0123",
    "--decision-rule",
    "safe-docs",
]

_SUCCESS_MOCK = MagicMock(returncode=0, stderr="", stdout="")


def _fail_mock(stderr: str = "error") -> MagicMock:
    return MagicMock(returncode=1, stderr=stderr, stdout="")


class TestSquashMergeMain:
    @patch("sys.argv", _BASE_ARGV)
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.shutil.rmtree")
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.subprocess.run")
    def test_happy_path_exits_zero(self, mock_run, mock_rmtree) -> None:
        """All subprocess calls succeed → main() returns normally, workspace cleaned."""
        from agent_gtd_dispatch.wave_manager import squash_merge

        mock_run.return_value = _SUCCESS_MOCK

        # Should not raise
        squash_merge.main()

        # Workspace cleanup called exactly once
        mock_rmtree.assert_called_once()
        rmtree_path = mock_rmtree.call_args[0][0]
        assert "wave-merge-" in str(rmtree_path)

    @patch("sys.argv", _BASE_ARGV)
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.shutil.rmtree")
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.subprocess.run")
    def test_ci_gate_failure_exits_one(self, mock_run, mock_rmtree) -> None:
        """ci_gate.run() raises → SystemExit(1), workspace cleaned up."""
        from agent_gtd_dispatch.wave_manager import squash_merge

        mock_run.return_value = _SUCCESS_MOCK  # all subprocess calls succeed

        mock_ci_gate = MagicMock()
        mock_ci_gate.run.side_effect = Exception("CI gate error: tests failed")

        with (
            patch.dict(
                sys.modules,
                {"agent_gtd_dispatch.wave_manager.ci_gate": mock_ci_gate},
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            squash_merge.main()

        assert exc_info.value.code == 1
        mock_rmtree.assert_called_once()

    @patch("sys.argv", _BASE_ARGV)
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.shutil.rmtree")
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.subprocess.run")
    def test_push_failure_exits_one(self, mock_run, mock_rmtree) -> None:
        """Push subprocess returncode=1 → SystemExit(1), workspace cleaned up."""
        from agent_gtd_dispatch.wave_manager import squash_merge

        # clone, fetch, checkout-branch, checkout-main, pull, merge, commit succeed;
        # push fails
        mock_run.side_effect = [
            _SUCCESS_MOCK,  # git clone
            _SUCCESS_MOCK,  # git fetch origin <branch>
            _SUCCESS_MOCK,  # git checkout <branch>
            _SUCCESS_MOCK,  # git checkout main
            _SUCCESS_MOCK,  # git pull origin main
            _SUCCESS_MOCK,  # git merge --squash
            _SUCCESS_MOCK,  # git commit -F -
            _fail_mock("push rejected"),  # git push origin main
        ]

        with pytest.raises(SystemExit) as exc_info:
            squash_merge.main()

        assert exc_info.value.code == 1
        mock_rmtree.assert_called_once()

    @patch("sys.argv", _BASE_ARGV)
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.shutil.rmtree")
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.subprocess.run")
    def test_workspace_cleaned_on_any_failure(self, mock_run, mock_rmtree) -> None:
        """shutil.rmtree is called even when clone fails."""
        from agent_gtd_dispatch.wave_manager import squash_merge

        mock_run.return_value = _fail_mock("repository not found")

        with pytest.raises(SystemExit) as exc_info:
            squash_merge.main()

        assert exc_info.value.code == 1
        # Cleanup must have been called regardless of failure
        mock_rmtree.assert_called_once()

    @patch("sys.argv", _BASE_ARGV)
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.shutil.rmtree")
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.subprocess.run")
    def test_missing_branch_exits_one(self, mock_run, mock_rmtree) -> None:
        """git fetch fails → SystemExit(1) before any merge attempt."""
        from agent_gtd_dispatch.wave_manager import squash_merge

        mock_run.side_effect = [
            _SUCCESS_MOCK,  # git clone
            _fail_mock("fatal: couldn't find remote ref feat/abc12345"),  # git fetch
        ]

        with pytest.raises(SystemExit) as exc_info:
            squash_merge.main()

        assert exc_info.value.code == 1
        # Merge should never have been attempted — only 2 subprocess calls
        assert mock_run.call_count == 2
        mock_rmtree.assert_called_once()

    @patch("sys.argv", _BASE_ARGV)
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.shutil.rmtree")
    @patch("agent_gtd_dispatch.wave_manager.squash_merge.subprocess.run")
    def test_ci_gate_import_error_is_skipped(self, mock_run, mock_rmtree) -> None:
        """ImportError on ci_gate import → logs warning, merge proceeds normally."""
        from agent_gtd_dispatch.wave_manager import squash_merge

        mock_run.return_value = _SUCCESS_MOCK

        # Ensure ci_gate is absent from sys.modules so import raises ImportError.
        # ci_gate.py does not exist in the package; this patch makes the test hermetic.
        modules_copy = {
            k: v
            for k, v in sys.modules.items()
            if k != "agent_gtd_dispatch.wave_manager.ci_gate"
        }
        with patch.dict(sys.modules, modules_copy, clear=True):
            squash_merge.main()

        # Success: no SystemExit raised, workspace cleaned up
        mock_rmtree.assert_called_once()
